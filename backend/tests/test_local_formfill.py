"""The local form filler's matching, consent, EEO decline and submit ladder.

The matcher tests are pure Python. The fill and ladder tests drive a REAL Chromium with
`set_content`, because a locator ladder is a statement about DOM resolution, which a dict-based fake
page can only replay. Nothing here touches the network or an
employer; a real submission is outside any test.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: E402,F401 - shared sys.path + heavy-dep stubs, installed before app imports
from app.services import local_formfill as ff  # noqa: E402
from app.services import local_profile  # noqa: E402

CURRENT = local_profile.CONSENT_TOS_VERSION

FIXTURES = conftest.BACKEND / "tests" / "fixtures" / "submit_pages"

try:
    from playwright.async_api import async_playwright
    _HAVE_BROWSER = True
except ImportError:  # pragma: no cover - environment without playwright
    _HAVE_BROWSER = False


def _field(label, input_id=None, input_type="input", options=None, **extra):
    return {"label": label, "input_id": input_id or label, "input_type": input_type,
            "options": options or [], "required_field": True, "selector": f'[id="{input_id or label}"]',
            **extra}


class ProfileDecodeTests(unittest.TestCase):
    def test_json_text_columns_are_decoded_and_bad_json_is_none(self):
        profile = ff.profile_for_fill({"work_experience": '[{"current": true}]', "skills": "not json",
                                       "portfolio_links": "[]", "email": "a@b.co"})
        self.assertEqual(profile["work_experience"], [{"current": True}])
        self.assertIsNone(profile["skills"])
        self.assertEqual(profile["portfolio_links"], [])
        self.assertEqual(profile["email"], "a@b.co")


class LabelMapTests(unittest.TestCase):
    def test_a_current_job_in_the_editor_shape_leaves_start_date_missing(self):
        fields = [_field("When can you start?", "start")]
        working = {"work_experience": [{"title": "SWE", "end_month": "", "end_year": "", "current": True}]}
        self.assertEqual(ff._match_fields_label_map(fields, working), {})

    def test_no_current_job_derives_immediately(self):
        fields = [_field("When can you start?", "start")]
        for experience in ([], [{"title": "SWE", "end_month": "12", "end_year": "2020", "current": False}]):
            self.assertEqual(ff._match_fields_label_map(fields, {"work_experience": experience}),
                             {"start": "Immediately"})

    def test_profile_columns_answer_their_labels_and_booleans_become_words(self):
        fields = [_field("First Name", "fn"), _field("Email", "em"),
                  _field("Are you legally authorized to work in the United States?", "wa"),
                  _field("Years of professional experience", "yrs")]
        profile = {"first_name": "Jane", "email": "jane@example.com", "authorized_to_work_us": 1,
                   "years_experience": 5}
        self.assertEqual(ff._match_fields_label_map(fields, profile),
                         {"fn": "Jane", "em": "jane@example.com", "wa": "Yes", "yrs": "5"})

    def test_a_legal_name_field_takes_the_full_name(self):
        # A real Ashby form labels its required name box "Legal Name", which no
        # alias matches, so a basic field went to Needs you.
        fields = [_field("Legal Name", "ln")]
        self.assertEqual(ff._match_fields_label_map(fields, {"full_name": "Jane Doe"}), {"ln": "Jane Doe"})

    def test_an_unanswerable_label_gets_no_value(self):
        self.assertEqual(ff._match_fields_label_map([_field("What is your favourite colour?")], {}), {})


class AnsweredOnlyFromProfileTests(unittest.TestCase):
    """The matcher answers from a stored column or not at all: no citizenship inference, no defaults."""
    US_AUTH = "Are you legally authorized to work in the United States?"
    US_SPONSOR = "Will you now or in the future require sponsorship?"

    def test_a_us_question_with_the_column_set_answers_yes_or_no(self):
        fields = [_field(self.US_AUTH, "wa"), _field(self.US_SPONSOR, "sp")]
        yes_no = {"authorized_to_work_us": 1, "requires_sponsorship": 0}
        self.assertEqual(ff._match_fields_label_map(fields, yes_no), {"wa": "Yes", "sp": "No"})
        no_yes = {"authorized_to_work_us": 0, "requires_sponsorship": 1}
        self.assertEqual(ff._match_fields_label_map(fields, no_yes), {"wa": "No", "sp": "Yes"})

    def test_an_empty_column_stays_missing_even_for_a_us_citizen(self):
        fields = [_field(self.US_AUTH, "wa"), _field(self.US_SPONSOR, "sp")]
        profile = {"us_citizenship_status": "citizen", "authorized_to_work_us": None,
                   "requires_sponsorship": None}
        self.assertEqual(ff._match_fields_label_map(fields, profile), {})

    def test_a_question_naming_canada_stays_missing_whatever_the_columns_say(self):
        fields = [_field("Are you legally authorized to work in Canada?", "wa"),
                  _field("Do you require work permit sponsorship in Canada?", "sp"),
                  _field("Yes", "lever_yes", input_type="radio", options=["Yes", "No"],
                         group_label="Do you require work permit sponsorship in Canada?")]
        profile = {"us_citizenship_status": "citizen", "authorized_to_work_us": 1,
                   "requires_sponsorship": 0}
        self.assertEqual(ff._match_fields_label_map(fields, profile), {})

    def test_an_empty_country_stays_missing(self):
        fields = [_field("Country", "c")]
        self.assertEqual(ff._match_fields_label_map(fields, {"country": None}), {})
        self.assertEqual(ff._match_fields_label_map(fields, {"country": "Canada"}), {"c": "Canada"})

    def test_an_empty_referral_source_stays_missing_on_a_text_and_an_option_field(self):
        text = _field("How did you hear about us?", "t")
        menu = _field("How did you hear about us?", "m", input_type="select",
                      options=["Company website", "LinkedIn", "Other"])
        for empty in (None, ""):
            self.assertEqual(ff._match_fields_label_map([text, menu], {"referral_source": empty}), {})

    def test_a_stored_referral_source_picks_the_forms_spelling_of_it(self):
        menu = _field("How did you hear about us?", "m", input_type="select",
                      options=["Company website", "LinkedIn", "Other"])
        self.assertEqual(ff._match_fields_label_map([menu], {"referral_source": "linkedin"}),
                         {"m": "LinkedIn"})


class ConsentAndEeoTests(unittest.TestCase):
    CERTIFY = _field("I certify that the information above is true and accurate", "cert",
                     input_type="checkbox")

    def test_consent_flag_off_affirms_nothing(self):
        values, audit = ff._consent_values([self.CERTIFY], {"auto_consent_authorized": 0})
        self.assertEqual((values, audit), ({}, []))

    def test_consent_flag_on_affirms_and_audits_the_authority(self):
        values, audit = ff._consent_values(
            [self.CERTIFY], {"auto_consent_authorized": 1, "consent_tos_version": CURRENT})
        self.assertEqual(values, {"cert": "Yes"})
        self.assertEqual(audit[0]["authority"], "standard")
        self.assertEqual(audit[0]["tos_version"], CURRENT)

    def test_a_flag_agreed_under_older_wording_affirms_nothing(self):
        values, audit = ff._consent_values(
            [self.CERTIFY], {"auto_consent_authorized": 1, "consent_tos_version": "2026-10-17"})
        self.assertEqual((values, audit), ({}, []))

    def test_a_stale_general_flag_leaves_a_background_box_unticked_though_its_own_flag_is_current(self):
        screening = _field("I authorize a background check and consumer report", "bg", input_type="checkbox")
        profile = {"auto_consent_authorized": 1, "consent_tos_version": "2026-10-17",
                   "background_check_authorized": 1,
                   "background_check_tos_version": local_profile.BACKGROUND_TOS_VERSION}
        self.assertEqual(ff._consent_values([screening], profile)[0], {})

        profile["consent_tos_version"] = CURRENT    # both current: both required flags count
        self.assertEqual(ff._consent_values([screening], profile)[0], {"bg": "Yes"})

    def test_an_undelegable_consent_is_never_affirmed_under_any_flag(self):
        noncompete = _field("I agree to the non-compete agreement", "nc", input_type="checkbox")
        profile = {"auto_consent_authorized": 1, "background_check_authorized": 1,
                   "arbitration_authorized": 1}
        self.assertEqual(ff._consent_values([noncompete], profile)[0], {})

    def test_an_undisclosed_demographic_is_answered_with_the_forms_own_decline_option(self):
        gender = _field("Gender", "g", input_type="select",
                        options=["Male", "Female", "Decline to self-identify"])
        values, audit = ff._eeo_decline_values([gender], {"gender": None})
        self.assertEqual(values, {"g": "Decline to self-identify"})
        self.assertEqual(audit[0]["option"], "Decline to self-identify")

    def test_a_disclosed_demographic_is_never_declined(self):
        gender = _field("Gender", "g", input_type="select", options=["Male", "Female", "Decline"])
        self.assertEqual(ff._eeo_decline_values([gender], {"gender": "Female"}), ({}, []))


_FILL_FORM = """
<form id="application_form">
  <label for="first_name">First Name *</label><input id="first_name" name="first_name" required>
  <label for="agree">I certify that the information above is true and accurate *</label>
  <input id="agree" type="checkbox" name="agree" required>
  <label for="gender">Gender</label>
  <select id="gender" name="gender"><option>Male</option><option>Female</option>
    <option>Decline to self-identify</option></select>
  <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
  <input type="submit" id="submit_app" value="Submit Application">
