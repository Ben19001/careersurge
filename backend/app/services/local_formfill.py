"""The local app's form filler: scrape a Greenhouse, Lever or Ashby application form, fill it
from the local profile, and click submit behind a caller-supplied fence.

Copied from what the local app needed of the earlier hosted `formfill.py`, since deleted. It imports
neither Supabase nor openai (httpx only through `local_answers`' Jev client).
Dropped on purpose: the Groq matcher (the local app never lets a model write a value) and
`screener_answers` (the Q&A bank, `local_answers`, replaced it). Every value a field receives
comes from the profile row, an answer the user saved, a consent the user authorized, or the form's
own decline option; a required field none of those answers stays in `missing`.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

from app.services import local_answers, local_profile, us_states

logger = logging.getLogger("careersurge.services.local_formfill")


PLAYWRIGHT_ACTION_TIMEOUT = int(os.getenv("PLAYWRIGHT_ACTION_TIMEOUT", "10000"))
# How long to wait for a network-backed autocomplete (Ashby's and Greenhouse's location fields) to
# render options after a keystroke. Its own budget rather than a multiple of the action timeout,
# because it is the only wait here that covers a round trip to someone else's geo service AND
# competes with every other page for the CPU: the same Ashby "Location" field that fills on the
# first try when a page has the box to itself timed out on 4 of 5 parallel fills at 20s. Every
# other wait in the fill path is on an element that is already in the DOM.
COMBOBOX_OPTION_TIMEOUT_MS = int(os.getenv("COMBOBOX_OPTION_TIMEOUT_MS", "30000"))
# How long to wait on a query that may simply have no matches, before trying a broader one. A
# service with results answers in a second or two; the long budget above is only worth paying on
# the last term in the ladder, when giving up means the field goes unfilled.
COMBOBOX_QUERY_TIMEOUT_MS = int(os.getenv("COMBOBOX_QUERY_TIMEOUT_MS", "6000"))
# Dispatch retries, bounded by attempts AND by time already spent on the field — see the loop in
# `fill_form_fields` for why both bounds are needed. The count matches `RESUME_ATTACH_ATTEMPTS`,
# which has been retrying the one upload path for as long as it has existed.
FILL_DISPATCH_ATTEMPTS = int(os.getenv("FILL_DISPATCH_ATTEMPTS", "3"))
FILL_FIELD_BUDGET_MS = int(os.getenv("FILL_FIELD_BUDGET_MS", "20000"))

# Location-ish fields, the only ones allowed to fall back to the candidate's country: answering
# "How did you hear about us?" with "United States" would answer a different question.
_LOCATION_FIELD = re.compile(r"\blocation\b|\bcountry\b|where are you|where do you|based in", re.I)
# Attach attempts for the résumé upload. Retried where the other fills are not because the bytes
# are cached per user per process, so a retry re-attaches without refetching, and a résumé is
# required on nearly every form — an upload that does not stick fails the whole application.
RESUME_ATTACH_ATTEMPTS = int(os.getenv("RESUME_ATTACH_ATTEMPTS", "3"))
RESUME_ATTACH_BACKOFF = 0.4
_REFERRAL_COMPANY_SITE = re.compile(r"company (web)?site|career site|our (web)?site|job board",
                                    re.I)
_REFERRAL_LINKEDIN = re.compile(r"linked.?in", re.I)
# "How did you hear about us?" in any of the wordings the corpus ships, for the one place that
# needs to recognise the QUESTION rather than match an alias to a column.
_REFERRAL_FIELD = re.compile(r"how did you (hear|find|first learn)|where did you hear"
                             r"|referral source", re.I)
# Which state a named metro sits in, so "are you in Boston / New York / Philadelphia?" can be
# answered "No" by someone in California without answering it wrongly for someone in Cambridge.
# Only the metros the corpus actually names, and only to decide when NOT to answer — an unknown
# city returns nothing rather than a guess.
_MONTH_ORDER = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_US_STATE_OF = {
    "boston": "ma", "cambridge": "ma", "new york": "ny", "new york city": "ny", "nyc": "ny",
    "brooklyn": "ny", "philadelphia": "pa", "pittsburgh": "pa", "san francisco": "ca",
    "los angeles": "ca", "san diego": "ca", "san jose": "ca", "oakland": "ca", "chicago": "il",
    "seattle": "wa", "austin": "tx", "dallas": "tx", "houston": "tx", "denver": "co",
    "atlanta": "ga", "miami": "fl", "washington": "dc", "washington dc": "dc", "boulder": "co",
    "north carolina": "nc", "california": "ca", "new jersey": "nj", "massachusetts": "ma",
}
# A submit POST plus its redirect is its own budget. Deliberately NOT folded into
# PLAYWRIGHT_ACTION_TIMEOUT, whose value is calibrated against ~150 real forms and which every
# other control on the page depends on.
SUBMIT_CONFIRM_TIMEOUT_MS = int(os.getenv("SUBMIT_CONFIRM_TIMEOUT_MS", "45000"))

# How long the scraper waits for a client-rendered (SPA) application form to render its
# <label> elements before reading them. domcontentloaded fires before React hydrates the form
# (e.g. Ashby), so without this the scrape sees zero labels. Overridable via env.
FORM_RENDER_TIMEOUT_MS = int(os.getenv("FORM_RENDER_TIMEOUT_MS", "15000"))

# In-page form scraper: one page.evaluate returns every field descriptor at once.
#
# Why in-page and not Playwright locators: Ashby renders its required asterisk as a CSS
# `::after { content: "*" }`, which never appears in innerText and has no locator API —
# getComputedStyle is the only way to read it. Batching also collapses ~9 CDP round-trips
# per label into one per page (~150 -> 1 on a 17-field form).
#
# Required-ness is an OR-union of independent signals, deliberately biased toward "required".
# `fill_form_fields` reports ok = not missing, and `apply._apply_one` treats that as "form complete",
# so a false negative would let an incomplete form read as submittable. A false positive only
# costs a skip. Measured against Ashby's own Apollo cache (FormFieldEntry.isRequired) on the
# OpenAI board: the pseudo-star and label-class signals each agree 17/17; control attributes
# alone agree 11/17; the old `"*" in innerText` agreed 6/17 and caught zero required fields.
_FIELD_SCRAPE_JS = r"""
() => {
  const CONTROL_SEL = 'input,select,textarea,[role="combobox"],[role="radiogroup"],[role="listbox"]';
  const STAR    = /[*✱＊★]/;        // ASCII *, heavy asterisk (Lever), fullwidth, star
  const REQ_CLS = /(^|[\s_-])required(?![a-z])/i; // "required", "_required_f7cvd_91", "is-required"
  const REQ_TXT = /\(\s*required\s*\)/i;          // literal "(required)" label noise

  // Every label-side required signal, in one place. Scoped to the label's own subtree — it
  // must NOT widen to the walked-up container: Ashby's radio options ("Male", "Female") are
  // themselves <label>s sharing a fieldset with the required group heading, so a
  // container-scoped read would mark every option row required.
  const labelSaysRequired = (label) => {
    const text = label.innerText || '';
    if (STAR.test(text) || REQ_TXT.test(text)) return true;  // Greenhouse, Lever
    if (label.getAttribute('aria-required') === 'true') return true;
    const nodes = [label, ...label.querySelectorAll('span,abbr,em,i,sup')];
    return nodes.some(n => REQ_CLS.test(n.className || '')   // Ashby _required_<hash>
      || ['::after', '::before'].some(p => {                 // Ashby CSS-only asterisk
           const c = window.getComputedStyle(n, p).content;
           return c && c !== 'none' && STAR.test(c);
         }));
  };

  // The label that owns a control: its wrapping <label> (Lever) or the one whose `for` points
  // at it (Greenhouse/Ashby). Used to stop a group heading from claiming its first option's
  // radio — those option labels are SIBLINGS via `for`, so closest('label') alone misses them
  // and heading + first option end up sharing one input_id.
  const ownerLabel = (el) => el.closest('label')
    || (el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null);

  const rows = [];
  let stamp = 0;
  [...document.querySelectorAll('label')].forEach((label, index) => {
    const text = (label.innerText || '').trim();
    if (!text || text === 'None') return;

    const labelFor = label.getAttribute('for');
    // Resolve the control. Greenhouse hits (1); Lever labels carry no `for` at all and wrap
    // their input, so they hit (2); Ashby comboboxes / yes-no button pairs / checkbox groups
    // carry a `for` pointing at an id that does not exist, so they fall through to (3).
    let control = labelFor ? document.getElementById(labelFor) : null;
    if (!control) control = label.querySelector(CONTROL_SEL);
    let container = label.parentElement;
    let fieldPathEl = null;
    for (let hop = 0; hop < 4 && container; hop++) {
      if (!control) {
        // Skip controls another label owns. A group heading ("Gender") legitimately resolves
        // to nothing: its options are separate rows, and it stays required-but-unfilled rather
        // than silently sharing "Male"'s radio.
        control = [...container.querySelectorAll(CONTROL_SEL)]
          .find(c => { const o = ownerLabel(c); return !o || o === label; }) || null;
      }
      // Remember the field-path wrapper but KEEP WALKING while unbound. Breaking here left
      // Ashby's location combobox — an id-less, name-less <input role="combobox"> that sits
      // outside the wrapper — permanently unresolved, so its row carried a `for` id that does
      // not exist in the DOM and could never be filled.
      if (!fieldPathEl && container.hasAttribute('data-field-path')) fieldPathEl = container;
      if (control) break;
      container = container.parentElement;
    }
    if (fieldPathEl) container = fieldPathEl;

    let required = labelSaysRequired(label);
    if (!required && control) {
      required = control.hasAttribute('required')
        || control.getAttribute('aria-required') === 'true';
    }
    if (!required && container && container.getAttribute('aria-required') === 'true') required = true;

    // input_id must stay stable across scrapes (DB dedupe key + LLM map key); selector is
    // this-session-only and may lean on an attribute we stamp right now.
    const fieldPath = container ? container.getAttribute('data-field-path') : null;
    let inputId = null, selector = null;
    if (control) {
      let name = control.getAttribute('name');
      const ctlType = control.getAttribute('type');
      if (name && (ctlType === 'checkbox' || ctlType === 'radio')) {
        // A checkbox/radio `name` is the GROUP key — every Lever pronoun option carries
        // name="pronouns". Qualify with the option value so each row keys to its own control
        // instead of collapsing the group onto one last-write-wins entry.
        const value = control.getAttribute('value');
        name = value ? name + '::' + value : null;
      }
      inputId = control.id || name || fieldPath || labelFor || ('cs-' + index);
      // Every bound control is stamped, with or without an id: the submit ladder's
      // form-subtree rung and the "form still mounted" test after the click both look for
      // `data-cs-field`, and Greenhouse's controls all carry ids, so stamping only id-less ones
      // left both blind on exactly the board that needs them.
      control.setAttribute('data-cs-field', String(stamp));
      if (control.id) {
        // Attribute selector, not "#{id}": ATS ids (Ashby UUIDs) can start with a digit,
        // which is an invalid CSS id selector and throws in querySelectorAll.
        selector = '[id="' + control.id + '"]';
        stamp++;
      } else {
        selector = '[data-cs-field="' + (stamp++) + '"]';
      }
    } else {
      inputId = fieldPath || labelFor || null;
    }

    let inputType = 'input';
    let options = [];
    let maxLength = null;
    let listboxId = null;
    let needsClick = false;
    let groupKey = null;
    let groupLabel = null;
    // The ancestor that owns the whole radio/checkbox group, and the selector that finds its
    // members. Resolved once below and used twice: to name the question, and to read the answers.
    let groupBox = null;
    let groupSel = null;
    // Whether `options` came from real form controls rather than from button text. Only the
    // former makes a selectorless row worth keeping — see the filter at the end of this script.
    let optionsAreControls = false;
    if (control) {
      const tag = control.tagName.toLowerCase();
      const role = control.getAttribute('role');
      const haspopup = control.getAttribute('aria-haspopup');
      const type = control.getAttribute('type');
      // Per-field char limit the form itself declares (250/1500/...): the single source of
      // truth for length validation downstream — no hardcoded cap.
      const mx = control.getAttribute('maxlength');
      if (mx && /^\d+$/.test(mx) && Number(mx) > 0) maxLength = Number(mx);

      if (tag === 'select') {
        inputType = 'select';
        options = [...control.options].map(o => o.text.trim()).filter(Boolean);
      } else if (role === 'combobox' || haspopup === 'true' || haspopup === 'listbox') {
        inputType = 'select';
        listboxId = control.getAttribute('aria-controls');
        if (listboxId) {
          const box = document.getElementById(listboxId);
          if (box) options = [...box.querySelectorAll('[role="option"]')]
            .map(o => o.innerText.trim()).filter(Boolean);
        }
        // Options render only once the listbox is opened; Python clicks these afterwards.
        needsClick = options.length === 0;
      } else if (tag === 'textarea') {
        inputType = 'textarea';
      } else if (type === 'file') {
        // Résumé/CV upload. Must be typed BEFORE the group-widget pass below, whose comment
        // records the exact bug of a file input being retyped as a select.
        inputType = 'file';
      } else if (type === 'checkbox' || type === 'radio') {
        inputType = type;
      }
      // Mutually exclusive options share one `name` (Lever's radios, Ashby's option rows). That
      // name is the GROUP identity, and the coverage tally needs it: without it each option of a
      // required pick-one counts as its own unmet requirement, so a correctly filled form still
      // reports missing=['Yes','No',...] and can never be ok.
      if (inputType === 'checkbox' || inputType === 'radio') {
        groupKey = control.getAttribute('name') || null;
        // Report the QUESTION, not the option. Lever renders the prompt outside any <label>, so
        // a grouped row's own text is just "Yes" — without this the coverage report names four
        // requirements as "Yes / No" and tells nobody which questions went unanswered.
        // Walk up to the ancestor that actually OWNS the group — the nearest one holding more
        // than one control of this `name`. `closest('fieldset,li,...')` alone lands on the
        // OPTION's own wrapper on Lever, which gives each option its own <li>; its first line is
        // then just "Yes", equal to the row's own text, so the guard below nulled groupLabel and
        // the coverage report named the requirement "Yes / No" instead of the question.
        // Two conditions, and both are load-bearing: the ancestor must hold more than one
        // control of this group (so it is the question's container, not one option's), AND its
        // first line must differ from this row's own text. Lever nests
        // <label> < <li> < <ul> < .application-field before the element that carries the
        // prompt, and every one of those already contains both radios while still reading
        // "Yes" — stopping at the first multi-control ancestor lands on <ul> and yields nothing.
        let box = null;
        const own = text.trim().toLowerCase();
        if (groupKey) {
          groupSel = '[name="' + groupKey.replace(/["\\]/g, '\\$&') + '"]';
          // The NEAREST ancestor holding more than one member. This is the group's own
          // container — on Lever the <ul> around the option <li>s — and it is what the option
          // sweep further down reads the answers from.
          for (let c = control.parentElement, hop = 0; hop < 8 && c; hop++, c = c.parentElement) {
            if (c.querySelectorAll(groupSel).length > 1) { groupBox = c; break; }
          }
          // Every ANSWER in this group, so the climb below can tell a question from an option.
          // Testing only against the row's own text is not enough: for the "No" row of a Yes/No
          // pair the group's own <ul> reads "Yes", which differs from "no", so the walk stopped
          // there and reported the question as "Yes" — the model then received a field asking
          // "Yes" whose options were ["Yes", "No"] and correctly answered nothing.
          const answers = new Set();
          if (groupBox) {
            groupBox.querySelectorAll(groupSel).forEach(c => {
              const o = ownerLabel(c);
              if (o) answers.add((o.innerText || '').trim().toLowerCase());
            });
          }
          answers.add(own);
          // The question lives higher: keep climbing until the text stops echoing an answer.
          for (let c = groupBox || control.parentElement, hop = 0; hop < 8 && c;
               hop++, c = c.parentElement) {
            if (c.querySelectorAll(groupSel).length < 2) continue;
            const lg = c.querySelector('legend');
            const txt = ((lg ? lg.innerText : c.innerText) || '')
              .split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
            if (txt && !answers.has(txt.toLowerCase())) { box = c; break; }
          }
        }
        box = box || control.closest('fieldset,li,[data-field-path]');
        if (box) {
          const legend = box.querySelector('legend');
          const head = (legend ? legend.innerText : box.innerText) || '';
          const first = head.split('\n').map(s => s.trim()).filter(Boolean)[0] || null;
          // Only when the wrapper actually carries the prompt. Where the options ARE the
          // wrapper's first line, this would just echo "Yes" and read as the question.
          groupLabel = (first && first.toLowerCase() !== text.trim().toLowerCase())
            ? first : null;
        }
      }
    }

    // Group widgets expose their choices as siblings, not <option>s: Ashby yes/no button pairs
    // (state lives in a hidden checkbox) and radio/checkbox groups (heading resolves to no
    // control). Gated on those two shapes only — running it unconditionally retyped a
    // <input type="file"> as a select because the resume upload button sat in its container.
    if (!options.length && container
        && (!control || inputType === 'checkbox' || inputType === 'radio')) {
      // Read the group's members from the ancestor that OWNS the group, not from this row's own
      // parent. `container` is the option's wrapper on Lever — one <li> holding one radio — so
      // the sweep found a single control, the `o !== label` test then discarded it as this row's
      // own, and `choices` came back empty for every grouped question on the board. That one
      // empty list is why 65% of remaining unfilled requirements were choice fields with no
      // options: the LLM skips its option-match branch, `_consent_values` has no affirmative to
      // pick, `_EEO_DECLINE_INTENT` has no decline wording, and the dispatcher has nothing to
      // match against — four separate failures from one bad container.
      const members = groupBox && groupSel
        ? [...groupBox.querySelectorAll(groupSel)]
        : [...container.querySelectorAll('input[type="radio"],input[type="checkbox"]')];
      const choices = members
        .map(c => {
          const o = ownerLabel(c);
          if (!o) return '';
          // A heading row owns no control, so its own label is the QUESTION and must never be
          // listed as one of the answers. An option row IS one of the answers, so it keeps its
          // own label — without this a Yes/No pair yields ["No"] alone and fails the >= 2 gate
          // below, which is the whole reason these fields scraped empty.
          if (o === label && !control) return '';
          return (o.innerText || '').trim();
        })
        .filter(Boolean);
      // Only toggle buttons (Ashby's Yes/No set `aria-pressed`) are answers. A lone checkbox whose
      // container also holds an "Upload" or "Remove" button must not offer those as choices.
      const buttons = [...container.querySelectorAll('button[aria-pressed]')]
        .map(b => (b.innerText || '').trim()).filter(Boolean);
      if (choices.length >= 2) { options = choices; optionsAreControls = true; }
      else if (buttons.length >= 2) options = buttons;
      // A heading with choices is a pick-one question; leave a resolved control's own type
      // alone so the fill dispatcher keeps acting on what the element actually is.
      if (!control && options.length) inputType = 'select';
    }

    rows.push({
      label: text, input_id: inputId, selector: selector, input_type: inputType,
      required_field: required, options: options, max_length: maxLength,
      listbox_id: listboxId, needs_click: needsClick, group_key: groupKey,
      group_label: groupLabel, options_are_controls: optionsAreControls,
    });
  });

  // Lever's custom questions have no <label> at all: the prompt sits in the question's
  // `.application-label .text` div, beside a bare <select> or <textarea>. The label pass never sees
  // them, so a required one was missing from `required_total` and a real run would click Submit
  // with it empty. Bind every control in a Lever question the label pass left
  // unstamped. Option rows of a radio or checkbox group do have labels, so they are already bound.
  document.querySelectorAll('li.application-question').forEach((question) => {
    const prompt = question.querySelector('.application-label .text');
    if (!prompt) return;
    const promptText = (prompt.innerText || '').split('\n')[0].replace(STAR, '').trim();
    if (!promptText) return;
    question.querySelectorAll('select,textarea,input').forEach((control) => {
      if (control.hasAttribute('data-cs-field')) return;
      if (['hidden', 'file', 'checkbox', 'radio', 'submit', 'button'].includes(control.type)) return;
      control.setAttribute('data-cs-field', String(stamp));
      const tag = control.tagName.toLowerCase();
      const mx = control.getAttribute('maxlength');
      rows.push({
        label: promptText,
        input_id: control.id || control.getAttribute('name') || ('cs-' + stamp),
        selector: '[data-cs-field="' + (stamp++) + '"]',
        input_type: tag === 'select' ? 'select' : (tag === 'textarea' ? 'textarea' : 'input'),
        required_field: control.hasAttribute('required') || STAR.test(prompt.innerText || '')
          || !!prompt.querySelector('.required'),
        options: tag === 'select' ? [...control.options].map(o => o.text.trim()).filter(Boolean) : [],
        max_length: mx && /^\d+$/.test(mx) && Number(mx) > 0 ? Number(mx) : null,
        listbox_id: null, needs_click: false, group_key: null, group_label: null,
        options_are_controls: false,
      });
    });
  });

  // Résumé/CV inputs routinely escape the label pass: Ashby and Lever render a styled drop-zone
  // ("Upload File" / "ATTACH RESUME/CV") over a hidden <input type="file"> whose label neither
  // wraps it nor carries a `for` that resolves. Sweep for file inputs the pass did not bind and
  // attach them. Hidden is fine — set_input_files does not require visibility.
  const boundSelectors = new Set(rows.map(r => r.selector).filter(Boolean));
  [...document.querySelectorAll('input[type="file"]')].forEach((el, i) => {
    let sel = el.id ? '[id="' + el.id + '"]' : null;
    if (sel && boundSelectors.has(sel)) return;
    if (!sel) {
      if (el.hasAttribute('data-cs-field')) return;   // already bound by the label pass
      el.setAttribute('data-cs-field', String(stamp));
      sel = '[data-cs-field="' + (stamp++) + '"]';
    }
    // Prefer whatever the user actually sees: the owning label, else the nearest container's
    // text, else the input's own name.
    let name = '';
    const own = ownerLabel(el);
    if (own) name = (own.innerText || '').trim();
    let c = el.parentElement;
    for (let hop = 0; hop < 4 && c && !name; hop++) {
      name = (c.innerText || '').trim();
      c = c.parentElement;
    }
    // The nearest container is usually just the drop-zone, whose text is the BUTTON — "UPLOAD
    // FILE". That is not what the form is asking for. Veeva's card reads "Please upload a copy of
    // your resume. ✱" one level up, and because the label was the button text the résumé matcher
    // could not tell that ask apart from "Provide writing samples" on the same form. Keep climbing
    // while the text is a bare button phrase, and stop at the first container that says more.
    const GENERIC = /^(upload|attach|choose|browse|select|add|drop)\b[\s\w]{0,16}$/i;
    for (let hop = 0; hop < 4 && c && GENERIC.test(name); hop++) {
      const outer = (c.innerText || '').trim();
      if (outer.length > name.length && outer.length < 300) name = outer;
      c = c.parentElement;
    }
    name = name || el.getAttribute('name') || ('file-' + i);

    // The label pass usually DID see the question ("Resume") but could not bind a control, so it
    // emitted a selectorless row. Adopt that row rather than emitting a duplicate — two rows for
    // one upload would double-count it in the required tally.
    const orphan = rows.find(r => !r.selector && /resum|cv|upload|attach/i.test(r.label || ''));
    if (orphan) {
      orphan.selector = sel;
      orphan.input_type = 'file';
      return;
    }
    rows.push({
      label: name.split('\n')[0] || name,
      input_id: el.id || el.getAttribute('name') || ('cs-file-' + i),
      selector: sel, input_type: 'file',
      required_field: el.hasAttribute('required')
        || el.getAttribute('aria-required') === 'true' || STAR.test(name),
      options: [], max_length: null, listbox_id: null, needs_click: false, group_key: null,
    });
  });

  // Drop rows that bound no control and are not a real pick-one group. Nothing can ever fill one
  // — there is no element to act on — yet a required one still counts toward `required_total` and
  // lands in `missing`, so it forces ok=false on every attempt.
  //
  // The live case is Greenhouse's résumé widget: its Attach / Dropbox / Google Drive / Enter
  // manually pills are <button>s, so the group pass reads them as a four-option question named
  // "Enter manually" with no control behind it, on every Figma form. The upload itself is a
  // separate row the file sweep binds, so this one is pure duplicate.
  //
  // `options_are_controls` is what separates that from a genuine radio/checkbox group heading,
  // whose options ARE bound as their own rows and whose row the coverage tally needs. Runs AFTER
  // the file sweep, which adopts selectorless rows to attach the résumé upload to the question
  // the label pass already saw — filtering earlier would delete the row that sweep looks for.
  return rows.filter(r => r.selector || r.options_are_controls);
}
"""

# (label alias → profile column), priority order — the FIRST alias that matches owns the field.
# Work-eligibility aliases precede location keys so "authorized to work in the United States"
# cannot leak into "state"; specific location keys precede generic ones.
#
# Shared with the LLM validator, which uses it in the opposite direction: given a label, the
# column named here is the ONLY profile source admissible as an answer to it.
_LABEL_ALIASES = (
    ("first name", "first_name"),
    ("last name", "last_name"),
    ("family name", "last_name"),
    ("surname", "last_name"),
    ("preferred name", "preferred_name"),
    ("full name", "full_name"),
    # Ashby's "Legal Name" box. "Legal first name" and "legal last name" never get
    # here: first name and last name above match them first.
    ("legal name", "full_name"),
    # Both spellings: aliases match on word boundaries, so "pronoun" does NOT match the plural —
    # and "Pronouns" is how every form writes it, so this alias matched nothing at all.
    ("pronouns", "pronouns"),
    ("pronoun", "pronouns"),
    ("email", "email"),
    ("phone", "phone"),
    ("mobile", "phone"),
    ("linkedin", "linkedin_url"),
    ("github", "github_url"),
    ("portfolio", "portfolio_links"),
    ("website", "portfolio_links"),
    # The "How did you hear about us?" family. Held back until `referral_source` had a write path
    # (the profile API now reads and writes it) because an alias onto an always-NULL column is
    # strictly worse than no alias: the scope_key branch below admits that column as the ONLY
    # source, so an empty one grounds nothing and the answer is dropped `wrong_profile_field`,
    # where with no alias it fell through to global grounding and sometimes passed. That "passed"
    # was luck, not correctness — global grounding answers "How did you hear about us?" with any
    # token in the profile — so the drop is the right behaviour for a user who has not set the
    # column, and onboarding capture is what turns it into a fill.
    # Status questions, placed BEFORE the generic keys: "bound by any agreements" would otherwise
    # never reach an alias, and "employee of the united states government" must not be read as a
    # work-eligibility or country question.
    # Relocation before the generic location keys, or "relocate to the job's location" matches
    # `location` and gets answered with the candidate's city — an address where a yes/no belongs.
    ("willing to relocate", "relocation_willingness"),
    ("relocate", "relocation_willingness"),
    ("relocation", "relocation_willingness"),
    ("years of industry experience", "years_experience"),
    ("years of relevant professional experience", "years_experience"),
    ("years of professional experience", "years_experience"),
    ("bound by any agreement", "restrictive_covenants"),
    ("non compete", "restrictive_covenants"),
    ("restrictive covenant", "restrictive_covenants"),
    ("post government employment", "post_gov_restrictions"),
    ("post employment restriction", "post_gov_restrictions"),
    ("employee of the united states government", "prior_gov_employment"),
    ("civilian or military employee", "prior_gov_employment"),
    ("how did you hear", "referral_source"),
    ("how did you find", "referral_source"),
    ("how did you first learn", "referral_source"),
    ("where did you hear", "referral_source"),
    ("referral source", "referral_source"),
    ("legally authorized", "authorized_to_work_us"),
    ("authorized to work", "authorized_to_work_us"),
    ("work authorization", "authorized_to_work_us"),
    # Veeva's wording. Same question, none of the three above match it, and the citizenship
    # derivation below answers it the moment an alias points at the column.
    ("unrestricted right to work", "authorized_to_work_us"),
    ("right to work", "authorized_to_work_us"),
    ("require sponsorship", "requires_sponsorship"),
    ("need sponsorship", "requires_sponsorship"),
    ("sponsorship", "requires_sponsorship"),
    ("citizenship", "us_citizenship_status"),
    ("security clearance", "security_clearance_info"),
    ("clearance", "security_clearance_info"),
    ("veteran", "veteran_status"),
    ("gender", "gender"),
    ("ethnicity", "ethnicity"),
    ("race", "ethnicity"),
    ("sexual orientation", "lgbtq_status"),
    ("lgbtq", "lgbtq_status"),
    ("salary", "salary_expectation"),
    ("compensation", "salary_expectation"),
    # Availability. Placed BEFORE the location keys because this list is first-match-wins
    # and "available"/"start" are generic enough to collide — the same reason the work-eligibility
    # aliases above precede them, so "authorized to work in the United States" cannot leak into
    # "state". Routed through label_map rather than the LLM on purpose: `_match_fields_label_map`
    # bypasses `_is_grounded` entirely, and an LLM answer like "Two weeks from offer" carries
    # `from` and `offer`, tokens that appear in no profile, so grounding drops it no matter what.
    # Reading "two" as "2" would fix only the number half, not the extra tokens, so these are not alternatives.
    ("when can you start", "availability_notice"),
    ("earliest start", "availability_notice"),
    ("start date", "availability_notice"),
    ("notice period", "availability_notice"),
    ("available to start", "availability_notice"),
    ("availability", "availability_notice"),
    ("address line 2", "address_line2"),
    ("address line 1", "address_line1"),
    ("street address", "address_line1"),
    ("address", "address_line1"),
    ("city", "city"),
    ("postal code", "zip_code"),
    ("zip", "zip_code"),
    ("province", "state"),
    ("state", "state"),
    ("country", "country"),
    ("current location", "current_location"),
    ("currently located", "current_location"),
    ("located", "current_location"),
    ("location", "current_location"),
)
# Columns stored as booleans; a form wants the words, not True/False.
_BOOL_PROFILE_KEYS = {"authorized_to_work_us", "requires_sponsorship", "veteran_status"}

# Both work-eligibility columns are US-specific (`authorized_to_work_us`), but boards ask the same
# question about their own country - "Are you legally authorized to work in Japan?", "Do you
# require work permit sponsorship in Canada?" - and answering those from a US column asserts
# something the profile never said. Such a question is left missing; the two names below only
# recognise it.
# Matched against the RAW label, and only capitalized runs count: a country is a proper noun, so
# "…require sponsorship to work in Japan?" names one and "Will you now or in the future require
# sponsorship?" does not. Lowercasing first made "the future" a country and flipped that answer.
_WORK_AUTH_IN_COUNTRY = re.compile(r"\bin\s+(?:the\s+)?((?:[A-Z][\w.]*)(?:\s+[A-Z][\w.]*)*)")
_US_NAMES = {"us", "usa", "united states", "united states of america", "america"}


def _match_fields_label_map(fields: list[dict], user_row: dict, listing: dict | None = None,
                            extra_aliases: tuple = ()) -> dict[str, str]:
    """Deterministic matcher over `_LABEL_ALIASES`, keyed by input_id.

    Zero-latency, no model. Aliases match on word boundaries — plain substring let
    "United States" satisfy the "state" alias — and the first matching alias owns the field even
    when its column is empty, so a miss falls through to `missing` rather than to a later,
    less specific alias answering a question it has no business answering. `extra_aliases` are
    `(normalized label, column)` pairs from the label cache, tried after every built-in alias
    so a mapped column is answered exactly as an alias match would be.
    """
    aliases = _LABEL_ALIASES + tuple(extra_aliases)
    out: dict[str, str] = {}
    # How many rows each question owns, so an answer matched on the QUESTION can be given to the
    # one row that is that answer rather than to all of them (see the assignment below).
    siblings: dict[str, int] = {}
    for f in fields:
        if f.get("group_label"):
            siblings[f["group_label"]] = siblings.get(f["group_label"], 0) + 1
    for field in fields:
        input_id = field.get("input_id")
        if not input_id:
            continue
        # Lever ships a grouped pick-one as one row per option and NO row for the question: the
        # question text exists only as `group_label` on each option row, whose own labels are
        # "Yes" and "No". Matching the option text alone, this path could not see a single one of
        # those questions — "Do you require work permit sponsorship in Canada?" was invisible to
        # it and reachable only by the model. The question is tried second, so a child with a
        # meaningful label of its own ("City" under an "Address" heading) still wins on its own.
        own_label = field.get("label", "")
        own_norm = normalize_role(own_label)
        group_label = field.get("group_label")
        # A field labelled exactly "Name" is the candidate's name, and it cannot live in the alias
        # table: matching there is substring-with-word-boundary and first-match-wins, so a bare
        # "name" alias would claim "Name of your previous employer", "Company Name" and "Preferred
        # Name" before their own aliases ever ran. Exact label only, and only when the field takes
        # free text — a "Name" with options is a pick-list of something else.
        if (_squash_label(own_label) == "name" and not (field.get("options") or [])
                and user_row.get("full_name")):
            out[input_id] = str(user_row["full_name"])
            continue
        # A row whose label is one of its group's OPTIONS is an answer, not a question, and must
        # never be alias-matched on its own text. Shield AI asks "How did you hear about us?" over
        # nine radio rows; the row "LinkedIn Post" matches the `linkedin` alias and "Company
        # website" matches `website`, so the rule below fired on every row, the question was never
        # consulted, and a question we hold the answer to filled nothing at all.
        is_option_row = bool(group_label) and any(
            _squash_label(own_label) == _squash_label(o) for o in (field.get("options") or []))
        candidates = [] if is_option_row else [own_norm]
        # The question is tried second so a child with a meaningful label of its own ("City" under
        # an "Address" heading) still wins on its own — that child is not one of the options.
        if group_label and (is_option_row or not any(
                re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", own_norm) for a, _ in
                aliases)):
            candidates.append(normalize_role(group_label))
        norm = next((c for c in candidates
                     if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", c)
                            for a, _ in aliases)), own_norm)
        for alias, key in aliases:
            if not re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", norm):
                continue
            value = user_row.get(key)
            if key == "portfolio_links":
                value = (value or [None])[0]
            elif key in _BOOL_PROFILE_KEYS:
                value = None if value is None else ("Yes" if value else "No")
                if key in ("authorized_to_work_us", "requires_sponsorship"):
                    # Answered from the column alone. A question that names another country is
                    # not about this column, so it stays missing. The country is named in
                    # whichever label the alias matched - for a Lever option row that is the
                    # group's question, not this row's "Yes". US names are checked as PREFIXES of
                    # the captured run so "...in the US without sponsorship?" reads as the US.
                    raw = str(group_label if norm != own_norm else field.get("label", ""))
                    named = _WORK_AUTH_IN_COUNTRY.search(raw) if any(c.islower() for c in raw) \
                        else None
                    words = re.sub(r"[^a-z ]+", "", named.group(1).lower()).split() if named else []
                    if words and not any(" ".join(words[:n]) in _US_NAMES for n in (1, 2, 3, 4)):
                        value = None
            elif key == "relocation_willingness" and (field.get("options") or []):
                # Cloudflare offers three sentences: "I currently live in this job's location." /
                # "I am willing to relocate to this job's location." / "I do not live and not
                # willing…". A stored "Yes" matches none of them, and the first two are BOTH true
                # of a willing candidate — which one is correct depends on where the job is, so it
                # cannot be answered from the profile alone. With the listing in hand it can.
                options = field.get("options") or []
                here = re.compile(r"currently live|already live|i live", re.I)
                willing = re.compile(r"willing to relocate|able to relocate", re.I)
                job_place = _plain_label((listing or {}).get("location") or "")
                mine = [p.strip().lower() for p in str(user_row.get("current_location") or "")
                        .split(",") if p.strip()]
                local = bool(job_place and mine and any(p and p in job_place for p in mine))
                # Each arm falls back to the stored value, so a plain Yes/No question still gets
                # the plain answer. Without that fallback an unwilling candidate lost their "No"
                # entirely on any form that did not phrase the options as sentences — the fixture
                # says "Yes", so only a user who declines would ever have hit it.
                if local:
                    value = next((o for o in options if here.search(o)), None) or value
                elif _CONSENT_NEGATIVE.match(str(value or "")):
                    # Not willing, and not local: the option that says both, else the bare "No".
                    value = next((o for o in options
                                  if _CONSENT_NEGATIVE.search(o) and willing.search(o)), None) \
                        or value
                elif value:
                    value = next((o for o in options if willing.search(o)
                                  and not _CONSENT_NEGATIVE.match(o)), None) or value
            elif key == "current_location" and (field.get("options") or []) and value:
                # "Are you located in one of the following cities or surrounding areas: Boston,
                # New York, Philadelphia or North Carolina?" — a yes/no whose answer is written
                # into the question. The candidate's city cannot go into a Yes/No field, so
                # without this the options gate below discards it and the question goes unanswered.
                #
                # "No" is only ever answered on an unambiguous miss. The phrase these questions all
                # carry is "or surrounding areas", so a candidate in Cambridge belongs to Boston
                # without naming it — hence the city→state map: a listed city in the candidate's
                # own state means we cannot tell, and cannot-tell answers nothing.
                asked = field.get("group_label") or own_label
                # Only when the question actually ENUMERATES places — a colon or "including"
                # followed by two or more names. "Are you located near an office?" has no list to
                # compare against, and parsing its words as place names answered "No" to a
                # question nobody had asked.
                parts = re.split(r":|\bincluding\b", asked, 1)
                listed = [p.strip(" .?") for chunk in parts[1:]
                          for p in re.split(r",|\bor\b|\band\b", chunk) if p.strip(" .?")]
                if len(listed) < 2:
                    listed = []
                mine = [p.strip().lower() for p in str(value).split(",") if p.strip()]
                city, state = (mine + ["", ""])[0], (mine + ["", ""])[1]
                names = {p.lower() for p in listed}
                if city in names or (state and state in names) or any(
                        _US_STATE_OF.get(p.lower()) == state for p in listed):
                    value = "Yes"
                elif listed and all(p.lower() not in (city, state)
                                    and _US_STATE_OF.get(p.lower(), "?") != state for p in listed):
                    value = "No"
                else:
                    value = None
            elif key == "referral_source":
                # "How did you hear about us?" is the most-asked question in the corpus and the
                # only honest answer for an applier that found the job on the company's own board
                # IS the company's own board — a description of how the application happened, not
                # a claim about the user, which is what separates it from `availability_notice`.
                #
                # Answered only when the column holds a value. The option search then looks for
                # the user's own value first and, failing that, the form's spelling of the
                # company's own board ("Company website", "Our website") or LinkedIn, because
                # every board words the same choice differently. An empty column stays missing:
                # picking either of those would claim how the user heard of the job.
                options = field.get("options") or []
                if options and value:
                    stored = str(value).strip().lower()
                    value = (next((o for o in options if _squash_label(o) == _squash_label(stored)),
                                  None)
                             or next((o for o in options if _REFERRAL_COMPANY_SITE.search(o)), None)
                             or next((o for o in options if _REFERRAL_LINKEDIN.search(o)), None))
            elif key == "availability_notice" and (field.get("options") or []) and value:
                # Veeva's new-grad boards ask "When would you be available to start in this role?"
                # and offer cohort dates — 'October 2026', 'July 2027'. A notice period cannot
                # match either. Someone available immediately is available for the earliest cohort,
                # so pick it; anything that is not a clean list of dates answers nothing.
                dates = []
                for option in field.get("options") or []:
                    when = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                                     r"\s+(\d{4})", option, re.I)
                    if not when:
                        dates = []
                        break
                    dates.append((int(when.group(2)), _MONTH_ORDER[when.group(1).lower()], option))
                immediate = re.search(r"immediate|now|asap|any\s*time", str(value), re.I)
                value = min(dates)[2] if dates and immediate else None
            elif key == "availability_notice" and not value:
                # Onboarding only writes this column when the user explicitly confirms it, so it is
                # usually empty and "When can you start?" went unanswered.
                #
                # A notice period is a COMMITMENT, not a preference, so a blanket default ("2
                # weeks") would put a claim about the user onto a live employer's form — the exact
                # fabrication `_is_grounded` exists to stop, just entered one layer earlier where
                # that guard cannot see it. Derivation from data the user actually gave us is a
                # different thing.
                #
                # An editor entry flagged `current` is a role they are still in. If there is none, they
                # are not currently employed and "Immediately" is INFERRED, not invented. If there
                # is one, we cannot know their notice period and the field stays missing, which is
                # the honest answer.
                current = [e for e in (user_row.get("work_experience") or [])
                           if isinstance(e, dict) and e.get("current")]
                value = None if current else "Immediately"
            # A field that carries options can only be answered with one of them. Without this,
            # an alias that matches a yes/no question ("...where the job is located?") hands it a
            # free-text profile value, and the dispatcher then force-checks a box to answer a
            # question the user never answered.
            options = field.get("options") or []
            if options:
                value = _option_for_answer(value, options)
            # When the alias matched the GROUP's question, every row of that group is holding the
            # same answer — and the dispatcher would click "Yes", then "No", and report whichever
            # came last. In a multi-row group the answer belongs to the row that IS that answer.
            # A one-row group (a lone input under a heading) keeps it: there is no other row it
            # could belong to.
            if (value and norm != own_norm and siblings.get(group_label, 0) > 1
                    and _squash_label(field.get("label", "")) != _squash_label(value)):
                value = None
            if value:
                out[input_id] = str(value)    # years_experience is an INTEGER column
            break
    return out


# --- Gated auto-consent (deterministic, authorized, audited) ---------------
# Standard/delegable attestations the user can authorize us to affirm at signup. Kept OUT of
# the LLM: the legal decision is a deterministic classifier + a per-user authorization flag.
_CONSENT_STANDARD_PATTERN = (
    r"certif|true and accurate|attest|information .*(correct|accurate)"
    r"|authoriz.*contact|permission to contact"
    # "polic(y|ies)" AND "notice": Greenhouse ships a required "Privacy notice*" box that missed
    # this pattern by one word and fell through to `atypical`, where nothing can affirm it — a
    # standard data-processing consent classified as an undelegable one.
    r"|consent.*(process|data)|privacy (polic|notice)|data processing|gdpr"
    # Interview recording / AI notetaking. This is data-processing consent — the same act as
    # agreeing to a privacy policy — and Sierra's wording carries no other consent vocabulary, so
    # it classified `atypical` (undelegable) or as no consent field at all. `transcrib` matches
    # transcribe/transcription and NOT "transcript", which is a document ask, not a consent.
    r"|notetak|note.?tak|transcrib|recording and summar"
    # "I affirm that all the information and materials I submit will reflect my own work and
    # experience" is the same act as "I certify the above is true", worded the way a company
    # policy acknowledgement words it. Anchored to the phrasing, never the bare verb: `affirm`
    # alone matched "employed at Affirm" and classified a former-employer question as a consent
    # attestation — the corpus has a company by that name.
    r"|i affirm|affirm that|confirm i have read"
    r"|equal (employment )?opportunity")
_CONSENT_STANDARD = re.compile(_CONSENT_STANDARD_PATTERN)

# Consumer-report authorization, split out of the standard tier and gated on its OWN flag.
# FCRA §604(b) requires this disclosure to be standalone — "a document that consists solely of the
# disclosure" — with separate written authorization, so affirming it under a general delegation
# buried in a ToS is the exact fact pattern the rule exists to prevent. California's ICRAA is
# stricter still: statutory damages without proof of harm.
_CONSENT_BACKGROUND_PATTERN = r"background check|consumer report|background screening|screening"
_CONSENT_BACKGROUND = re.compile(_CONSENT_BACKGROUND_PATTERN)

# Arbitration, promoted out of `atypical` onto its own flag for the same reason background was
# promoted out of `standard`: not because it is less serious, but because a delegation the user
# gave for something else cannot carry it. Signing an arbitration clause as someone's agent gives
# up their jury trial and their class participation against that employer, and at the moment the
# general authorization is granted the employer is not chosen and the clause does not exist —
# there is nothing specific to have authorized. So it needs a standalone disclosure that names
# arbitration, its own flag, its own version string and its own timestamp.
#
# NON-COMPETES AND IP ASSIGNMENT ARE DELIBERATELY NOT IN HERE. They stay `atypical` and stay
# refused: a disclosure naming arbitration is not authority to restrain where someone may work
# next or to hand over what they build. Widening this pattern would silently re-scope a consent
# the user gave for one thing.
_CONSENT_ARBITRATION_PATTERN = r"arbitrat"
_CONSENT_ARBITRATION = re.compile(_CONSENT_ARBITRATION_PATTERN)

# Any consent-ish box. Matches _CONSENT_ANY but NOT any tier pattern ⇒ atypical (non-compete,
# IP assignment, ...) → never auto-checked.
#
# COMPOSED from the tier patterns rather than written out by hand. The invariant is that
# every label a tier will auto-affirm must ALSO match _CONSENT_ANY — otherwise the model guard has
# a hole for exactly the labels the deterministic path affirms, which is the worst possible place
# for one. Hand-maintaining two lists did not hold that invariant: `_CONSENT_STANDARD` matched
# "You have my permission to contact my references", "I attest the information above is accurate",
# "The information I have provided is correct" and "Equal employment opportunity
# self-identification", none of which the old broad pattern caught. Composing makes the superset
# property structural instead of a rule someone has to remember.
# `assign|non.?compet|non.?solicit|restrictive covenant|confidentiality` are here for the MODEL
# GUARD, not for any tier: "I assign all intellectual property created during employment to the
# Company" matched none of the words above, so `_CONSENT_ANY` did not match it, so the guard in
# `_match_fields_llm` never dropped the model's answer — an LLM "Yes" would have ticked an
# IP-assignment box with only the system prompt standing in the way. Matching here fixes that
# without making any of them affirmable: they classify `atypical`, which no flag can authorize.
# The things no delegation can carry, named separately because two places need them: the broad
# pattern below (so the model guard sees them) and `_consent_values` (so a row that names one is
# never re-read as its question's milder tier).
_CONSENT_UNDELEGABLE_PATTERN = (r"\bassign|non.?compet|non.?solicit|restrictive covenant"
                                r"|confidentiality|intellectual property")
_CONSENT_UNDELEGABLE = re.compile(_CONSENT_UNDELEGABLE_PATTERN)
_CONSENT_BROAD_PATTERN = (r"agree|consent|terms|privacy|acknowledge|authoriz|self.?identif|"
                          + _CONSENT_UNDELEGABLE_PATTERN)
_CONSENT_ANY = re.compile("|".join((_CONSENT_BROAD_PATTERN, _CONSENT_STANDARD_PATTERN,
                                    _CONSENT_BACKGROUND_PATTERN, _CONSENT_ARBITRATION_PATTERN)))
# "Are you authorized to work in the US?" is a work-eligibility question, not a consent box, but
# it trips _CONSENT_ANY on "authoriz" and was being discarded as an attestation the model may not
# answer. Exempt the eligibility phrasings — they carry no legal delegation.
# A question ABOUT the candidate's existing obligations is not an agreement to take one on.
# "Are you currently bound by any agreements with a current or former employer that may restrict
# your ability to work…?" is a screener question the candidate answers from fact; classifying it
# as consent blocks the model from answering it AND leaves it undelegable, i.e. permanently
# unanswerable. The discriminator is the ASSENT VERB, not the question mark: an interrogative that
# never asks the applicant to agree/consent/accept/acknowledge/authorize/certify/sign is a
# status question. "Do you agree to the non-compete?" contains one and stays consent.
# The assent verbs are anchored to their phrasings for the same reason the standard pattern is:
# a bare `affirm` matches the company Affirm and a bare `sign` matches "significant"/"design".
_CONSENT_STATUS_QUESTION = re.compile(
    r"^(?:are|do|does|did|have|has|had|will|would|is|was|were)\b"
    # `agree(?!ment)`: "Do you agree to…" is assent, "bound by any agreements" is a description of
    # contracts that already exist — the noun is exactly what these status questions ask about.
    r"(?!.*(?:\b(?:agree(?!ment)|consent|accept|acknowledg|authoriz|certif|attest)\w*\b"
    r"|\bi affirm\b|\baffirm that\b|\bsign (?:this|the|below)\b|\bsignature\b|\bopt.in\b))")
_WORK_ELIGIBILITY = re.compile(
    r"authoriz\w*\s+to\s+work|work\s+authoriz|legally\s+authoriz|eligible\s+to\s+work")

# --- EEO / self-identification (answered by decline, never by guess) -------
# Tight allowlist of the protected-characteristic questions. Deliberately excludes `pronouns`,
# which is not an EEO category.
_EEO_QUESTION = re.compile(
    r"\bgender\b|\brace\b|ethnic|hispanic|latino|veteran|disability|disabled"
    # `transgender` is its own word here: it does NOT contain "gender" as a \b-delimited token,
    # so Greenhouse's "I identify as transgender (please select one)" matched nothing and fell
    # through to the model, which correctly declined and left the requirement unmet.
    r"|transgender"
    r"|sexual orientation|lgbtq|self.?identif")
# Deliberately still NOT matched: Greenhouse's bare "I identify as:" heading. Its wording alone
# does not say which characteristic it asks about, and the two neighbours it ships beside are
# "I identify as transgender" (protected, now caught above) and "I identify as a first-generation
# professional" (not a protected characteristic, and not ours to auto-decline). A pattern loose
# enough to catch the first would catch the second, so that one stays with the model — which
# declines it, leaving it in `missing` rather than guessing. Fail-closed, and visible.
# An accommodation REQUEST is not a status question — answering it "decline" would refuse help
# the candidate may actually need.
_EEO_NOT_A_STATUS = re.compile(r"accommodat|assistance|adjustment")
# The wording ATSes actually ship, case-insensitively: Ashby "Decline to self-identify" and
# "I do not want to answer", Greenhouse "Decline To Self Identify".
_EEO_DECLINE = re.compile(
    r"decline"
    # "I don't wish to answer" (Greenhouse), "I do not want to answer" (Ashby). Curly apostrophe
    # included — matching only the ASCII one silently missed the option on a live form.
    r"|do(?:n[’']?t| not)\s+(?:wish|want|care)\s+to\s+(?:answer|disclose|say|identify)"
    r"|prefer\s+not\s+to|choose\s+not\s+to|rather\s+not\s+(?:answer|say)"
    r"|not\s+wish\s+to\s+(?:answer|disclose)", re.I)
# Greenhouse renders its four required EEO questions as comboboxes whose options only exist once
# the menu is opened, so at match time there is no option list to pick a decline FROM. This stands
# in for "answer this with whatever decline option the form itself offers", and the combobox
# dispatcher resolves it against the live menu. It must never reach a text or checkbox field.
_EEO_DECLINE_INTENT = "\x00eeo-decline"
# The same device for an AUTHORIZED consent rendered as a combobox. Sierra ships its interview-
# recording consent that way: `_consent_values` could classify the tier and confirm the user had
# granted it, then had to fail closed because there was no option list to pick an affirmative FROM,
# leaving a question nothing could answer. This stands in for "affirm this with whatever
# affirmative the form itself offers", resolved against the live menu by the combobox dispatcher.
# Only ever emitted for a tier the user has already authorized — it carries no authority of its own.
_CONSENT_AFFIRM_INTENT = "\x00consent-affirm"
# The word the dispatcher ticks or clicks to affirm a consent box, and the probe it resolves against
# a form's own options ("Yes, I consent"). A signal to the dispatcher, not profile data.
_AFFIRM = "Yes"


def _plain_label(label) -> str:
    """Lowercase + collapse whitespace ONLY — NOT normalize_role (which rewrites role
    synonyms like 'pm'→'product manager' and would corrupt legal-label matching)."""
    return " ".join(str(label).lower().split())


def normalize_role(text: str) -> str:
    """MECHANICAL normalization only: lowercase + alnum + single spaces, "(required)" dropped.

    Copied from `common.normalize_role`; here it is the form-label normalizer. It must not
    rewrite synonyms ("pm" -> "product manager"), which would corrupt legal-label matching.
    """
    text = (text or "").lower()
    text = re.sub(r"\(required\)", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return " ".join(text.split())



def _option_for_answer(value, options: list) -> str | None:
    """The option that expresses `value`, or None. Shared by all three paths that answer a select.

    Three matches, narrowing in confidence and stopping at the first:

    1. Verbatim (case-insensitive) — the model was asked for one of these and usually gives it.
    2. Punctuation-flattened, for the decoration boards wrap options in ("Yes" / "Yes," / "Yes —").
    3. A clean negative against the ONE negative option. Affirm asks "Have you previously been
       employed at Affirm?" with five sentence-length options; the candidate's stored answer is
       "No", the model relays "No", and neither matches any option verbatim, so a question we had
       the answer to failed. Exactly one of the five is a negative, which makes the mapping
       unambiguous.

    The affirmative direction is deliberately NOT mirrored: four of Affirm's five options are
    affirmative variants (full-time / part-time / intern / contractor) and nothing in a "Yes" says
    which, so a "Yes" resolves nothing and the field stays missing. And a negative option is never
    chosen just because it is the safe one — only when the candidate's own answer is negative.
    """
    if not options or value in (None, ""):
        return None
    text = str(value).strip()
    exact = next((o for o in options if o.strip().lower() == text.lower()), None)
    if exact is not None:
        return exact
    squashed = next((o for o in options if _squash_label(o) == _squash_label(text)), None)
    if squashed is not None:
        return squashed
    # Cloudflare's "Country*" is a phone dial-code list — 'United States +1', 'Afghanistan +93' —
    # so a stored "United States" matched nothing. Strip the trailing code and compare again.
    # Deliberately not a prefix match: that would leave 'United States +1' and 'United States
    # Minor Outlying Islands +246' both matching "United States", and the wrong one is a
    # different country.
    dialled = next((o for o in options
                    if _squash_label(re.sub(r"\s*\+\d{1,4}\s*$", "", o)) == _squash_label(text)),
                   None)
    if dialled is not None:
        return dialled
    if _CONSENT_NEGATIVE.match(text):
        negatives = [o for o in options if _CONSENT_NEGATIVE.search(o)]
        if len(negatives) == 1:
            return negatives[0]
    # 4. A number against banded options. Veeva asks for a GPA and offers '3.7 - 4.0',
    #    '3.3 - 3.69', … 'Below 2.7'; the stored answer is "3.7" and matches none of them
    #    literally. A number is either inside a range or it is not, so this decides without
    #    judgement — and a value outside every band still returns None rather than the nearest.
    number = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", text)
    if number:
        want = float(number.group(1))
        for option in options:
            bounds = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", option)]
            if not bounds:
                continue
            if len(bounds) >= 2 and min(bounds) <= want <= max(bounds):
                return option
            # One-sided bands: "Below 2.7", "Under 5", "3.0+", "More than 10".
            if len(bounds) == 1:
                if re.search(r"below|under|less than|fewer than|<", option, re.I) \
                        and want < bounds[0]:
                    return option
                if re.search(r"above|over|more than|at least|\+|>", option, re.I) \
                        and want >= bounds[0]:
                    return option
    return None


def _squash_label(text) -> str:
    """`_plain_label` with punctuation flattened too: 'Yes — I have.' -> 'yes i have'.

    For comparing two renderings of the SAME string across sources — a stored screener question
    against the form's wording, a model's answer against an option's text. Forms decorate options
    with required stars, curly quotes and non-breaking spaces, and an exact comparison reads that
    decoration as a different answer. Never used for classification (a squashed legal label loses
    the sentence boundaries the consent patterns rely on).
    """
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _eeo_decline_values(fields: list[dict], user_row: dict) -> tuple[dict[str, str], list[dict]]:
    """Answer an EEO/demographic question the candidate never disclosed with the form's OWN
    decline option, and an audit trail of every one.

    Returns ({input_id: option}, [{input_id, label, option}]). These questions may never be
    guessed — inventing a race or a veteran status is fabricating a protected characteristic —
    but leaving them blank is not neutral either: they are frequently required, so a blank makes
    the whole application incompletable. Every ATS offers an explicit decline, which is a truthful
    answer for someone who has not disclosed, so that is what we select.

    Three guards keep this honest: the label must be a demographic question (an accommodation
    request is NOT a disability-status question), the mapped profile column must be EMPTY (a
    disclosed value always wins), and the decline text must be one of the field's own options —
    never text we synthesized.
    """
    values: dict[str, str] = {}
    audit: list[dict] = []
    # Every row's own label. A group whose question is in here has a heading row (Ashby's shape).
    heading_labels = {f.get("label") for f in fields}
    for f in fields:
        iid = f.get("input_id")
        options = f.get("options") or []
        if not iid:
            continue
        # One option of a checkbox or radio group is an ANSWER, and the question is its group_label.
        # Lever's ethnicity options read "Bi-Racial/Mixed Race - Having 2 or more racial/ethnic
        # groups", which matches the EEO pattern on its own, so every such option was "declined" in
        # turn. Each decline clicked the same "I prefer not to disclose" box, the clicks undid one
        # another, and the fallback ticked the option itself: a race the user never gave (audit
        # 2026-10-24). So an option row is judged by its group's question, and only the option whose
        # own text is the decline takes it. A group with a heading row is left to the heading below.
        group_label = f.get("group_label")
        is_option_row = bool(group_label) and f.get("input_type") in ("checkbox", "radio")
        if is_option_row and group_label in heading_labels:
            continue
        question = group_label if is_option_row else f.get("label", "")
        plain = _plain_label(question)
        if not _EEO_QUESTION.search(plain) or _EEO_NOT_A_STATUS.search(plain):
            continue
        if is_option_row and not _EEO_DECLINE.search(_plain_label(f.get("label", ""))):
            continue
        norm = normalize_role(question)
        key = next((k for a, k in _LABEL_ALIASES
                    if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", norm)), None)
        if key and user_row.get(key):
            continue                      # they disclosed it; their answer stands
        if is_option_row:
            decline = f.get("label")      # this option is the decline, checked above
        elif options:
            decline = next((o for o in options if _EEO_DECLINE.search(_plain_label(o))), None)
        elif f.get("input_type") == "select":
            # Options unreadable until the menu opens; defer the choice to the dispatcher.
            decline = _EEO_DECLINE_INTENT
        else:
            decline = None                # no options and not a menu — nothing to decline with
        # Ashby ships the question as a heading row that resolves to NO control, with the options
        # as sibling radio rows carrying the real ids. Assigning the decline to the heading
        # answered nothing — the fill loop has no element to click — while the audit still
        # reported a decline. Retarget the child row whose own label IS the decline, and take its
        # exact wording: the option we click must be the form's, never text we picked.
        target_id = iid
        if not f.get("selector"):
            child = next((c for c in fields
                          if c.get("group_label") == f.get("label") and c.get("input_id")
                          and _EEO_DECLINE.search(_plain_label(c.get("label", "")))), None)
            if child:
                target_id, decline = child["input_id"], child["label"]
        if decline:
            values[target_id] = decline
            audit.append({"input_id": target_id, "label": question, "option": decline})
    return values, audit


# Affirmative option text a consent <select> or radio group actually ships. A consent select whose
# options are none of these falls to `missing`, which is the correct failing-closed direction, but
# the vocabulary has to cover the common wordings or Needs you fills with noise.
_CONSENT_AFFIRMATIVE = ("yes", "y", "i agree", "agree", "agreed", "i accept", "accept",
                        "accepted", "i consent", "consent", "i acknowledge", "acknowledge",
                        "acknowledged", "i certify", "certify", "confirm", "i confirm", "true")
# Real forms rarely label the option with the bare word. Palantir's notetaker question offers
# "Yes, I consent" / "No, I do not consent", and exact-set membership matched neither, so an
# authorized consent question failed closed for want of vocabulary rather than for want of
# authority. Matched on the opening token, with a negation guard because "No, I do not consent"
# also contains "consent" — reading that one as agreement is the failure that matters here.
_CONSENT_AFFIRMATIVE_OPENS = re.compile(
    r"^\s*(?:yes|i\s+(?:agree|consent|accept|acknowledge|certify|confirm)"
    r"|agree|accept|acknowledge|certify|confirm|true)\b", re.I)
_CONSENT_NEGATIVE = re.compile(r"\b(?:no|not|never|decline|refuse|disagree|opt[- ]?out)\b|n't",
                               re.I)


def _consent_tier(plain_label: str) -> str | None:
    """Which consent tier a label falls into, or None if it is not a consent field at all.

    Shared by `_consent_values`, which acts on it, and the coverage report, which explains it — the
    two must agree or the report would name a tier the filler never used.

    Tested strictest-first. The tier patterns are not supposed to overlap, but ordering makes the
    precedence a property of the code rather than of that assumption, and a real form will
    eventually ship one box reading "I certify the above is true and agree to binding arbitration"
    — which must be gated on the arbitration flag, not on the weaker one it also matches.
    """
    # A work-eligibility QUESTION is not a delegation. It trips `_CONSENT_ANY` on "authoriz", and
    # the carve-out that fixed that for the model guard was never applied here, so "Are you legally
    # authorized to work in Japan?" classified `atypical` — consent-shaped, undelegable, and unable
    # to be answered by anything. Checked first: it is an exemption from the whole vocabulary.
    if _WORK_ELIGIBILITY.search(plain_label) or _CONSENT_STATUS_QUESTION.match(plain_label):
        return None
    if _CONSENT_BACKGROUND.search(plain_label):
        return "background"
    if _CONSENT_ARBITRATION.search(plain_label):
        return "arbitration"
    if _CONSENT_STANDARD.search(plain_label):
        return "standard"
    return "atypical" if _CONSENT_ANY.search(plain_label) else None


# Which flags must ALL be set, and which column holds the ToS version recorded in the audit, for
# each tier a user can delegate. `atypical` is absent on purpose: no flag authorizes it. The two
# single-purpose flags are required ALONGSIDE `auto_consent_authorized`, never instead of it.
_CONSENT_TIERS = {
    "standard": (("auto_consent_authorized",), "consent_tos_version"),
    "background": (("auto_consent_authorized", "background_check_authorized"),
                   "background_check_tos_version"),
    "arbitration": (("auto_consent_authorized", "arbitration_authorized"),
                    "arbitration_tos_version"),
}
# When a row touches two tiers, the one with the narrower authority governs it.
_TIER_STRICTNESS = ("atypical", "arbitration", "background", "standard")


def _consent_values(fields: list[dict], user_row: dict) -> tuple[dict[str, str], list[dict]]:
    """Consent fields to auto-affirm + an audit trail, ONLY under the authority the user granted.

    Returns ({input_id: affirmative}, [{input_id, label, tos_version, authority}]).

    FOUR tiers, and which one a label falls into decides which flag governs it:

      standard     truthfulness attestations, contact permission, data-processing/EEO consent.
                   Governed by `auto_consent_authorized`.
      background   consumer reports and pre-employment screening. Governed by its OWN
                   `background_check_authorized` flag, because FCRA §604(b) requires a standalone
                   disclosure with separate written authorization — a general delegation cannot
                   carry it. Unauthorized, this pass never ticks it; only an answer the user saved can.
      arbitration  binding-arbitration acknowledgements. Own flag for the same reason, arrived at
                   through agency law rather than statute: signing one as the user's agent gives up
                   their jury trial and their class participation, and the general authorization is
                   granted before any employer or clause exists, so there is nothing specific in it
                   to have authorized.
      atypical     non-compete, IP assignment, anything else consent-shaped. This pass never ticks
                   it under any flag; only an answer the user saved can, and Needs you asks for a required one.

    The two single-purpose flags are both required ALONGSIDE `auto_consent_authorized`, never
    instead of it: they widen a delegation that has to exist first, and a user who revoked the
    general authorization has not left a narrower one standing.

    Each audit entry names the authority used and the version string of THAT authority, so a
    dispute can be answered with what the user actually agreed to and when.

    Handles checkbox, radio AND select. The `select` case was previously invisible to this
    deterministic path while also being invisible to the model guard, so a consent dropdown was
    neither auto-affirmed correctly nor kept away from the LLM.

    `input` and `textarea` are DELIBERATELY excluded, and this is not an oversight to be tidied up
    later: those are signature fields ("type your full name", "enter your initials"), and typing a
    name is a legally distinct act from ticking a box — it attests that the user personally
    completed the form. That is not something we could stand behind if challenged, so it fails
    closed and surfaces in `missing` instead. Deliberate coverage sacrifice.
    """
    values: dict[str, str] = {}
    audit: list[dict] = []
    # How many rows each question owns. A lone acknowledgement box has exactly one child; a
    # yes/no pair has two — and inheriting a tier into a pair would tick BOTH halves.
    siblings: dict[str, int] = {}
    for f in fields:
        if f.get("group_label"):
            siblings[f["group_label"]] = siblings.get(f["group_label"], 0) + 1
    for f in fields:
        iid = f.get("input_id")
        itype = f.get("input_type")
        if not iid or itype not in ("checkbox", "radio", "select"):
            continue
        # A child row's own label is not the legal act: Ashby's certification block renders the
        # attestation as a heading and the only control as a checkbox labelled "I confirm I have
        # read the above." — which matches no tier pattern, so the question went unanswered while
        # its authorized tier sat one row away in `group_label`. Consider BOTH, then take the
        # strictest: a child reading "I agree" under an arbitration heading is an arbitration
        # waiver, so the tier with the narrower authority always wins over the broader one.
        own_plain = _plain_label(f.get("label", ""))
        own_tier = _consent_tier(own_plain)
        group_label = f.get("group_label")
        # A pick-one whose OPTIONS are its rows: Lever ships "…we may use AI notetakers… see our
        # candidate privacy policy" as a radio pair labelled "Yes, I consent" / "No, I do not
        # consent". The question is standard-tier data-processing consent; the rows are not, and
        # `_consent_tier("yes, i consent")` reads `atypical` — undelegable, so an authorized
        # question went unanswered. Inherit the QUESTION's tier, which is where the legal act
        # actually is. Safe for a multi-row group because the affirmative-option selection below
        # picks exactly one row; the `siblings == 1` guard exists for the OTHER shape, a lone
        # checkbox with no options, where every row would otherwise be ticked.
        group_tier = None
        if group_label and (siblings.get(group_label) == 1 or f.get("options")):
            group_tier = _consent_tier(_plain_label(group_label))
        # A vague row ("Yes, I consent") reads `atypical` only because its words name no tier, so
        # it defers to its heading's tier. Unless the row itself names an undelegable act ("I agree
        # to the non-compete" under a plain "Acknowledgements" heading): then the row says more.
        if group_tier and own_tier == "atypical" and not _CONSENT_UNDELEGABLE.search(own_plain):
            own_tier = None
        row_tiers = {own_tier, group_tier}
        governing_tier = next((t for t in _TIER_STRICTNESS if t in row_tiers), None)
        if governing_tier not in _CONSENT_TIERS:
            continue                          # atypical or not consent: never ticked here
        required_flags, tos_column = _CONSENT_TIERS[governing_tier]
        # A flag ticked under older wording counts as unset: the user never saw the text now in force,
        # so the box waits in Needs you until they tick it again.
        if not all(local_profile.consent_is_current(user_row, flag) for flag in required_flags):
            continue                          # flag not set or stale: never ticked, never affirmed by implication
        tos = user_row.get(tos_column)
        options = f.get("options") or []
        if options:
            # Same case-insensitive comparison the LLM validator uses on options, deliberately:
            # consistent behaviour between the two paths matters more than local convenience.
            affirmative = next(
                (o for o in options
                 if o.strip().lower() in _CONSENT_AFFIRMATIVE
                 or (_CONSENT_AFFIRMATIVE_OPENS.match(o) and not _CONSENT_NEGATIVE.search(o))),
                None)
            if affirmative is None:
                continue                      # no affirmative option to pick -> fail closed
        elif itype == "select" and not group_label and not siblings.get(f.get("label", "")):
            # A STANDALONE consent combobox whose options load on click. The tier is known and the
            # authority granted; only the wording is unavailable here, so defer to the dispatcher
            # rather than fail closed on a question nothing else can answer
            # (see _CONSENT_AFFIRM_INTENT). Deliberately not for a row inside a group: there the
            # real control is a sibling, and emitting an intent on the heading as well would have
            # the dispatcher answering the same question twice.
            affirmative = _CONSENT_AFFIRM_INTENT
        elif itype == "select":
            continue                          # menu unreadable at match time -> fail closed
        else:
            affirmative = _AFFIRM             # lone checkbox/radio: the dispatcher checks it
        # In a group whose rows ARE the options, every row shares the same option list and would
        # otherwise be given the same answer — the dispatcher then clicks "Yes, I consent" and
        # "No, I do not consent" in turn and the form keeps whichever landed last.
        if (group_label and siblings.get(group_label, 0) > 1
                and _squash_label(f.get("label", "")) != _squash_label(affirmative)):
            continue
        values[iid] = affirmative
        audit.append({"input_id": iid, "label": f.get("label", ""), "tos_version": tos,
                      "authority": governing_tier})
    return values, audit


async def fill_form_fields(page, fields: list[dict], user_row: dict,
                            listing: dict | None = None) -> dict:
    """Fill a scraped application form from the user's profile - WITHOUT submitting.

    `fields` is the list from `grab_page_fields`; `user_row` is the decoded profile
    (`profile_for_fill`). Called by `local_apply` for the real run and for `fill_check`. Never
    locates or clicks a submit button.

    Values, in precedence order: label-map answers from the profile, EEO declines for the gaps
    (a disclosed value always wins), then consent affirmations, which win for their own ids and
    exist only under a flag the user set. When `listing` is given, fields still empty then try the
    user's saved answers (`local_answers.find_answer`), and required fields no alias knows try the
    label cache and Jev (`local_answers.map_labels`, which only picks a profile column; the value is
    copied from the profile). No model writes a value. Returns
    {filled, required_total, required_filled, missing, missing_shapes, missing_detail, ok, consented,
    declined}; `missing_shapes` is the control of each missing question (see its comment below).
    """
    consent_values, consented = _consent_values(fields, user_row)
    decline_values, declined = _eeo_decline_values(fields, user_row)
    values = _match_fields_label_map(fields, user_row, listing)
    for iid, value in decline_values.items():
        values.setdefault(iid, value)
    values.update(consent_values)
    declined = [d for d in declined if d["input_id"] in values]

    # Saved uploads by input_id, and checkbox-group answers by input_id, both filled from the bank.
    upload_payloads: dict[str, dict] = {}
    picked_items: dict[str, list[str]] = {}
    if listing is not None:
        # The fields nothing above answered. A question is the group heading when the field is
        # one option of a pick-one group, else its own label; a heading row owns its question, so
        # the option rows under it are skipped. The lookups hit SQLite and Jev, so they run off the
        # event loop.
        heading_labels = {f.get("label") for f in fields}
        group_sizes: dict[str, int] = {}
        for f in fields:
            if f.get("group_label"):
                group_sizes[f["group_label"]] = group_sizes.get(f["group_label"], 0) + 1

        # The fields still open: they have an id, no value yet, and are not a row under a heading
        # row (the heading carries the question). An upload is open too: a file the user saved for
        # it is attached further down, and the résumé input keeps its own path.
        open_fields = []
        for f in fields:
            group = f.get("group_label")
            has_id = bool(f.get("input_id"))
            has_value = f.get("input_id") in values
            has_question = bool(group or f.get("label"))
            is_under_heading = bool(group) and group in heading_labels
            if has_id and not has_value and has_question and not is_under_heading:
                open_fields.append(f)

        saved_answers: dict[tuple[str, bool], str | None] = {}
        for f in open_fields:
            group = f.get("group_label")
            question = group or f["label"]
            is_upload = f.get("input_type") == "file"
            if (question, is_upload) not in saved_answers:
                saved_answers[(question, is_upload)] = await asyncio.to_thread(
                    local_answers.find_answer, question, listing.get("company") or "",
                    listing.get("company_key"), listing.get("id"), file=is_upload)
            value = saved_answers[(question, is_upload)]
            if is_upload:
                payload = await asyncio.to_thread(local_answers.attachment_payload, value) if value else None
                if payload:
                    upload_payloads[f["input_id"]] = payload
                    values[f["input_id"]] = payload["name"]
                continue
            # A checkbox question's answer is a JSON list. A checkbox row takes the item that is its
            # own label; a heading row over option controls keeps the list for the pass below, which
            # ticks every member; a text box gets the items joined; a single pick-one gets nothing.
            items = None
            if value and value.startswith("["):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                    items = parsed
            if items is not None:
                if f.get("input_type") == "checkbox":
                    value = next((item for item in items
                                  if _squash_label(item) == _squash_label(f.get("label", ""))), None)
                elif f.get("options_are_controls"):
                    picked_items[f["input_id"]] = items
                    value = ", ".join(items)
                elif f.get("input_type") in ("input", "textarea"):
                    value = ", ".join(items)
                else:
                    value = None
            elif value and f.get("options"):
                value = _option_for_answer(value, f["options"])
            # A pick-one group has one row per choice, and every row was asked the same question.
            # Only the row whose own text IS the answer ("Yes") takes it, or the dispatcher would
            # click every row in turn. A group of one row keeps the answer.
            is_choice_row = bool(group) and group_sizes[group] > 1
            row_is_the_answer = value and _squash_label(f.get("label", "")) == _squash_label(value)
            if is_choice_row and not row_is_the_answer:
                value = None
            if value:
                values[f["input_id"]] = value

        # Only REQUIRED fields no alias knows go to the label cache and Jev: an optional unknown
        # question stays blank, and a label an alias owns is answered (or left missing) by it. A
        # consent or attestation is never mapped to a profile column: only the user authorizes one.
        unknown_labels = []
        for f in open_fields:
            question = f.get("group_label") or f["label"]
            plain = normalize_role(question)
            alias_known = any(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", plain)
                              for alias, _ in _LABEL_ALIASES)
            is_consent = _consent_tier(_plain_label(question)) is not None
            still_open = f["input_id"] not in values
            is_upload = f.get("input_type") == "file"
            needs_jev = (still_open and f.get("required_field") and not alias_known and not is_consent
                         and not is_upload)
            if needs_jev and question not in unknown_labels:
                unknown_labels.append(question)
        if unknown_labels:
            mapping = await asyncio.to_thread(local_answers.map_labels, unknown_labels)
            mapped_aliases = tuple((normalize_role(label), column) for label, column in mapping.items())
            for iid, value in _match_fields_label_map(fields, user_row, listing, mapped_aliases).items():
                values.setdefault(iid, value)

    # A group heading is not a widget. The scrape retypes a selectorless heading that owns
    # radio/checkbox children as `select` (§scrape), so a value resolved for the QUESTION lands on
    # a row with no control and reaches the combobox dispatcher, which finds no listbox and raises
    # — the largest single cause of unmet required questions in the load test ("Are you legally
    # authorized to work in the United States?", "Will you require visa sponsorship?").
    # The answer to such a question IS one of its children, so move the value onto the child whose
    # own label is that answer and let the checkbox/radio dispatcher click it. The group tally
    # below already re-points a child at its heading's key, so the question counts as filled.
    for field in fields:
        iid = field.get("input_id")
        if not field.get("options_are_controls") or not values.get(iid):
            continue
        want = _plain_label(values[iid])
        members = [f for f in fields if f.get("group_label") == field.get("label")
                   and f.get("selector") and f.get("input_id")]
        if iid in picked_items:
            # A checkbox question: every member whose label is one of the saved items is ticked.
            wanted_items = {_squash_label(item) for item in picked_items[iid]}
            ticked = [f for f in members if _squash_label(f.get("label", "")) in wanted_items]
            for member in ticked:
                values.setdefault(member["input_id"], member["label"])
            if ticked:
                del values[iid]
            continue
        member = next((f for f in members if _plain_label(f.get("label", "")) == want), None)
        if member is None:
            # "Yes" against a child labelled "Yes, I am authorized to work in the US". Only when
            # exactly one child starts with the answer: "No" is a prefix of "Not applicable" too,
            # and picking the wrong half of a work-authorization question is worse than leaving it.
            starts = [f for f in members if _plain_label(f.get("label", "")).startswith(want)]
            member = starts[0] if len(starts) == 1 else None
        if member is not None:
            values.setdefault(member["input_id"], values[iid])
            del values[iid]

    filled: dict[str, str] = {}
    put_fields: list[dict] = []
    t = PLAYWRIGHT_ACTION_TIMEOUT
    # Whether a generically-labelled upload ("UPLOAD FILE", "Attach") can only be the résumé.
    sole_file_field = sum(1 for f in fields if f.get("input_type") == "file") == 1
    # The resume is read once per form from `profile.resume_path` (the PDF `save_profile` wrote), so
    # a retried attach re-sends the same bytes and nothing is fetched. No resume on disk -> None,
    # and the upload field stays in `missing`.
    resume_payload = None
    resume_path = Path(user_row.get("resume_path") or "")
    if user_row.get("resume_path") and resume_path.is_file():
        owner = re.sub(r"[^A-Za-z0-9]+", "_", str(user_row.get("full_name") or "")).strip("_")
        resume_payload = {"name": f"{owner}_Resume.pdf" if owner else "resume.pdf",
                          "mimeType": "application/pdf", "buffer": resume_path.read_bytes()}

    for idx, field in enumerate(fields):
        label = field.get("label", "")
        input_id = field.get("input_id")
        input_type = field.get("input_type")
        selector = field.get("selector")
        value = values.get(input_id)

        # Dispatch is retried, bounded by BOTH the attempt count and the time already spent on
        # this field. The two bounds do different jobs: a fast failure (detached node, an
        # intercepted pointer) gets every attempt for almost no cost, while a failure that IS a
        # timeout gets one more try and then stops — measured, fills containing a dispatch
        # failure run 59s against 11.3s for clean ones, because those failures spend the full
        # combobox budget. A flat three attempts would put a single field near two minutes at
        # concurrency 5, re-proving what the first wait already established (ElevenLabs said
        # 'No results' because it wanted a country, not because it was slow).
        field_started = time.perf_counter()
        put = False          # also the state a field that never binds leaves behind
        for attempt in range(FILL_DISPATCH_ATTEMPTS):
            # Start each attempt from the SCRAPED selector: the re-bind below re-resolves it
            # against the live DOM, which is most of the point of trying again.
            selector = field.get('selector')
            # Re-bind by label text, in two cases that reach the same place:
            #
            #   * An SPA re-mounted the control between scrape and fill, dropping the `data-cs-field`
            #     attribute the scrape stamped on it (Ashby does this to its location combobox).
            #   * The scrape NEVER bound it — a <label for> naming an id that never renders, which is
            #     how Ashby ships its certification block. These used to be skipped on sight, even
            #     though this walk searches the DOM by label text and never consults the old selector,
            #     so it was always capable of resolving them.
            #
            # Either way the widget is on screen and the row is valid, so reporting it unfillable
            # without looking is wrong. A row this fails to resolve still falls through to `missing`
            # immediately below, so the no-timeout property that motivated the skip is preserved.
            #
            # `count() == 0` was too weak a test for gone. ElevenLabs' Ashby board leaves the stamped
            # node in the DOM but not actionable, so the count passed, the re-bind never ran, and the
            # combobox click below waited out its whole budget on an element that would never take it
            # — 3 of every 5 Location failures in the corpus, identically at concurrency 2, 3 and 5,
            # which is what ruled out load as the cause. Visibility is only demanded of comboboxes:
            # Ashby backs its yes/no pairs with a permanently invisible checkbox that the dispatcher
            # clicks through its visible <button>, and requiring visibility there would break it.
            stale = bool(selector) and input_type == "select" and not await page.locator(
                selector).first.is_visible()
            if not selector or stale or await page.locator(selector).count() == 0:
                selector = await page.evaluate(
                    """([want, mark]) => {
                         const norm = s => (s || '').trim();
                         const label = [...document.querySelectorAll('label')]
                           .find(l => norm(l.innerText) === norm(want));
                         if (!label) return null;
                         const SEL = 'input,select,textarea,[role="combobox"]';
                         const owned = n => {
                           const o = n.closest('label')
                             || (n.id ? document.querySelector('label[for="' + CSS.escape(n.id) + '"]') : null);
                           return !o || o === label;
                         };
                         // Collect every candidate at the nearest hop that has one, rather than
                         // taking the first: a re-render can leave a hidden twin of the control in
                         // the DOM, and `find` returned whichever came first in document order. On
                         // ElevenLabs that was the dead one — the click landed on it and the
                         // autocomplete was never queried, which read as a slow menu.
                         let found = label.querySelector(SEL) ? [label.querySelector(SEL)] : [];
                         for (let c = label.parentElement, hop = 0; hop < 4 && c && !found.length;
                              hop++) {
                           found = [...c.querySelectorAll(SEL)].filter(owned);
                           c = c.parentElement;
                         }
                         const el = found.find(n => n.offsetParent !== null
                                                    || n.getClientRects().length) || found[0];
                         if (!el) return null;
                         el.setAttribute('data-cs-refill', mark);
                         return '[data-cs-refill="' + mark + '"]';
                       }""", [label, str(idx)])
            # Still nothing: the label walk could not find a control either. Skip the browser entirely
            # rather than probing a dead `[id="..."]` for a guaranteed timeout — unbound is `missing`
            # by definition, and it should cost 0ms.
            if not selector:
                if input_type != "file" and value:
                    logger.debug("field %r (#%s) has no bound control; not filling", label, input_id)
                break

            target = page.locator(selector).first
            # Fill in place, dispatching on input type. Best-effort + per-field isolated: any
            # Playwright error (detached node, ATS-specific widget) is logged and the field counts as
            # missing so a required field never reads as covered when it isn't.
            put = False
            try:
                if input_type == "file":
                    # An upload. Deliberately outside the `values` gate: the LLM is instructed to
                    # return null for file fields, so an upload never has a resolved value — the file
                    # IS the answer. The résumé is automated from the profile; any other upload is
                    # attached only when the user saved a file for that question (`upload_payloads`),
                    # otherwise it stays missing so the report keeps naming what the
                    # application still needs.
                    # Match the id as well as the label: Greenhouse labels BOTH its résumé and its
                    # cover-letter input "Attach", so the label alone identifies neither — the ids
                    # (`resume` / `cover_letter`) are what distinguish them.
                    # "Autofill from resume" is Ashby's own parser-uploader, not the application's
                    # résumé field: feeding it would race our own fills by rewriting the form.
                    # A form whose ONLY file input is labelled "UPLOAD FILE" (Ashby) or "Attach" is
                    # asking for the résumé — there is nothing else it could want. The same generic
                    # label on a form with two file inputs stays ambiguous and is left alone, which
                    # is the Greenhouse résumé/cover-letter pair.
                    norm = normalize_role(f"{label} {input_id or ''}")
                    generic = sole_file_field and re.search(r"upload|attach|file", norm)
                    is_resume_input = (re.search(r"resum|\bcv\b", norm) or generic) and "autofill" not in norm
                    payload = upload_payloads.get(input_id)
                    if is_resume_input and resume_payload:
                        payload = resume_payload
                    if payload:
                        value = payload["name"]
                        # Attach, verify, and re-attach if it did not stick. Worth retrying where
                        # the other fills are not: the bytes were read once above, so an
                        # attempt costs one more attach and no re-read, and the observed failures are the drop-zone still
                        # wiring up its handler rather than anything about the file.
                        for attempt in range(RESUME_ATTACH_ATTEMPTS):
                            put = False
                            try:
                                await target.set_input_files(payload, timeout=t)
                                # A silent no-op would otherwise report as covered, so confirm the
                                # input holds the file. Greenhouse REPLACES the input on attach to
                                # render the filename, so a detached element is evidence the file
                                # landed, not that it failed — only a still-attached, still-empty
                                # input is a real failure.
                                put = True
                                try:
                                    if await target.count() and not await target.evaluate(
                                            "el => el.files.length", timeout=t):
                                        put = False
                                except Exception:  # noqa: BLE001 - swapped out mid-check; it took
                                    pass
                            except Exception as exc:  # noqa: BLE001 - last attempt reports missing
                                if attempt == RESUME_ATTACH_ATTEMPTS - 1:
                                    raise
                                logger.info("upload attach attempt %d/%d failed on %r: %s",
                                            attempt + 1, RESUME_ATTACH_ATTEMPTS, label, exc)
                            if put:
                                break
                            await asyncio.sleep(RESUME_ATTACH_BACKOFF * (attempt + 1))
                elif not value:
                    pass
                elif input_type in ("input", "textarea"):
                    # A number input takes numbers and nothing else. Sierra asks "What is your
                    # average deal size?" as <input type="number">, the stored answer is "Not
                    # applicable", and Playwright refuses with "Cannot type text into
                    # input[type=number]" — after which the field burned all three dispatch
                    # retries proving the same thing. The field is genuinely unanswerable with a
                    # non-numeric value, so fail in one attempt and say why.
                    if (await target.evaluate(
                            "el => (el.getAttribute('type') || '').toLowerCase()", timeout=t)
                            == "number" and not re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", value)):
                        logger.info("field %r is numeric; %r is not a number, leaving it unfilled",
                                    label, value[:40])
                        break
                    await target.fill(value, timeout=t)
                    put = True
                    # Lever's "Current location" is an autocomplete. Its answer is the hidden
                    # `selectedLocation` beside the box, set only when a suggestion is clicked, and
                    # Lever empties the box when it loses focus without one. So typed text alone
                    # read as filled here while the page sent nothing. Type it as
                    # keystrokes so the suggestions load, then click a suggestion only when it IS the
                    # user's place, and count the field as filled only when the hidden value is set.
                    #
                    # "Is the user's place": the suggestion starts with their city, and the part right
                    # after the city is their state, as the code or the full name ("San Francisco, CA"
                    # takes "San Francisco, CA, USA"). The board's top hit is not enough: for
                    # "Portland, ME" it can be Portland, Oregon. Only the part after the city counts,
                    # and a code must match exactly in capitals, because "New York" is both a city and
                    # a state: for "New York, NY" Lever lists New York, Lincolnshire, United Kingdom
                    # first (review round 2). A value with no state has nothing to tell two Portlands
                    # apart, so it is left for Needs you, and so is a list with no matching row.
                    # The list is re-read until a matching row appears, so rows still showing an
                    # earlier prefix's results are passed over. The row is clicked by its exact text,
                    # and what the box then holds is checked again: the list can change between the
                    # read and the click (review round 3), and a wrong place must never stay filled.
                    chosen_location = target.locator("xpath=..").locator('input[name="selectedLocation"]')
                    if await chosen_location.count():
                        put = False
                        city, _, rest = value.partition(",")
                        city = city.strip()
                        region = rest.split(",")[0].strip()
                        # A US state code ("CA") is matched exactly, and its full name in any case.
                        # Anything else ("California", "Ontario") is a name, plus its code if it has one.
                        if region.upper() in us_states.US_STATES:
                            state_codes = {region.upper()}
                            state_names = {us_states.US_STATES[region.upper()].lower()}
                        else:
                            state_names = {region.lower()}
                            state_codes = {code for code, name in us_states.US_STATES.items()
                                           if name.lower() == region.lower()}
                        if not (city and region):
                            logger.info("location %r names no state; leaving field %r for the user",
                                        value[:40], label)
                            break
                        after_city = re.compile(rf"\s*{re.escape(city)}\s*,\s*([^,]+)", re.I)

                        def names_users_place(text: str) -> bool:
                            """Whether `text` is the user's city followed by their state."""
                            found = after_city.match(text or "")
                            if not found:
                                return False
                            state_part = found.group(1).strip()
                            return state_part in state_codes or state_part.lower() in state_names

                        await target.fill("", timeout=t)
                        await target.press_sequentially(value, delay=15, timeout=t)
                        rows = target.locator("xpath=..").locator(".dropdown-results > *")
                        match_text = None
                        deadline = time.perf_counter() + COMBOBOX_QUERY_TIMEOUT_MS / 1000
                        while match_text is None and time.perf_counter() < deadline:
                            texts = await rows.all_inner_texts()
                            match_text = next((text for text in texts if names_users_place(text)), None)
                            if match_text is None:
                                await asyncio.sleep(0.2)
                        if match_text is None:
                            # Retrying cannot change what the board suggests for the same text.
                            logger.info("no location suggestion matches %r on field %r", value[:40], label)
                            break
                        exact_row = rows.filter(has_text=re.compile(rf"^\s*{re.escape(match_text.strip())}\s*$"))
                        await exact_row.first.click(timeout=t)
                        shown = await target.input_value(timeout=t)
                        stored = await chosen_location.input_value(timeout=t)
                        if stored and names_users_place(shown):
                            put = True
                        else:
                            logger.warning("location field %r took %r, not %r; clearing it",
                                           label, shown[:60], value[:40])
                            await target.fill("", timeout=t)
                            await chosen_location.evaluate("el => { el.value = ''; }", timeout=t)
                            break
                elif input_type == "select":
                    # Native <select> matches by visible label. Everything else is an ARIA combobox
                    # (Ashby's location, all 8 of Greenhouse's) — those were previously skipped in
                    # silence, which is most of why required coverage sat below half.
                    want_decline = value == _EEO_DECLINE_INTENT
                    # An authorized consent whose options were unreadable at match time; the
                    # affirmative is chosen from the live menu instead (see _CONSENT_AFFIRM_INTENT).
                    want_affirm = value == _CONSENT_AFFIRM_INTENT
                    if await target.evaluate("el => el.tagName.toLowerCase()", timeout=t) == "select":
                        if want_decline or want_affirm:
                            native = await target.evaluate(
                                "el => [...el.options].map(o => o.text)", timeout=t)
                            value = (next((o for o in native if _EEO_DECLINE.search(o)), None)
                                     if want_decline else _option_for_answer(_AFFIRM, native))
                        if value:
                            await target.select_option(label=value, timeout=t)
                            put = True
                    else:
                        await target.click(timeout=t)          # opens the listbox
                        typed = False
                        try:
                            # Real keystrokes, not `fill`: react-select (Greenhouse) only queries its
                            # option source on key events and ignores a value set wholesale. Skipped
                            # for a decline, whose text we don't know until the menu is open.
                            if not (want_decline or want_affirm):
                                await target.press_sequentially(value, delay=15, timeout=t)
                                typed = True
                        except Exception:  # noqa: BLE001 - a div/button trigger; the click sufficed
                            pass
                        if typed:
                            # Confirm the keystrokes actually landed. A control that takes them
                            # silently and keeps an empty value is a dead node the re-bind picked in
                            # place of the live one: the autocomplete is never queried, no menu ever
                            # opens, and the wait below then spends its full budget proving it. Fail
                            # in a second instead of thirty, and say which it was.
                            try:
                                if not (await target.input_value(timeout=t)).strip():
                                    raise RuntimeError("combobox took no text; likely a stale node")
                            except Exception as exc:  # noqa: BLE001
                                if "stale node" in str(exc):
                                    await page.keyboard.press("Escape")
                                    raise
                                # Not an <input> at all (a div trigger) — nothing to verify.
                        # Resolve THIS combobox's own listbox. A bare `[role="option"]` sweep matches
                        # every open menu on the page: on Greenhouse it returned the 254 options of
                        # the phone country-code dropdown, so the location field would have been
                        # answered "Afghanistan+93". Never guess a menu — leave the field unfilled.
                        listbox_sel = await target.evaluate(
                            """(el, mark) => {
                                 const id = el.getAttribute('aria-controls')
                                   || el.getAttribute('aria-owns');
                                 let box = id ? document.getElementById(id) : null;
                                 for (let c = el.parentElement, hop = 0; hop < 5 && c && !box; hop++) {
                                   box = c.querySelector('[role="listbox"]');
                                   c = c.parentElement;
                                 }
                                 if (!box) return null;
                                 if (box.id) return '[id="' + box.id + '"]';
                                 box.setAttribute('data-cs-listbox', mark);
                                 return '[data-cs-listbox="' + mark + '"]';
                               }""", str(idx), timeout=t)
                        if not listbox_sel:
                            await page.keyboard.press("Escape")
                            raise RuntimeError("no listbox for combobox")
                        options = page.locator(f'{listbox_sel} [role="option"]')
                        # Autocompletes (Ashby's and Greenhouse's location fields) fetch their options
                        # over the network once text is typed, so the listbox is empty for a beat
                        # after typing — reading it immediately found nothing and skipped the field in
                        # silence. Its own budget here: this wait is on a round trip, not on a
                        # mis-bound element, so it is the one place where waiting longer pays.
                        #
                        # `attached`, not `visible`: under load the options exist in the DOM well
                        # before the menu finishes painting, and waiting on paint timed the field out
                        # on pages where the answer was already sitting there. The click below does
                        # its own actionability wait, so nothing is clicked before it can be.
                        # Query ladder, narrowest first. The same Ashby field is configured per board:
                        # Harvey's Location matches cities ("Farmington, CT" resolves), ElevenLabs'
                        # matches COUNTRIES ONLY — "San Francisco" returns a visible menu reading "No
                        # results", and typing a city there could never work. That one board's
                        # Location was 7 of 34 pooled failures, misread for a long time as a slow
                        # autocomplete because a menu that says "no results" and a menu that has not
                        # loaded look identical through `wait_for`.
                        #
                        # The last rung is the general form of that fix: clear the query entirely
                        # and read whatever the widget offers unfiltered, then pick from ITS
                        # vocabulary below. A typed query that matches nothing is not a slow
                        # network, it is a value the widget does not stock — Replit's "How did you
                        # hear about this role?" spent 30s proving that about "Company website".
                        # Clearing is what a person does when the box says no results.
                        terms = [value]
                        if typed and "," in value:
                            terms.append(value.split(",")[0].strip())
                        if typed and _LOCATION_FIELD.search(label) and user_row.get("country"):
                            terms.append(user_row["country"])
                        if typed:
                            terms.append("")
                        wanted, cleared = value, False
                        for i, term in enumerate(terms):
                            last = i == len(terms) - 1
                            if i:
                                await target.fill("")
                                if term:
                                    await target.press_sequentially(term, delay=15, timeout=t)
                                # What we typed is what we are claiming, so the exact-match filter
                                # below has to look for THIS term. Falling back to the country is
                                # still true of the candidate — it is just the coarsest granularity
                                # the form is willing to accept.
                                value, cleared = term, not term
                            try:
                                await options.first.wait_for(
                                    state="attached",
                                    timeout=COMBOBOX_OPTION_TIMEOUT_MS if last
                                    else COMBOBOX_QUERY_TIMEOUT_MS)
                                break
                            except Exception:  # noqa: BLE001 - try a broader term, or give up
                                if last:
                                    raise
                        if want_decline:
                            # Only the form's OWN wording. If this menu offers no decline, answer
                            # nothing — a demographic question is never answered by guessing, and
                            # falling back to "first option" would pick a real characteristic.
                            chosen = options.filter(has_text=_EEO_DECLINE)
                            value = (await chosen.first.inner_text()).strip() if await chosen.count() \
                                else None
                        elif want_affirm:
                            # The affirmative in the form's own words, via the same resolver every
                            # other option path uses — so "Yes, I consent" is found and "No, I do
                            # not consent" cannot be. A menu offering neither affirms nothing.
                            texts = [x.strip() for x in await options.all_inner_texts()]
                            value = _option_for_answer(_AFFIRM, texts)
                            chosen = options.filter(
                                has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
                            ) if value else options.filter(has_text=re.compile(r"^\x00$"))
                        elif cleared:
                            # The query was emptied, so the menu is the widget's WHOLE vocabulary
                            # and `.first` would be an arbitrary answer. Choose from its texts with
                            # the same resolver the select path uses — verbatim, punctuation, lone
                            # negative, numeric band — and for a referral question prefer the
                            # company's own site, then LinkedIn. No match means no click.
                            texts = [x.strip() for x in await options.all_inner_texts()]
                            pick = _option_for_answer(wanted, texts)
                            if pick is None and user_row.get("referral_source") \
                                    and _REFERRAL_FIELD.search(label):
                                pick = (next((o for o in texts
                                              if _REFERRAL_COMPANY_SITE.search(o)), None)
                                        or next((o for o in texts
                                                 if _REFERRAL_LINKEDIN.search(o)), None))
                            value = pick
                            chosen = options.filter(
                                has_text=re.compile(rf"^\s*{re.escape(pick)}\s*$", re.I)
                            ) if pick else options.filter(has_text=re.compile(r"^\x00$"))
                        else:
                            exact = options.filter(
                                has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I))
                            # The fallback to the unfiltered list is only defensible BECAUSE the value
                            # was typed: the widget has filtered its own options, so `.first` means "the
                            # widget's best match for what I asked for". When `press_sequentially` threw
                            # — a div/button trigger that cannot take keystrokes — nothing was typed,
                            # every option is still on offer, and `.first` would click whatever happens
                            # to be at the top of the menu. That is the "Afghanistan+93" failure the
                            # listbox resolution above exists to prevent, reached by another route, so
                            # an untyped combobox demands an exact match or stays unfilled.
                            chosen = exact if (await exact.count() or not typed) else options
                        if await chosen.count():
                            await chosen.first.click(timeout=t)
                            put = True
                        # Greenhouse leaves a listbox open over the form, and its options then
                        # intercept the pointer for every control underneath — one unclosed dropdown
                        # cascades into failures on unrelated fields further down.
                        await page.keyboard.press("Escape")
                elif input_type in ("checkbox", "radio"):
                    # Only ever reached when a value is present — a False boolean / consent box the
                    # user didn't authorize never lands in `values`, so it stays unchecked.
                    #
                    # Ashby renders a pick-one as a PERMANENTLY invisible <input type="checkbox"
                    # tabindex="-1"> paired with visible Yes/No <button>s. `check()` waits out its
                    # timeout on an element that will never be visible, so click what the user would
                    # click and fall back to forcing the hidden input.
                    # A grouped pick-one is answered by the MEMBER whose own label is the answer, not
                    # by this row's control. Resolving it through `group_key` beats both paths below:
                    # the ancestor walk stops the moment a parent holds more than one control, which
                    # is precisely the shape of every radio group, and `check()` on the visible input
                    # is a real pointer click that a cookie banner or an hCaptcha iframe can and does
                    # intercept — one Lever board burned the full action timeout per option that way.
                    # A DOM click cannot be intercepted, and it lands on the element the user would
                    # actually press.
                    group_key = field.get("group_key")
                    clicked = False
                    if group_key:
                        clicked = await page.evaluate(
                            """([name, want]) => {
                                 const norm = s => (s || '').trim().toLowerCase();
                                 const sel = '[name="' + name.replace(/["\\\\]/g, '\\\\$&') + '"]';
                                 const owner = el => el.closest('label') || (el.id
                                   ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                                   : null);
                                 for (const m of document.querySelectorAll(sel)) {
                                   const o = owner(m);
                                   if (!o || norm(o.innerText) !== norm(want)) continue;
                                   (o.querySelector('button') || o).click();
                                   // The member IS the option here, so it must end up selected —
                                   // unlike the single-checkbox-backed pair handled below, where
                                   // answering "No" correctly leaves the box unchecked.
                                   return !!m.checked;
                                 }
                                 return false;
                               }""", [group_key, value])
                    if clicked:
                        put = True
                    elif await target.is_visible():
                        await target.check(timeout=t)
                        put = True
                    else:
                        clicked = await target.evaluate(
                            """(el, want) => {
                                 const norm = s => (s || '').trim().toLowerCase();
                                 let c = el.parentElement;
                                 for (let hop = 0; hop < 5 && c; hop++) {
                                   // Stop the moment the ancestor holds more than our own control:
                                   // on Ashby every yes/no question's buttons share a grandparent, so
                                   // a wider search clicks a DIFFERENT question's "Yes".
                                   if (c.querySelectorAll(
                                         'input[type=checkbox],input[type=radio]').length > 1) break;
                                   const hit = [...c.querySelectorAll('button,label')]
                                     .find(n => norm(n.innerText) === norm(want));
                                   if (hit) { hit.click(); return true; }
                                   c = c.parentElement;
                                 }
                                 return false;
                               }""", value, timeout=t)
                        # Verify against the answer we gave, not against "is it ticked". Ashby backs a
                        # yes/no pair with ONE checkbox, so answering "No" correctly leaves it
                        # UNCHECKED — treating checked-ness as success reported every "No" as a
                        # failure and left the question in `missing`.
                        expected = value.strip().lower() not in ("no", "false", "0")
                        put = bool(clicked) and await target.is_checked(timeout=t) == expected
            except Exception as exc:  # noqa: BLE001 - per-field isolation, never abort the form
                logger.warning("fill failed for field %r (#%s, %s): %s",
                               label, input_id, input_type, exc)
            if put:
                break
            # Nothing to dispatch means nothing to retry. Without this the loop re-walked the DOM
            # three times for every field the resolvers left empty — cheap (~24ms each) but it
            # buries the real retries in the log, and a retry that cannot possibly change the
            # outcome is not a retry.
            if not value and input_type != "file":
                break
            spent_ms = (time.perf_counter() - field_started) * 1000
            if attempt == FILL_DISPATCH_ATTEMPTS - 1 or spent_ms > FILL_FIELD_BUDGET_MS:
                break
            # Logged, not silent: a board that needs a retry on every single fill is a bug to
            # fix, and a retry that quietly rescues it would hide exactly that.
            logger.info('retrying field %r (#%s, %s): attempt %d after %.0fms',
                        label, input_id, input_type, attempt + 2, spent_ms)

        if put:
            filled[label] = value
            put_fields.append(field)

    # Coverage is tallied per QUESTION, not per control. Mutually exclusive options share a
    # `group_key`, and counting each one separately made a correctly answered pick-one report as
    # several unmet requirements (`missing=['Yes','No',...]`) so `ok` could never be true.
    #
    # `group_key` alone does not carry that all the way. Ashby splits a question into a heading
    # row — which is the row `required_field` lands on — and option rows holding the real
    # controls, and the two key differently, so ticking the control could never satisfy the
    # requirement it answers. `group_label` names the question a row belongs to; resolve it to
    # that question's own key and the halves become one group. Consulted ONLY via `group_label`,
    # never for a row's own key, so two rows sharing a label (every form's second "Decline to
    # self-identify") cannot collapse into each other.
    keys = [field.get("group_key") or ("id", field.get("input_id"), field.get("label"))
            for field in fields]
    question_key = {field.get("label"): key for field, key in zip(fields, keys)}
    put_ids = {id(field) for field in put_fields}

    groups: dict = {}
    for field, key in zip(fields, keys):
        key = question_key.get(field.get("group_label")) or key
        g = groups.setdefault(key, {"required": False, "filled": False,
                                    "labels": [], "title": None, "why": [], "rows": []})
        g["rows"].append(field)
        g["required"] = g["required"] or bool(field.get("required_field"))
        g["title"] = g["title"] or field.get("group_label")
        g["labels"].append(field.get("label", ""))
        # The three facts that distinguish every reason a field can go unfilled. `missing` alone
        # cannot: "the scrape never bound a control", "no value was resolved for it" and "the click
        # failed" are the same entry in that list, and they need completely different fixes.
        g["why"].append({"input_id": field.get("input_id"),
                         "input_type": field.get("input_type"),
                         # Whether the SCRAPE bound a control. False here plus a filled field means
                         # the label-text re-bind rescued it; False plus unfilled means neither
                         # could find it.
                         "scrape_bound": bool(field.get("selector")),
                         "had_value": field.get("input_id") in values,
                         # An unanswered consent field has three indistinguishable causes: the
                         # authority flag is off, the menu was unreadable at match time, or the
                         # options are readable but none is recognised as affirmative. The tier
                         # plus the options separate all three.
                         "consent_tier": _consent_tier(_plain_label(field.get("label", ""))),
                         "options": field.get("options") or []})
        if id(field) in put_ids:
            g["filled"] = True

    # A deferred decline only learns the form's actual wording at click time — record what was
    # really selected, so the audit names the option the employer will see.
    for entry in declined:
        if entry["option"] == _EEO_DECLINE_INTENT:
            entry["option"] = filled.get(entry["label"]) or "(no decline option offered)"
    # Report what LANDED, not what was intended. `fill_form_fields` already drops a decline a
    # disclosed value overrode; this drops the ones whose control was never clicked. Without it an
    # unfillable EEO question reads as "we declined on your behalf" in an audit whose whole purpose
    # is to say what the employer was actually told.
    put_input_ids = {field.get("input_id") for field in put_fields}
    declined = [d for d in declined if d["input_id"] in put_input_ids]

    required_total = sum(1 for g in groups.values() if g["required"])
    required_filled = sum(1 for g in groups.values() if g["required"] and g["filled"])
    unmet = [g for g in groups.values() if g["required"] and not g["filled"]]
    missing = [g["title"] or " / ".join(dict.fromkeys(g["labels"])) for g in unmet]

    # The control each unmet question was asked with, parallel to `missing`, so the tray can
    # show a text box, a picker, checkboxes or an upload button and refuse an answer the form would
    # not take. From the scrape rows of the question: any upload input is `file`; several checkbox
    # rows are `multi` and radio rows `single`, their options the rows' own labels; a select with
    # options is `single`; a select with none (a typed autocomplete), an input and a textarea are
    # `text`, limited by the form's own `maxlength`. A lone checkbox is `single` over its scraped
    # `options` when it has any (Ashby's Yes/No buttons over one hidden box), else over its label
    # (a true acknowledgement box).
    missing_shapes = []
    for g in unmet:
        rows = g["rows"]
        checkboxes = [r for r in rows if r.get("input_type") == "checkbox"]
        radios = [r for r in rows if r.get("input_type") == "radio"]
        selects = [r for r in rows if r.get("input_type") == "select" and r.get("options")]
        shape = {"kind": "text", "options": [], "max_length": None}
        if any(r.get("input_type") == "file" for r in rows):
            shape["kind"] = "file"
        elif len(checkboxes) > 1:
            shape.update(kind="multi", options=list(dict.fromkeys(r["label"] for r in checkboxes)))
        elif radios:
            shape.update(kind="single", options=list(dict.fromkeys(r["label"] for r in radios)))
        elif selects:
            shape.update(kind="single", options=list(selects[0]["options"]))
        elif checkboxes:
            shape.update(kind="single", options=checkboxes[0].get("options") or [checkboxes[0]["label"]])
        else:
            shape["max_length"] = next((r["max_length"] for r in rows if r.get("max_length")), None)
        missing_shapes.append(shape)

    return {
        "filled": filled,
        "required_total": required_total,
        "required_filled": required_filled,
        "missing": missing,
        "missing_shapes": missing_shapes,
        # Parallel to `missing`, one entry per unmet question, truncated label so a multi-sentence
        # legal attestation stays readable next to the facts that explain it.
        "missing_detail": [{"label": (m[:60] + "…") if len(m) > 60 else m, "fields": g["why"]}
                           for m, g in zip(missing, unmet)],
        "ok": not missing,
        "consented": consented,
        "declined": declined,
    }