</form>"""


@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class FillOnARealPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.resume = Path(tmp.name) / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 fake")

    def _fill(self, profile, html=_FILL_FORM):
        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(html)
                fields = await ff.grab_page_fields(page)
                report = await ff.fill_form_fields(page, fields, profile)
                state = await page.evaluate(
                    """() => ({first: document.getElementById('first_name').value,
                               agree: document.getElementById('agree').checked,
                               gender: document.getElementById('gender').value,
                               files: document.getElementById('resume').files.length})""")
                await browser.close()
                return report, state
        return asyncio.run(_go())

    def _profile(self, **extra):
        return {"first_name": "Jane", "full_name": "Jane Doe", "resume_path": str(self.resume), **extra}

    def test_consent_off_leaves_the_box_unchecked_and_reports_it_missing(self):
        report, state = self._fill(self._profile(auto_consent_authorized=0))
        self.assertFalse(state["agree"])
        self.assertFalse(report["ok"])
        self.assertEqual([m for m in report["missing"] if "certify" in m], report["missing"])
        self.assertEqual(report["consented"], [])

    def test_consent_on_checks_the_box_and_a_complete_form_is_ok(self):
        report, state = self._fill(self._profile(auto_consent_authorized=1, consent_tos_version=CURRENT))
        self.assertEqual((state["first"], state["agree"], state["files"]), ("Jane", True, 1))
        self.assertTrue(report["ok"], report["missing"])
        self.assertEqual((report["required_total"], report["required_filled"]), (3, 3))
        self.assertEqual(report["consented"][0]["tos_version"], CURRENT)

    def test_consent_agreed_under_older_wording_leaves_the_box_unchecked_and_reports_it_missing(self):
        report, state = self._fill(self._profile(auto_consent_authorized=1, consent_tos_version="2026-10-17"))
        self.assertFalse(state["agree"])
        self.assertFalse(report["ok"])
        self.assertEqual([m for m in report["missing"] if "certify" in m], report["missing"])
        self.assertEqual(report["consented"], [])

    def test_eeo_decline_picks_the_forms_own_option_and_is_audited(self):
        report, state = self._fill(self._profile(auto_consent_authorized=1, consent_tos_version=CURRENT))
        self.assertEqual(state["gender"], "Decline to self-identify")
        self.assertEqual(report["declined"][0]["option"], "Decline to self-identify")

    def test_no_resume_on_disk_leaves_the_upload_missing(self):
        report, state = self._fill(self._profile(auto_consent_authorized=1, consent_tos_version=CURRENT,
                                                 resume_path="/nonexistent.pdf"))
        self.assertEqual(state["files"], 0)
        self.assertEqual(report["missing"], ["Resume *"])


def _lone_checkbox(question, buttons):
    """Ashby's draw of one question: a hidden checkbox and the buttons that stand in for it."""
    return (f'<div class="field"><label for="q">{question} *</label><div>'
            '<input id="q" type="checkbox" style="display:none">' + buttons + '</div></div>')


_YES_NO = ('<button type="button" aria-pressed="false">Yes</button>'
           '<button type="button" aria-pressed="false">No</button>')


_LEVER_ETHNICITY_OPTIONS = (
    "African American/Black", "Hispanic, Latinx, or Spanish origin", "White/Caucasian",
    "Bi-Racial/Mixed Race - Having 2 or more racial/ethnic groups", "Another race, ethnicity, or origin",
    "I prefer not to disclose",
)


@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class LeverEeoCheckboxGroupTests(unittest.TestCase):
    """Lever's "I identify my ethnicity as / Select all that apply" checkbox group.

    The markup is Match Group's live form (jobs.lever.co/matchgroup, trimmed to six of its twelve options).
    A profile with no ethnicity must decline and tick nothing else. The fill_check dry run ticked
    "Bi-Racial/Mixed Race" beside "I prefer not to disclose", which would tell the employer a race the
    user never gave.
    """

    def test_no_ethnicity_ticks_only_the_decline(self):
        name = "surveysResponses[ce20ed16][responses][field0]"
        rows = "".join(f'<li><label><input type="checkbox" name="{name}" value="{option}">'
                       f'<span class="application-answer-alternative">{option}</span></label></li>'
                       for option in _LEVER_ETHNICITY_OPTIONS)
        body = ('<li class="application-question"><div class="application-label full-width multiple-select">'
                '<div class="text">I identify my ethnicity as</div><p class="description">Select all that apply</p>'
                f'</div><div class="application-field full-width"><ul data-qa="checkboxes">{rows}</ul></div></li>')

        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(f'<form id="application_form"><ul>{body}</ul></form>')
                fields = await ff.grab_page_fields(page)
                await ff.fill_form_fields(page, fields, {"full_name": "Jane Doe", "ethnicity": None})
                ticked = await page.eval_on_selector_all(
                    "input[type=checkbox]:checked", "boxes => boxes.map(b => b.value)")
                await browser.close()
                return ticked

        self.assertEqual(asyncio.run(_go()), ["I prefer not to disclose"])

    def test_a_radio_group_with_no_gender_on_file_picks_only_the_decline(self):
        # The same form's gender question is a radio group with no heading row; the option-row rule covers it.
        name = "surveysResponses[ce20ed16][responses][field1]"
        rows = "".join(f'<li><label><input type="radio" name="{name}" value="{option}">'
                       f'<span class="application-answer-alternative">{option}</span></label></li>'
                       for option in ("Female", "Male", "Non-binary", "I prefer not to disclose"))
        body = ('<li class="application-question"><div class="application-label full-width multiple-choice">'
                '<div class="text">What gender do you most closely identify as?</div></div>'
                f'<div class="application-field full-width"><ul data-qa="multiple-choice">{rows}</ul></div></li>')

        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(f'<form id="application_form"><ul>{body}</ul></form>')
                fields = await ff.grab_page_fields(page)
                await ff.fill_form_fields(page, fields, {"full_name": "Jane Doe", "gender": None})
                picked = await page.eval_on_selector_all(
                    "input[type=radio]:checked", "radios => radios.map(r => r.value)")
                await browser.close()
                return picked

        self.assertEqual(asyncio.run(_go()), ["I prefer not to disclose"])

    def _ticked(self, body, profile, selector):
        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(f'<form id="application_form"><ul>{body}</ul></form>')
                fields = await ff.grab_page_fields(page)
                await ff.fill_form_fields(page, fields, profile)
                ticked = await page.eval_on_selector_all(selector, "boxes => boxes.map(b => b.value)")
                await browser.close()
                return ticked
        return asyncio.run(_go())

    def test_a_disclosed_ethnicity_is_never_declined_in_a_checkbox_group(self):
        # Review round 2: the decline is skipped when the group's question maps to a profile column the
        # user filled in. Ticking "prefer not to disclose" beside their answer would contradict it.
        name = "surveysResponses[ce20ed16][responses][field0]"
        rows = "".join(f'<li><label><input type="checkbox" name="{name}" value="{option}">'
                       f'<span class="application-answer-alternative">{option}</span></label></li>'
                       for option in _LEVER_ETHNICITY_OPTIONS)
        body = ('<li class="application-question"><div class="application-label full-width multiple-select">'
                '<div class="text">I identify my ethnicity as</div></div>'
                f'<div class="application-field full-width"><ul data-qa="checkboxes">{rows}</ul></div></li>')
        ticked = self._ticked(body, {"full_name": "Jane Doe", "ethnicity": "Hispanic, Latinx, or Spanish origin"},
                              "input[type=checkbox]:checked")
        self.assertEqual(ticked, ["Hispanic, Latinx, or Spanish origin"])

    def test_a_disclosed_gender_is_never_declined_in_a_radio_group(self):
        name = "surveysResponses[ce20ed16][responses][field1]"
        rows = "".join(f'<li><label><input type="radio" name="{name}" value="{option}">'
                       f'<span class="application-answer-alternative">{option}</span></label></li>'
                       for option in ("Female", "Male", "Non-binary", "I prefer not to disclose"))
        body = ('<li class="application-question"><div class="application-label full-width multiple-choice">'
                '<div class="text">What gender do you most closely identify as?</div></div>'
                f'<div class="application-field full-width"><ul data-qa="multiple-choice">{rows}</ul></div></li>')
        ticked = self._ticked(body, {"full_name": "Jane Doe", "gender": "Female"}, "input[type=radio]:checked")
        self.assertEqual(ticked, ["Female"])

    def test_a_required_custom_question_dropdown_is_scraped_and_reported_missing(self):
        # Same form: Lever's custom questions put the prompt in a <div class="text">, not a <label>. The
        # scrape returned no field at all, so required_total skipped it, the dry run reported 6 of 6, and a
        # real run would click Submit with it empty.
        body = ('<li class="application-question custom-question"><div><div class="application-label full-width '
                'dropdown"><div class="text">Will you now or in the future require sponsorship?'
                '<span class="required">✱</span></div></div><div class="application-field full-width '
                'required-field"><div class="application-dropdown"><select name="cards[6313411a][field1]" '
                'required=""><option value="">Select...</option><option value="Yes">Yes</option>'
                '<option value="No">No</option></select></div></div></div></li>')

        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(f'<form id="application_form"><ul>{body}</ul></form>')
                fields = await ff.grab_page_fields(page)
                report = await ff.fill_form_fields(page, fields, {"full_name": "Jane Doe"})
                await browser.close()
                return report

        report = asyncio.run(_go())
        self.assertEqual(report["required_total"], 1)
        self.assertEqual(len(report["missing"]), 1)