# Per-ATS submit selectors, tried first because a board that names its own button is not worth
# guessing at. Keyed by `job_applications.ats` / `listings.source` ('greenhouse', 'lever', 'ashby').
_ATS_SUBMIT_SELECTORS = {
    "greenhouse": ("#submit_app", "#application_form button[type=submit]",
                   "button#submit_app"),
    "lever": ('button[data-qa="btn-submit"]', ".application-page button[type=submit]"),
    "ashby": ('[data-testid="application-form-submit-button"]',
              'button[type=submit][class*="ashby"]'),
}

# Text of last resort. Anchored and case-insensitive: a substring match on "submit" also matches
# "Submit another application", which is a different button on the page we land on AFTER succeeding.
_SUBMIT_TEXT = re.compile(r"^\s*(submit application|submit|apply now|apply)\s*$", re.I)
_NOT_SUBMIT_TEXT = re.compile(r"another|save|back|cancel|draft|previous|withdraw", re.I)

# "It worked": the ATS rendered its own acknowledgement in response to our POST.
_SUCCESS_TEXT = re.compile(
    r"thank you for applying|application (?:has been )?(?:submitted|received)"
    r"|thanks for applying|we(?:'| ha)ve received your application"
    r"|your application (?:has been|was) (?:submitted|sent|received)", re.I)