def _lever_location(suggestions, lands_on=None):
    """Lever's "Current location" autocomplete, with its behaviour copied from Match Group's live form.

    Typing loads suggestions into `.dropdown-results` after a short delay; clicking one writes the hidden
    `selectedLocation`, which is the only value Lever submits. The box is emptied when it loses focus
    with no suggestion chosen. `lands_on` maps a row's text to the place a click on it actually records,
    standing in for a list that changed between the filler's read and its click.
    """
    return ('<form id="application_form"><ul><li class="application-question"><label>'
            '<div class="application-label">Current location <span class="required">✱</span></div>'
            '<div class="application-field"><input class="location-input" id="location-input" type="text" '
            'name="location" required=""><input id="selected-location" type="hidden" name="selectedLocation">'
            '<div class="dropdown-container"><div class="dropdown-results"></div></div></div>'
            '</label></li></ul></form>'
            '<script>'
            f'const SUGGESTIONS = {suggestions!r}; const LANDS_ON = {json.dumps(lands_on or {})};'
            'const box = document.getElementById("location-input");'
            'const chosen = document.getElementById("selected-location");'
            'const results = document.querySelector(".dropdown-results");'
            'box.addEventListener("input", () => { chosen.value = ""; results.innerHTML = "";'
            '  setTimeout(() => SUGGESTIONS.forEach(name => { const row = document.createElement("div");'
            '    row.textContent = name; row.addEventListener("mousedown", () => {'
            '      const place = LANDS_ON[name] || name;'
            '      box.value = place; chosen.value = JSON.stringify({name: place}); results.innerHTML = ""; });'
            '    results.appendChild(row); }), 100); });'
            'box.addEventListener("blur", () => { if (!chosen.value) box.value = ""; });'
            '</script>')