# "It did not work, and the form is telling us why."
_VALIDATION_SELECTORS = ("[role=alert]", ".field_with_errors", ".error", "[aria-invalid=true]",
                         ".field-error", "[data-testid*='error']")

# "Look in your email": the ATS took the form and now wants a mailed code or link before it counts.
# Like _SUCCESS_TEXT, only a phrase that was not on the page before the click counts.
_EMAIL_LINK_TEXT = re.compile(
    r"check your (?:email|inbox)|confirm your (?:email|application)|(?:verification|confirmation) link", re.I)
# A field that asks for the emailed code, matched on its label, aria-label or placeholder.
_CODE_PROMPT_TEXT = r"verification code|security code|one-time|enter the code (?:we|sent|from|emailed)"

# What a confirmation page's URL path looks like, per ATS, beside `_ATS_SUBMIT_SELECTORS`: it is
# the one signal that proves the ATS accepted the form. Greenhouse as the fixtures assume it; Lever's and
# Ashby's are to be filled in from real confirmation pages, and until then a URL change on those boards that shows
# no acknowledgement text fails closed to `verify_submit`.
_CONFIRM_URL = {"greenhouse": re.compile(r"/jobs/\d+/confirmation")}

# Visible captcha widgets, by vendor. The reCAPTCHA anchor frame inside `.grecaptcha-badge` is the
# invisible variant's badge, which is always on screen but asks the user nothing until the click.
_CAPTCHA_WIDGETS = (
    ("recaptcha", 'iframe[src*="recaptcha/api2/anchor"]:not([src*="size=invisible"])'),
    ("recaptcha", 'iframe[src*="recaptcha/enterprise/anchor"]:not([src*="size=invisible"])'),
    ("hcaptcha", 'iframe[src*="hcaptcha.com"]'),
    ("turnstile", 'iframe[src*="challenges.cloudflare.com"]'),
)
# The popup a captcha shows after the click. reCAPTCHA keeps its challenge frame in the DOM, hidden, from
# load, so only a VISIBLE one counts. Turnstile is not here: it shows its challenge inside the widget's own
# frame, which a user who solved it leaves on screen, so it would read as a popup that never closed. A
# Turnstile that only appears after the click ends as `unknown`, `verify_submit`, which fails closed.
_CAPTCHA_CHALLENGES = (
    'iframe[src*="recaptcha/api2/bframe"]', 'iframe[src*="recaptcha/enterprise/bframe"]',
    'iframe[title*="recaptcha challenge" i]', 'iframe[src*="hcaptcha.com"][src*="frame=challenge"]',
)
# How often the post-click wait looks at the page again.
OUTCOME_POLL_S = 0.25