@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class LeverLocationTests(unittest.TestCase):
    """Lever's location was reported filled while the page held no location."""

    def _fill(self, location, suggestions, lands_on=None):
        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(_lever_location(suggestions, lands_on))
                fields = await ff.grab_page_fields(page)
                report = await ff.fill_form_fields(page, fields, {"current_location": location})
                chosen = await page.input_value("#selected-location")
                await browser.close()
                return report, chosen
        with mock.patch.object(ff, "COMBOBOX_QUERY_TIMEOUT_MS", 1000):
            return asyncio.run(_go())

    def test_the_suggestion_naming_the_users_city_and_state_is_chosen(self):
        # The board's top hit is another country's Los Angeles; the state picks the right one, and "CA"
        # matches "California".
        report, chosen = self._fill("Los Angeles, CA", ["Los Angeles, Chile", "Los Angeles, California, United States"])
        self.assertEqual(chosen, '{"name":"Los Angeles, California, United States"}')
        self.assertEqual((report["required_total"], report["required_filled"]), (1, 1))

    def test_a_city_named_like_its_state_still_needs_the_state_after_the_city(self):
        # Review round 2: "New York" is in the city part of every row, so the state is read only after it.
        report, chosen = self._fill("New York, NY", ["New York, Lincolnshire, United Kingdom",
                                                     "New York, NY, USA"])
        self.assertEqual(chosen, '{"name":"New York, NY, USA"}')
        self.assertEqual((report["required_total"], report["required_filled"]), (1, 1))

    def test_a_click_that_records_another_place_is_undone_and_reported_missing(self):
        # Review round 3: the list can change between the read and the click, so the click can land on the
        # UK row. Whatever the board records is checked again, and a wrong place is cleared.
        report, chosen = self._fill("New York, NY", ["New York, NY, USA"],
                                    lands_on={"New York, NY, USA": "New York, Lincolnshire, United Kingdom"})
        self.assertEqual(chosen, "")
        self.assertEqual((report["required_total"], report["required_filled"]), (1, 0))
        self.assertEqual(len(report["missing"]), 1)

    def test_a_state_written_in_full_matches_its_code(self):
        report, chosen = self._fill("Los Angeles, California", ["Los Angeles, CA, USA"])
        self.assertEqual(chosen, '{"name":"Los Angeles, CA, USA"}')

    def test_a_suggestion_in_another_state_is_never_chosen(self):
        # Review round 1: clicking the top hit would tell the employer the user lives in Oregon.
        report, chosen = self._fill("Portland, ME", ["Portland, Oregon, United States"])
        self.assertEqual(chosen, "")
        self.assertEqual((report["required_total"], report["required_filled"]), (1, 0))

    def test_a_location_with_no_state_is_left_for_the_user(self):
        report, chosen = self._fill("Portland", ["Portland, Oregon, United States", "Portland, Maine, United States"])
        self.assertEqual(chosen, "")
        self.assertEqual(len(report["missing"]), 1)

    def test_no_suggestion_leaves_the_field_missing(self):
        report, chosen = self._fill("Los Angeles, CA", [])
        self.assertEqual(chosen, "")
        self.assertEqual((report["required_total"], report["required_filled"]), (1, 0))
        self.assertEqual(len(report["missing"]), 1)


@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class LoneCheckboxShapeTests(unittest.TestCase):
    """What Needs you is asked for a question drawn as one checkbox."""

    def _scrape(self, body):
        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(f'<form id="application_form">{body}</form>')
                fields = await ff.grab_page_fields(page)
                report = await ff.fill_form_fields(page, fields, {"full_name": "Jane Doe"})
                await browser.close()
                return fields, report
        return asyncio.run(_go())

    def test_ashby_yes_no_buttons_become_the_cards_choices(self):
        fields, report = self._scrape(_lone_checkbox("Are you related to any current employees?", _YES_NO))
        self.assertEqual(fields[0]["options"], ["Yes", "No"])
        self.assertEqual(report["missing_shapes"], [{"kind": "single", "options": ["Yes", "No"], "max_length": None}])

    def test_an_acknowledgement_box_keeps_its_label_as_the_one_choice(self):
        fields, report = self._scrape(
            '<label for="ack">I have read the privacy notice *</label><input id="ack" type="checkbox">')
        self.assertEqual(fields[0]["options"], [])
        self.assertEqual(report["missing_shapes"][0]["options"], ["I have read the privacy notice *"])

    def test_two_checkbox_rows_in_one_group_stay_multi_over_their_labels(self):
        fields, report = self._scrape(
            '<fieldset><legend>Which tools do you use? *</legend>'
            '<label><input type="checkbox" name="t" value="a" required> Python</label>'
            '<label><input type="checkbox" name="t" value="b" required> Go</label></fieldset>')
        (shape,) = report["missing_shapes"]
        self.assertEqual((shape["kind"], shape["options"]), ("multi", ["Python", "Go"]))

    def test_buttons_that_are_not_toggles_are_never_offered_as_choices(self):
        fields, report = self._scrape(_lone_checkbox(
            "I confirm the attached file is mine",
            '<button type="button">Upload</button><button type="button">Remove</button>'))
        self.assertEqual(fields[0]["options"], [])
        self.assertEqual(report["missing_shapes"][0]["options"], ["I confirm the attached file is mine *"])