# Profile columns stored as JSON text (`local_profile.save_profile`); the filler reads Python values.
_PROFILE_JSON_COLUMNS = ("target_roles", "portfolio_links", "skills", "education", "work_experience",
                         "certifications", "languages", "projects", "work_preferences")


def profile_for_fill(row) -> dict:
    """The `profile` row as the filler reads it: a dict with its JSON text columns decoded.

    Called by `local_apply` before every fill. The editor stores `work_experience` entries with
    `end_month`, `end_year` and `current`; the matcher's "current job" rule reads that `current`
    flag. A column that is NULL or not valid JSON decodes to None, which every reader treats as
    empty.
    """
    profile = dict(row)
    for column in _PROFILE_JSON_COLUMNS:
        raw = profile.get(column)
        if isinstance(raw, str):
            try:
                profile[column] = json.loads(raw)
            except ValueError:
                profile[column] = None
    return profile


async def grab_page_fields(page) -> list[dict]:
    """Read the form fields from a page: one `_FIELD_SCRAPE_JS` evaluate plus a click per combobox.

    Called by `local_apply` for the real run and `fill_check`. Copied from the hosted scraper
    without the `company_job_info` upsert. Each row carries a `selector` valid only for THIS page
    instance (it may name an attribute the scrape just stamped). Returns [] for a page with no
    form, which `local_apply` reads as `unsupported_form`.
    """
    # SPA forms (Ashby, etc.) render their fields client-side AFTER domcontentloaded, so wait for
    # at least one <label> to attach before reading. On timeout, proceed - a form-less page (a
    # job-not-found shell) should return [], not throw.
    try:
        await page.wait_for_selector("label", state="attached", timeout=FORM_RENDER_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - nothing rendered; the evaluate below returns []
        logger.info("no <label> rendered on %s within %dms", page.url, FORM_RENDER_TIMEOUT_MS)

    rows = []
    seen = set()
    for descriptor in await page.evaluate(_FIELD_SCRAPE_JS):
        row_key = (normalize_role(descriptor["label"]), descriptor["input_id"])
        if row_key in seen:
            continue
        seen.add(row_key)

        # Aria comboboxes only render their listbox once opened, which no evaluate can do - so the
        # JS flags them and we click just those, back out here on the live page.
        if descriptor.pop("needs_click", False) and descriptor.get("selector"):
            try:
                element = page.locator(descriptor["selector"]).first
                await element.click(timeout=PLAYWRIGHT_ACTION_TIMEOUT)
                # Resolve THIS widget's own menu after the click: react-select (Greenhouse) only
                # sets `aria-controls` while the menu is open, and a bare `[role="option"]` sweep
                # would match every open menu on the page (the phone country-code list).
                listbox_id = descriptor.get("listbox_id")
                listbox_sel = f'[id="{listbox_id}"]' if listbox_id else None
                try:
                    listbox_sel = await element.evaluate(
                        """el => {
                             const id = el.getAttribute('aria-controls')
                               || el.getAttribute('aria-owns');
                             let box = id ? document.getElementById(id) : null;
                             for (let c = el.parentElement, hop = 0; hop < 5 && c && !box;
                                  hop++) {
                               box = c.querySelector('[role="listbox"]');
                               c = c.parentElement;
                             }
                             return box && box.id ? '[id="' + box.id + '"]' : null;
                           }""") or listbox_sel
                except Exception:  # noqa: BLE001 - keep the pre-click id rather than lose it
                    pass
                if listbox_sel:
                    option_locator = page.locator(f'{listbox_sel} [role="option"]')
                    descriptor["options"] = [t.strip() for t in await option_locator.all_inner_texts()
                                             if t.strip()]
            except Exception as exc:  # noqa: BLE001 - one dead widget must not kill the scrape
                logger.warning("could not open combobox %r on %s: %s",
                               descriptor["label"], page.url, exc)
            # Close it whatever happened: an open listbox overlays the controls below it, so the
            # NEXT combobox's click is intercepted and burns the full action timeout.
            try:
                await page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001 - closing is best-effort, never fatal
                pass
        descriptor.pop("listbox_id", None)
        rows.append(descriptor)

    # input_id keys the value map, so two questions sharing one is a silent last-write-wins
    # overwrite - surface it.
    keyed = [r["input_id"] for r in rows if r["input_id"]]
    if len(keyed) != len(set(keyed)):
        logger.warning("duplicate input_id on %s: %s", page.url,
                       sorted({k for k in keyed if keyed.count(k) > 1}))
    return rows


async def captcha_widget(page) -> str | None:
    """The vendor of a captcha widget the user can see on the form (`recaptcha`, `hcaptcha`, `turnstile`), else None.

    Called by `submit_form` before the fence. A visible widget cannot be passed by clicking
    Submit, and the app never solves one, so the row is handed to the user instead. An
    invisible reCAPTCHA is not found here: its badge frame carries `size=invisible` and its challenge
    only appears after the click, which `challenge_shown` catches.
    """
    for vendor, selector in _CAPTCHA_WIDGETS:
        widgets = page.locator(selector)
        for index in range(await widgets.count()):
            if await widgets.nth(index).is_visible():
                return vendor
    return None


async def challenge_shown(page) -> bool:
    """Whether a captcha challenge popup is on screen; checked after the click."""
    for selector in _CAPTCHA_CHALLENGES:
        frames = page.locator(selector)
        for index in range(await frames.count()):
            if await frames.nth(index).is_visible():
                return True
    return False


async def code_prompt(page) -> bool:
    """Whether the page asks for an emailed code in a field that was not part of the form we filled.

    Called after the click by `wait_for_outcome`. A visible text-like input whose label,
    `aria-label` or placeholder says "verification code", "security code", "one-time" or "enter the
    code". Fields `grab_page_fields` stamped (`data-cs-field`) are the form we already answered, and a
    field this function already reported (`data-cs-code`) is the same prompt, so neither counts. The
    match is stamped `data-cs-code`, which is how `local_handoff` finds the box to type into.
    """
    return await page.evaluate(
        """pattern => {
             const wanted = new RegExp(pattern, 'i');
             for (const el of document.querySelectorAll('input')) {
               if (['hidden', 'checkbox', 'radio', 'file', 'submit', 'button'].includes(el.type)) continue;
               if (el.hasAttribute('data-cs-field') || el.hasAttribute('data-cs-code')) continue;
               if (!el.offsetParent && getComputedStyle(el).position !== 'fixed') continue;
               const names = [el.getAttribute('aria-label'), el.placeholder];
               for (const label of el.labels || []) names.push(label.textContent);
               if (names.some(name => name && wanted.test(name))) {
                 el.setAttribute('data-cs-code', '1');
                 return true;
               }
             }
             return false;
           }""", _CODE_PROMPT_TEXT)


async def find_submit_control(page, ats: str):
    """The one submit control on the page as `(locator, None)`, or `(None, reason)` when there is not exactly one.

    Called by `submit_form` and by `local_handoff` to press the confirm button of a code step. The
    ladder: the board's own selectors, then the submit button of the form `grab_page_fields` stamped,
    then a button whose text is "Submit"/"Apply". First RUNG that matches wins, not first element:
    falling through to a broader rung after a narrower one matched would re-introduce the ambiguity
    the narrow rung resolved. Two plausible controls is refused (`submit_ambiguous`): refusing costs
    one application, guessing wrong sends the user's name to an employer through the wrong button.
    """
    candidates = None
    for selector in _ATS_SUBMIT_SELECTORS.get(ats, ()):
        located = page.locator(selector)
        if await located.count():
            candidates = located
            break

    if candidates is None:
        # Scoped to the scraped form subtree: `grab_page_fields` stamps `data-cs-field` on the live
        # DOM, which matters because ATS pages routinely carry a second form (newsletter signup,
        # job search) with its own submit button.
        scoped = page.locator("form:has([data-cs-field]) button[type=submit], "
                              "form:has([data-cs-field]) input[type=submit]")
        if await scoped.count():
            candidates = scoped

    if candidates is None:
        buttons = page.locator("button, input[type=submit], input[type=button]")
        matched = []
        for i in range(min(await buttons.count(), 40)):
            node = buttons.nth(i)
            label = (await node.inner_text() or "").strip() or (
                await node.get_attribute("value") or "")
            if _SUBMIT_TEXT.match(label) and not _NOT_SUBMIT_TEXT.search(label):
                matched.append(node)
        if len(matched) > 1:
            return None, "submit_ambiguous"
        if not matched:
            return None, "submit_control_not_found"
        return matched[0], None

    if await candidates.count() > 1:
        return None, "submit_ambiguous"
    return candidates.first, None


async def read_body(page) -> str | None:
    """The page's visible text, or None when it cannot be read (torn down, or mid-navigation)."""
    try:
        return await page.inner_text("body", timeout=PLAYWRIGHT_ACTION_TIMEOUT)
    except Exception:  # noqa: BLE001 - without a baseline the text signals cannot be trusted
        return None


async def wait_for_outcome(page, ats: str, before_url: str, body_before: str | None, *,
                           timeout_ms: int | None = None, watch_challenge: bool = True) -> dict:
    """Poll the page after a click until it proves what happened, or `timeout_ms` passes.

    Called by `submit_form` (the 45 s window), by `local_handoff` for the long wait after the user
    solves a captcha popup (`watch_challenge=False`, the popup is the thing being solved) and for the
    wait after a code is typed. `before_url` and `body_before` are read just before the click: only
    text that was not already on the page counts, so a job description that says "once we have
    received your application" cannot read as an acknowledgement.

    Returns `{"outcome": ..., "reason": ...}`, the first of these to hold, in this order (the page
    checks come first because a confirmation URL can still carry a code or link step):
    - `challenge`: a captcha popup is visible;
    - `code`: a new field asks for an emailed code;
    - `email_link`: new text tells the user to open a link in their email;
    - `submitted`: the URL changed to the board's confirmation page, or new acknowledgement text;
    and, only once the window is over:
    - `unknown`: the URL changed to something unrecognised, or nothing proves what happened;
    - `rejected_by_form`: validation markup is visible with the form still mounted.
    A URL change alone is never `submitted`: an unrecognised page may be a code step still loading.
    """
    timeout_ms = SUBMIT_CONFIRM_TIMEOUT_MS if timeout_ms is None else timeout_ms
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    confirm_url = _CONFIRM_URL.get(ats)
    while True:
        try:
            if watch_challenge and await challenge_shown(page):
                return {"outcome": "challenge", "reason": "captcha_challenge"}
            if await code_prompt(page):
                return {"outcome": "code", "reason": "code_prompt"}
            body = await read_body(page)
            if body_before is not None and body is not None:
                before_links = {m.group(0).lower() for m in _EMAIL_LINK_TEXT.finditer(body_before)}
                now_links = {m.group(0).lower() for m in _EMAIL_LINK_TEXT.finditer(body)}
                if now_links - before_links:
                    return {"outcome": "email_link", "reason": "email_link"}
            # The URL must have CHANGED to the confirmation page: after a code is typed, `before_url` is the
            # code step, which may itself sit on the confirmation URL.
            if (confirm_url is not None and page.url != before_url
                    and confirm_url.search(urllib.parse.urlsplit(page.url).path)):
                return {"outcome": "submitted", "reason": None}
            # With no baseline (the pre-click read failed) the text signal is skipped: it fails closed.
            if body_before is not None and body is not None:
                before_phrases = {m.group(0).lower() for m in _SUCCESS_TEXT.finditer(body_before)}
                now_phrases = {m.group(0).lower() for m in _SUCCESS_TEXT.finditer(body)}
                if now_phrases - before_phrases:
                    return {"outcome": "submitted", "reason": None}
        except Exception:  # noqa: BLE001 - a page mid-navigation refuses to be read; look again next lap
            if page.is_closed():
                raise    # the window is gone (a user closing the headed one): the caller decides
        if loop.time() >= deadline:
            break
        await asyncio.sleep(OUTCOME_POLL_S)

    if page.url != before_url:
        return {"outcome": "unknown", "reason": "unrecognized_page_after_submit"}
    # A validation banner only means rejection if the form is STILL THERE. A success page can
    # legitimately contain `.error` markup left over in the DOM.
    if await page.locator("[data-cs-field]").count():
        for selector in _VALIDATION_SELECTORS:
            node = page.locator(selector)
            if await node.count() and await node.first.is_visible():
                return {"outcome": "rejected_by_form", "reason": "form_rejected"}
    # We clicked and cannot prove what happened. Never recorded as applied.
    return {"outcome": "unknown", "reason": "submit_outcome_unknown"}


async def submit_form(page, fields: list[dict], ats: str, mark_attempted, *, allow_captcha: bool = False) -> dict:
    """Locate the submit control, fence, click it, and classify what the ATS did about it.

    Called by `local_apply.apply_one` only when every required field is filled, and by
    `local_handoff` once the user has solved a captcha (`allow_captcha=True`). `mark_attempted()`
    is the caller's compare-and-set that moves the row to `submitting` and stamps
    `submit_attempted_at`; it runs AFTER the control resolved to exactly one element and
    immediately BEFORE the click, the only point at which "immediately before the click" is true,
    and returns False when the row has moved, in which case nothing is sent.

    Returns `{"outcome": ..., "reason": ...}` where outcome is one of
    - `submitted`, `email_link`, `code`, `challenge`, `unknown`, `rejected_by_form`: what the page
      proved within the 45 s window, see `wait_for_outcome`;
    - `captcha`: a visible captcha widget is on the form (`reason` is its vendor); nothing was
      clicked or marked, so the row can still be handed to the user;
    - `no_control`: zero or several plausible submit controls; nothing was clicked or marked;
    - `lost_row`: `mark_attempted()` returned False (the row moved, or the page left the ATS's
      hosts, which the caller checks and settles); nothing was clicked.
    """
    candidates, no_control_reason = await find_submit_control(page, ats)
    if candidates is None:
        return {"outcome": "no_control", "reason": no_control_reason}

    if not allow_captcha:
        widget = await captcha_widget(page)
        if widget:
            return {"outcome": "captcha", "reason": widget}

    before_url = page.url
    # The text already on the page, read before the click: a Greenhouse posting shows its job
    # description beside the form, and a sentence like "once we have received your application" in
    # it must not read as the ATS acknowledging ours.
    body_before = await read_body(page)

    # THE FENCE. A crash between this write and the click leaves `submitting` on the row, which
    # `local_apply.recover()` reads as "a submission MAY have gone out". False means we no longer
    # hold the row, and the correct response is to send nothing at all.
    if not mark_attempted():
        return {"outcome": "lost_row", "reason": "row_moved"}

    # DOM click, not a pointer click: a pointer click is intercepted by cookie banners and captcha
    # iframes, and one Lever board burned the full action timeout per option that way.
    await candidates.evaluate("el => el.click()")
    return await wait_for_outcome(page, ats, before_url, body_before)