@unittest.skipUnless(_HAVE_BROWSER, "playwright not installed")
class SubmitLadderTests(unittest.TestCase):
    """The ladder resolves the same control as the earlier hosted app's tests, or refuses."""

    def setUp(self):
        saved = (ff.SUBMIT_CONFIRM_TIMEOUT_MS, ff.PLAYWRIGHT_ACTION_TIMEOUT)
        # Against `set_content` fixtures nothing is in flight, so every "unknown" case would burn the
        # real 45 s. The order the signals are checked in is unaffected by how long we wait.
        ff.SUBMIT_CONFIRM_TIMEOUT_MS, ff.PLAYWRIGHT_ACTION_TIMEOUT = 250, 2000
        self.addCleanup(lambda: setattr(ff, "SUBMIT_CONFIRM_TIMEOUT_MS", saved[0]))
        self.addCleanup(lambda: setattr(ff, "PLAYWRIGHT_ACTION_TIMEOUT", saved[1]))

    def _submit(self, fixture, ats, *, fence=True, after=None):
        html = (FIXTURES / fixture).read_text()
        fenced = []

        async def _go():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.set_content(html)
                clicked = []
                # Intercept the DOM click so nothing is submitted, while proving WHICH element the
                # ladder resolved to.
                await page.expose_function("_record", lambda t: clicked.append(t))
                await page.evaluate(
                    """() => document.addEventListener('click', e => {
                         e.preventDefault();
                         window._record((e.target.id || e.target.className ||
                                         e.target.value || e.target.innerText || '').trim());
                       }, true)""")
                if after:
                    await after(page)

                def mark_attempted():
                    fenced.append(len(clicked))   # how many clicks had landed when the fence ran
                    return fence
                result = await ff.submit_form(page, [], ats, mark_attempted)
                await browser.close()
                return result, clicked

        result, clicked = asyncio.run(_go())
        return result, clicked, fenced

    def test_the_ats_selector_wins_over_the_second_form_on_the_page(self):
        result, clicked, _ = self._submit("greenhouse.html", "greenhouse")
        self.assertEqual(clicked, ["submit_app"])
        self.assertNotEqual(result["outcome"], "no_control")

    def test_the_form_subtree_rung_resolves_a_board_with_no_ats_id(self):
        _, clicked, _ = self._submit("ashby.html", "ashby")
        self.assertEqual(len(clicked), 1)
        self.assertIn("ashby-button", clicked[0])

    def test_two_plausible_controls_are_refused_and_never_fenced(self):
        result, clicked, fenced = self._submit("lever.html", "lever")
        self.assertEqual((result["outcome"], result["reason"]), ("no_control", "submit_ambiguous"))
        self.assertEqual((clicked, fenced), ([], []))

    def test_a_page_with_no_submit_control_is_refused_and_never_fenced(self):
        async def _strip(page):
            await page.evaluate("() => document.querySelectorAll('button,input[type=submit]').forEach(n => n.remove())")
        result, clicked, fenced = self._submit("ashby.html", "ashby", after=_strip)
        self.assertEqual((result["outcome"], result["reason"]), ("no_control", "submit_control_not_found"))
        self.assertEqual((clicked, fenced), ([], []))

    def test_the_fence_runs_before_the_click(self):
        _, clicked, fenced = self._submit("greenhouse.html", "greenhouse")
        self.assertEqual(fenced, [0])
        self.assertEqual(len(clicked), 1)

    def test_a_fence_that_finds_the_row_moved_sends_nothing(self):
        result, clicked, fenced = self._submit("greenhouse.html", "greenhouse", fence=False)
        self.assertEqual(result["outcome"], "lost_row")
        self.assertEqual((clicked, fenced), ([], [0]))

    def test_a_success_page_is_read_as_submitted(self):
        async def _ack(page):
            await page.evaluate(
                """() => document.querySelector('#application_form')
                     .addEventListener('click', () => {
                        document.body.innerHTML = '<h1>Thank you for applying</h1>';
                     }, true)""")
        result, _, _ = self._submit("greenhouse.html", "greenhouse", after=_ack)
        self.assertEqual(result["outcome"], "submitted")

    def test_acknowledgement_wording_already_in_the_job_description_is_not_a_success(self):
        # Greenhouse shows the posting beside the form. A blocked click leaves that text in place.
        async def _description(page):
            await page.evaluate(
                """() => { const p = document.createElement('p');
                           p.textContent = 'Once we have received your application, a recruiter reviews it.';
                           document.body.append(p); }""")
        result, _, _ = self._submit("greenhouse.html", "greenhouse", after=_description)
        self.assertEqual((result["outcome"], result["reason"]), ("unknown", "submit_outcome_unknown"))

    def test_a_failed_pre_click_read_disables_the_text_signal(self):
        # Without a baseline, description wording on the page cannot be told from an acknowledgement.
        async def _description_and_broken_read(page):
            await page.evaluate(
                """() => { const p = document.createElement('p');
                           p.textContent = 'Once we have received your application, a recruiter reviews it.';
                           document.body.append(p); }""")
            real = page.inner_text
            calls = []

            async def flaky(selector, **kwargs):
                calls.append(selector)
                if len(calls) == 1:    # the baseline read before the click
                    raise RuntimeError("mid-navigation")
                return await real(selector, **kwargs)
            page.inner_text = flaky
        result, _, _ = self._submit("greenhouse.html", "greenhouse", after=_description_and_broken_read)
        self.assertEqual((result["outcome"], result["reason"]), ("unknown", "submit_outcome_unknown"))

    def test_a_validation_banner_with_the_form_still_mounted_is_a_rejection(self):
        async def _reject(page):
            await page.evaluate(
                """() => document.querySelector('#submit_app')
                     .addEventListener('click', () => {
                        const e = document.createElement('div');
                        e.setAttribute('role', 'alert');
                        e.textContent = 'Email is required';
                        document.body.prepend(e);
                     }, true)""")
        result, _, _ = self._submit("greenhouse.html", "greenhouse", after=_reject)
        self.assertEqual((result["outcome"], result["reason"]), ("rejected_by_form", "form_rejected"))

    def test_a_click_with_no_visible_response_is_unknown(self):
        result, _, _ = self._submit("greenhouse.html", "greenhouse")
        self.assertEqual((result["outcome"], result["reason"]), ("unknown", "submit_outcome_unknown"))


if __name__ == "__main__":
    unittest.main()
