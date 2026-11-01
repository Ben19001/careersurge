"""The local app's profile, resume, saved answers and prepared batch.

Four writers that the onboarding and Profile screens call into (through `app.local_api`), and their readers:
- `parse_resume` / `save_draft` / `save_profile`: a PDF becomes a *draft* the user reviews, and only
  `save_profile` (called after the user confirms) writes the `profile` row the form filler reads.
  `get_draft` / `get_profile` read them back.
- `seed_global_questions` / `save_global_answer` / `list_global_questions`: the global rows of `application_qna_bank`.
- `build_prepared`: one `prepared` `job_applications` row per listing, with the repost evidence.

Flask-free, no Supabase, no `app.config`; storage is `app.store.db`. The model never writes text:
code finds candidate lines, `jev.choose` picks among them (or says none), and the pick is copied.
Normalization and similarity are Python; SQL only stores the results.
"""
import datetime
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import pdfplumber

from app.services import jev
from app.store import db

logger = logging.getLogger(__name__)

# The earlier hosted extractor's page limit; later pages are references and appendices.
_MAX_PAGES = 4
# Jev caps state plus the longest question at 32k tokens; this keeps a 4-page resume far under it.
_MAX_STATE_CHARS = 60000
_MAX_CANDIDATES = 50
# Page geometry for column detection (see _read_lines). Fractions are of the page width/height.
_HEADER_BAND = 0.15        # top of page 1: name and contact lines, read as they come, never split
_GUTTER_MIN_WIDTH = 14     # points; a narrower empty band is just a wide word space
_GUTTER_ZONE = (0.15, 0.85)  # the gap must sit inside the middle of the page
_COLUMN_MIN_WIDTH = 0.18   # each side must be at least this wide to be a column
_COLUMN_MIN_PROSE = 8      # and hold at least this many words that are not date tokens
_MIN_WORDS_FOR_COLUMNS = 16
_LINE_TOLERANCE = 3        # points; words whose bottoms differ by less share a line

# What each pattern matches, with an example:
# _EMAIL: jane.doe@example.com   _PHONE: (408) 555-0142, 415-555-0199, +1 408.555.0142
# _LINKEDIN: linkedin.com/in/janedoe (profile links only)   _GITHUB: github.com/janedoe (the user part only,
# so github.com/janedoe/repo is found as github.com/janedoe)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_%-]+/?", re.I)
_GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9-]+/?", re.I)
# _URL: https://x.dev/me, www.me.io, or a bare janedoe.dev (only with one of the listed endings, and
# not when it is the tail of an email address or a longer dotted name like ASP.NET inside a word).
_URL = re.compile(r"(?:https?://|www\.)[^\s|,;]+|(?<![@\w.])[a-z0-9-]+\.(?:dev|io|me|app|com|org|net|co)(?:/[^\s|,;]*)?(?![\w@])", re.I)
# _PLACE: "San Jose, CA" anywhere in a piece of text (city, comma, two capitals).
_PLACE = re.compile(r"([A-Z][A-Za-z.' ]+),\s*([A-Z]{2})\b")
# _TRAILING_PLACE: a header line that ENDS in a place, "Acme Corp, San Francisco, CA" -> text "Acme Corp",
# place "San Francisco, CA". The separator before the place may be a comma, pipe, dot or dash.
_TRAILING_PLACE = re.compile(r"^(?P<text>.+?)[,|·•–-]\s*(?P<place>[A-Z][A-Za-z.' ]+,\s*[A-Z]{2})$")
# The resume the onboarding screen accepts: a PDF of at most 1 MB, as the hosted onboarding enforced.
RESUME_MAX_BYTES = 1024 * 1024
RESUME_RULE = "Your resume must be a PDF under 1 MB."

# _BULLET: the marker at the start of a bullet line ("- ", "• ", "* "), stripped to get the text.
_BULLET = re.compile(r"^\s*[-•*–·▪●◦]\s*")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE = (r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}"
         r"|(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}|(?:19|20)\d{2})")
# _DATE_RANGE: "Jan 2021 - Present", "06/2019 - 12/2020", "2018-2020". _DATE_OR_RANGE also accepts a lone
# date ("May 2026"), which education uses for an expected graduation. _DATE_TOKEN is one word of such a date.
_DATE_RANGE = re.compile(rf"\b({_DATE})\s*(?:-|–|—|to)\s*({_DATE}|present|current|now|ongoing)\b", re.I)
_DATE_TOKEN = re.compile(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?|(?:19|20)\d{2}|"
                         r"present|current|now|to|[-–—]|\d{1,2}/(?:19|20)\d{2}", re.I)
_DATE_OR_RANGE = re.compile(rf"\b({_DATE})(?:\s*(?:-|–|—|to)\s*({_DATE}|present|current|now|ongoing))?\b", re.I)

# Whole-line headings. `other` headings exist only to end the section before them.
_HEADINGS = (
    ("experience", re.compile(r"(work |professional |relevant )?(experience|employment( history)?|work history)", re.I)),
    ("education", re.compile(r"education( (and|&) training)?|academic background", re.I)),
    ("skills", re.compile(r"(technical |core |key )?skills( (and|&) \w+)?|technologies", re.I)),
    ("projects", re.compile(r"(personal |selected |side )?projects", re.I)),
    ("certifications", re.compile(r"certifications?( (and|&) licenses)?|licenses( (and|&) certifications)?", re.I)),
    ("other", re.compile(r"summary|profile|objective|about( me)?|awards|honors|publications|interests|"
                         r"volunteer(ing)?( experience)?|languages|references|activities|leadership", re.I)),
)

# _DEGREE_LINE: a line that names a degree: "Bachelor of Science in CS", "B.S. Computer Science", "MBA".
_DEGREE_LINE = re.compile(r"(?<!\w)(bachelor|master|associate|doctor|ph\.?d|mba|b\.?[sa]\.?|m\.?[sa]\.?|diploma|certificate)(?!\w)", re.I)
# _DEGREE_SPLIT: splits a degree line into the degree and the field: "Bachelor of Science in Computer
# Science" -> "Bachelor of Science" / "Computer Science"; "B.S. Computer Science" -> "B.S." / "Computer Science".
_DEGREE_SPLIT = re.compile(
    r"^(?P<degree>(?:Bachelor|Master|Associate|Doctor)s?(?: of [A-Za-z]+)*|B\.?[A-Z]\.?|M\.?[A-Z]\.?|MBA|Ph\.?D\.?)"
    r"[\s,]+(?:in\s+)?(?P<field>.+)$")
# _GPA: "GPA: 3.8" or "GPA 3.8/4.0", with the comma before it, so cutting it out of a degree line leaves no stray comma.
_GPA = re.compile(r",?\s*GPA:?\s*(\d(?:\.\d+)?)(?:\s*/\s*4(?:\.0+)?)?", re.I)

# (question, question_type, field shape) for the global rows the onboarding screen asks once. The shape
# is the control onboarding draws and the rule `save_global_answer` enforces, in the vocabulary of the
# Needs you card (`{kind, options, max_length}`, plus the kind `number`). It is fixed here, not stored.
# Salary and relocation depend on the job, so the global answer is only the default until the user saves a
# company or job answer; the rest are facts about the person.
GLOBAL_SEED = (
    ("Are you legally authorized to work in the United States?", "stable_fact",
     {"kind": "single", "options": ["Yes", "No"], "max_length": None}),
    ("Will you now or in the future require visa sponsorship?", "stable_fact",
     {"kind": "single", "options": ["Yes", "No"], "max_length": None}),
    ("What are your salary expectations?", "job_dependent",
     {"kind": "number", "options": [], "max_length": None}),
    ("What is your notice period?", "stable_fact",
     {"kind": "single", "max_length": None,
      "options": ["Immediately", "1 week", "2 weeks", "1 month", "2 months", "3 months or more"]}),
    ("Are you willing to relocate?", "job_dependent",
     {"kind": "single", "options": ["Yes", "No"], "max_length": None}),
    ("When can you start?", "stable_fact",
     {"kind": "single", "max_length": None,
      "options": ["Immediately", "Within 2 weeks", "Within 1 month", "Within 2 months", "3 months or more"]}),
)
# The shape of a global row the seed does not know (one the Needs you tray created): a text box, capped
# so any short answer fits but a pasted resume is refused.
GLOBAL_TEXT_SHAPE = {"kind": "text", "options": [], "max_length": 500}

# "Applied before" for the repost check: a row in one of these statuses.
_APPLIED_STATUSES = ("pending", "in_progress", "submitting", "applied", "blocked_needs_input")
_WEAK_WINDOW = datetime.timedelta(days=365)


def _dedupe(items) -> list:
    """The items in first-seen order with repeats dropped (`dict.fromkeys` keeps insertion order).

    Used for every candidate list and for skills and certifications, so a value that appears twice
    in a resume is offered once.
    """
    return list(dict.fromkeys(items))


def _split_columns(body: list[dict], page_width: float) -> list[list[dict]]:
    """Return `[body]` for a one-column page, or `[left_words, right_words]` for a two-column one.

    Step 1, find a gutter: sort the words by their left edge and walk them, tracking `reach`, the
    furthest right edge seen so far (so one long word cannot hide a gap behind it). The widest
    gap that is at least `_GUTTER_MIN_WIDTH` points and sits inside `_GUTTER_ZONE` is the gutter.
    Step 2, check that it really separates two columns: right-aligned dates and locations on a
    one-column resume also leave a gap, so BOTH sides must be at least `_COLUMN_MIN_WIDTH` of the
    page wide and hold `_COLUMN_MIN_PROSE` words that are not date tokens. A column of dates is
    narrow and is all date tokens, so it fails both tests.
    """
    if len(body) < _MIN_WORDS_FOR_COLUMNS:
        return [body]
    zone_left = page_width * _GUTTER_ZONE[0]
    zone_right = page_width * _GUTTER_ZONE[1]
    gutter = None
    widest = _GUTTER_MIN_WIDTH
    reach = None
    for word in sorted(body, key=lambda w: w["x0"]):
        if reach is not None and word["x0"] - reach >= widest and reach >= zone_left and word["x0"] <= zone_right:
            widest = word["x0"] - reach
            gutter = (reach + word["x0"]) / 2
        reach = word["x1"] if reach is None else max(reach, word["x1"])
    if gutter is None:
        return [body]

    left = [word for word in body if word["x1"] <= gutter]
    right = [word for word in body if word["x0"] >= gutter]
    for side in (left, right):
        if not side:
            return [body]
        width = max(w["x1"] for w in side) - min(w["x0"] for w in side)
        prose_words = 0
        for word in side:
            if not _DATE_TOKEN.fullmatch(word["text"]):
                prose_words += 1
        if width < page_width * _COLUMN_MIN_WIDTH or prose_words < _COLUMN_MIN_PROSE:
            return [body]
    return [left, right]


def _read_lines(pdf_bytes: bytes) -> list[dict]:
    """The first `_MAX_PAGES` pages as reading-order lines `{text, size, page, top}`.

    Why not plain `extract_text`: on a two-column page it interleaves the columns line by line,
    which glues a sidebar skill onto an experience bullet. So each page's words below the page-1
    header band go through `_split_columns`, and columns are read left then right. Words are
    grouped into a line when their bottoms are within `_LINE_TOLERANCE` points. `top` is the line's
    height down the page as a fraction, used to find the name. Raises ValueError for bytes
    pdfplumber cannot open.
    """
    lines = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_number, page in enumerate(pdf.pages[:_MAX_PAGES]):
                words = page.extract_words(extra_attrs=["size"])
                head_limit = page.height * _HEADER_BAND if page_number == 0 else 0
                head = [word for word in words if word["top"] < head_limit]
                body = [word for word in words if word["top"] >= head_limit]

                columns = _split_columns(body, page.width)

                # Words to lines: sort by baseline, start a new line when the baseline jumps.
                for group in (head, *columns):
                    group.sort(key=lambda w: (w["bottom"], w["x0"]))
                    row = []
                    for word in group + [None]:  # the None sentinel flushes the last line
                        if row and (word is None or abs(word["bottom"] - row[-1]["bottom"]) > _LINE_TOLERANCE):
                            row.sort(key=lambda w: w["x0"])
                            lines.append({"text": " ".join(w["text"] for w in row),
                                          "size": max(w["size"] for w in row),
                                          "page": page_number, "top": min(w["top"] for w in row) / page.height})
                            row = []
                        if word is not None:
                            row.append(word)
    except Exception as exc:  # pdfminer raises a different type per kind of damage
        raise ValueError("not a readable PDF") from exc
    return lines


def _normalize_question(text: str) -> str:
    """Lowercase, punctuation removed, whitespace collapsed: the key of `application_qna_bank`.

    Used by the seed and `save_global_answer` here and imported by the classifier in `local_answers`, so one
    spelling of a question is one row.
    """
    return " ".join(re.sub(r"[^\w\s]", "", text.lower()).split())


def _pick_field(answers: dict | None, question_id: str, candidates: list[str], fallback_index: int = 0) -> dict:
    """The `{value, candidates, confidence, source}` dict the review screen shows for one field.

    `answers` is `jev.choose`'s result, or None when Jev was unavailable: then the field takes
    its `fallback_index`-th regex candidate with confidence 0. A question that was never asked
    (no candidates, `source` `none`) or that Jev answered `none` (`source` `jev`) has value None;
    the candidates stay so the user can still pick one. Used by every scalar and every entry field in `parse_resume`.
    """
    if answers is None:
        value = candidates[fallback_index] if len(candidates) > fallback_index else None
        return {"value": value, "candidates": candidates, "confidence": 0, "source": "regex_fallback"}
    if question_id not in answers:  # no candidates, so no question was sent
        return {"value": None, "candidates": candidates, "confidence": 0, "source": "none"}
    value, confidence = answers[question_id]
    return {"value": value, "candidates": candidates, "confidence": confidence, "source": "jev"}


def _split_date(text: str | None) -> tuple[str, str, bool]:
    """`(month "01".."12" or "", 4-digit year or "", is_current)` from "Jan 2021", "06/2019", "2018".

    The editor stores months as zero-padded strings and a separate `current` flag instead of an
    end date (`app.profile.tsx:757`). Used for both ends of every experience and education range.
    """
    if text is None:
        return "", "", False
    if re.fullmatch(r"present|current|now|ongoing", text.strip(), re.I):
        return "", "", True
    year = re.search(r"(?:19|20)\d{2}", text)
    name = re.search(r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", text, re.I)
    numeric = re.match(r"(0?[1-9]|1[0-2])/", text)
    if name:
        month = f"{_MONTHS.index(name.group(0).lower()) + 1:02d}"
    elif numeric:
        month = numeric.group(1).zfill(2)
    else:
        month = ""
    return month, year.group(0) if year else "", False


def _split_entries(lines: list[str], pattern: re.Pattern) -> list[dict]:
    """Split a section into entries around the lines that carry a date.

    Returns `{"headers": [...], "dates": (start, end_or_None), "body": [...]}` per entry. The
    headers are the date line with the date cut out plus up to two header-like lines on each side
    (not a bullet, short, no closing period), which covers both "Title / Company / Dates" and
    "Title  Dates / Company". Everything after the headers up to the next entry is the body.
    Forward claims win over backward ones when two entries have no bullets between them. Text
    before the first entry is dropped. Used for experience and education.
    """
    date_lines = [index for index, line in enumerate(lines) if pattern.search(line)]
    # A header line is short, starts like a name, is not a bullet and does not end like a sentence.
    # Wrapped bullet text usually fails one of these, which keeps it out of the headers.
    header_like = []
    for line in lines:
        is_bullet = bool(_BULLET.match(line))
        is_short = len(line) <= 80
        ends_like_sentence = line.endswith((".", ";"))
        starts_like_name = line[:1].isalnum()
        header_like.append(not is_bullet and is_short and not ends_like_sentence
                           and starts_like_name and not pattern.search(line))
    # Three passes, each line belongs to at most one entry (`claimed`):
    #   1. every date line is claimed first, so no header search can run into another entry's date;
    #   2. forward: up to two header-like lines BELOW each date line ("Title  Dates" then "Company");
    #   3. backward: up to two header-like lines ABOVE each date line ("Title", "Company", then dates).
    # Forward runs before backward, so when two entries have no bullets between them the line
    # between belongs to the entry above it.
    claimed = set(date_lines)
    forward = {}
    for index in date_lines:
        picked = []
        for follower in range(index + 1, min(index + 3, len(lines))):
            if not header_like[follower] or follower in claimed:
                break
            picked.append(follower)
            claimed.add(follower)
        forward[index] = picked
    backward = {}
    for index in date_lines:
        picked = []
        for leader in range(index - 1, max(index - 3, -1), -1):
            if not header_like[leader] or leader in claimed:
                break
            picked.insert(0, leader)
            claimed.add(leader)
        backward[index] = picked

    entries = []
    for position, index in enumerate(date_lines):
        match = pattern.search(lines[index])
        rest = f"{lines[index][:match.start()]} {lines[index][match.end():]}".strip(" |,-–—·•\t")
        headers = [lines[i] for i in backward[index]] + ([rest] if rest else []) + [lines[i] for i in forward[index]]
        # The entry's body starts after its last header line (the date line itself if it has no
        # forward headers) and ends where the next entry's first header line begins.
        last_header_line = index
        if forward[index]:
            last_header_line = forward[index][-1]
        next_entry_start = len(lines)
        if position + 1 < len(date_lines):
            following = date_lines[position + 1]
            next_entry_start = following
            if backward[following]:
                next_entry_start = backward[following][0]
        body = [_BULLET.sub("", line).strip() for line in lines[last_header_line + 1:next_entry_start]]
        entries.append({"headers": headers, "dates": (match.group(1), match.group(2)),
                        "body": [line for line in body if line]})
    return entries


_FIELD_LABELS = {
    "full_name": "the candidate's full name",
    "email": "the candidate's own email address",
    "phone": "the candidate's own phone number",
    "linkedin_url": "the candidate's own LinkedIn profile",
    "github_url": "the candidate's own GitHub profile",
}


def _ask_jev(resume_text: str, candidates: dict, place_names: list[str], experience: list[dict],
             education: list[dict]) -> dict | None:
    """Build every question for one resume and send them in a single `jev.choose` request.

    A field with no candidates asks nothing (there is nothing to choose from), and a resume with
    no candidates at all sends no request and returns `{}`. Returns None when Jev is unavailable
    for any reason, which `_pick_field` turns into the regex fallback; the parse never fails
    because Jev did. Question ids: the `_FIELD_LABELS` keys, `location`, `exp<N>_title`,
    `exp<N>_company`, `edu<N>_institution`, `edu<N>_degree`.
    """
    questions = {}
    for key, label in _FIELD_LABELS.items():
        if candidates[key]:
            questions[key] = (f"Which option is {label} on this resume?", candidates[key])
    if place_names:
        questions["location"] = ("Which option is the city and state where the candidate lives?", place_names)
    for number, entry in enumerate(experience):
        if entry["headers"]:
            questions[f"exp{number}_title"] = (
                "Which option is the job title of this position? The company name is not the title.", entry["headers"])
            questions[f"exp{number}_company"] = (
                "Which option is the employer's name for this position? The job title is not the company.",
                entry["headers"])
    for number, entry in enumerate(education):
        if entry["headers"]:
            questions[f"edu{number}_institution"] = (
                "Which option is the name of the school, college or university?", entry["headers"])
        if entry["degree_lines"]:
            questions[f"edu{number}_degree"] = (
                "Which option names the degree that was earned (for example Bachelor of Science)?",
                entry["degree_lines"])
    if not questions:
        return {}
    try:
        return jev.choose(resume_text[:_MAX_STATE_CHARS], questions)
    except jev.JevUnavailable as exc:
        logger.warning("jev unavailable during resume parse (%s); using regex candidates", exc.reason)
        return None


def parse_resume(pdf_bytes: bytes) -> dict:
    """Turn a text-PDF resume into a review draft; stored as `settings['profile_draft']` and returned.

    Code finds candidates, one `jev.choose` request picks among them, and values are copied from
    the resume, never generated. Scalars come back as `{value, candidates, confidence, source}`
    with `source` `jev`, `regex_fallback` or `none` (nothing to choose from);
    `work_experience` and `education` are lists in the profile editor's shape (`app.profile.tsx:757`, `:768`) plus a `picks` dict holding the same
    per-field detail for the entry's picked fields. When Jev is unavailable every picked field
    takes its first regex candidate with confidence 0 (an entry's company takes the second header
    line, since the title comes first), so the parse never fails because Jev did. A scanned image
    PDF yields no text: every field comes back empty. Raises ValueError for bytes that are not a
    readable PDF.

    The draft is stored for crash safety and is NOT written into `profile`: formfill and the
    approval snapshot read `profile`, and an unconfirmed pick must never be sent. The PDF itself is
    stored in `data_dir()/resumes` and the draft records it as `resume_path`, so a reopened review can
    confirm without the user choosing the file again (`save_profile`).
    """
    if len(pdf_bytes) > RESUME_MAX_BYTES or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError(RESUME_RULE)

    # 1. PDF to reading-order lines.
    lines = _read_lines(pdf_bytes)
    texts = [line["text"] for line in lines]
    resume_text = "\n".join(texts)

    # 2. Sections: a whole-line heading starts one; "other" headings only end the previous one.
    sections = {key: [] for key, _ in _HEADINGS}
    header_block = []
    current = None
    for text in texts:
        heading = None
        if len(text) <= 40:  # a heading is a short line; longer ones are prose that happens to match
            for key, pattern in _HEADINGS:
                if pattern.fullmatch(text.strip().rstrip(":")):
                    heading = key
        if heading is not None:
            current = heading
        elif current is None:
            header_block.append(text)
        else:
            sections[current].append(text)

    # 3. Scalar candidates by regex.
    def unique(items):
        """Stripped, non-empty, de-duplicated, capped at `_MAX_CANDIDATES` (Jev takes at most 254)."""
        stripped = []
        for item in items:
            if item.strip():
                stripped.append(item.strip())
        return _dedupe(stripped)[:_MAX_CANDIDATES]

    name_lines = [line for line in lines if line["page"] == 0 and line["top"] < 1 / 3
                  and not re.search(r"\d|@|http", line["text"])]
    name_lines.sort(key=lambda line: -line["size"])
    candidates = {
        "full_name": unique(line["text"] for line in name_lines)[:5],
        "email": unique(_EMAIL.findall(resume_text)),
        "phone": unique(_PHONE.findall(resume_text)),
        "linkedin_url": unique(_LINKEDIN.findall(resume_text)),
        "github_url": unique(_GITHUB.findall(resume_text)),
    }
    places = []
    for text in header_block:
        for piece in re.split(r"\s*[|•·]\s*", text):
            places.extend(match.groups() for match in _PLACE.finditer(piece))
    places = _dedupe(places)[:_MAX_CANDIDATES]
    place_names = [f"{city.strip()}, {state}" for city, state in places]
    # Portfolio links come from the header block only (a bare "ASP.NET" in a skills list is not a
    # link). Email addresses are blanked first, otherwise me@site.com would also yield "site.com".
    # LinkedIn and GitHub links have their own fields, so they are left out here.
    header_without_emails = _EMAIL.sub(" ", "\n".join(header_block))
    portfolio = []
    for url in unique(_URL.findall(header_without_emails)):
        if not _LINKEDIN.search(url) and not _GITHUB.search(url):
            portfolio.append(url)

    # 4. Entries: experience and education, with the header lines Jev picks among.
    experience = _split_entries(sections["experience"], _DATE_RANGE)
    for entry in experience:
        cleaned = []
        entry["location"] = ""
        for header in entry["headers"]:
            trailing = _TRAILING_PLACE.match(header)
            if trailing:
                if entry["location"] == "":  # the first place on a header line wins
                    entry["location"] = trailing.group("place")
                header = trailing.group("text")
            cleaned.append(header.strip())
        entry["headers"] = unique(cleaned)
    education = _split_entries(sections["education"], _DATE_OR_RANGE)
    for entry in education:
        entry["headers"] = unique(entry["headers"])
        # The degree usually sits on a header line, or in the first lines under it ("B.S. in CS, GPA 3.8").
        nearby_lines = entry["headers"] + entry["body"][:3]
        entry["degree_lines"] = unique(line for line in nearby_lines if _DEGREE_LINE.search(line))

    # 5. One Jev request for everything.
    answers = _ask_jev(resume_text, candidates, place_names, experience, education)

    # 6. Assemble the draft.
    draft = {key: _pick_field(answers, key, candidates[key]) for key in _FIELD_LABELS}
    # City and state come from one pick: Jev chooses a "City, ST" line and both halves are copied.
    location = _pick_field(answers, "location", place_names)
    # Jev returns the "City, ST" text; look up the (city, state) pair that text was made from.
    place_by_name = dict(zip(place_names, places))
    picked_place = place_by_name.get(location["value"])
    for field, index in (("city", 0), ("state", 1)):
        draft[field] = {
            "value": picked_place[index].strip() if picked_place else None,
            "candidates": [place[index].strip() for place in places],
            "confidence": location["confidence"],
            "source": location["source"],
        }
    draft["portfolio_links"] = {"value": portfolio, "candidates": portfolio, "confidence": None, "source": "regex"}

    skills = []
    for line in sections["skills"]:
        line = _BULLET.sub("", line)
        # "Languages: Python, Go" -> drop the category label, keep the items. A colon late in the
        # line is part of an item, not a label, hence the 30 character limit.
        items = line.split(":", 1)[-1] if ":" in line[:30] else line
        for skill in re.split(r"[,;|•·]", items):
            if 0 < len(skill.strip()) <= 40:
                skills.append(skill.strip())
    draft["skills"] = _dedupe(skills)

    draft["work_experience"] = []
    for number, entry in enumerate(experience):
        title = _pick_field(answers, f"exp{number}_title", entry["headers"], 0)
        company = _pick_field(answers, f"exp{number}_company", entry["headers"], 1)
        start_month, start_year, _ = _split_date(entry["dates"][0])
        end_month, end_year, is_current = _split_date(entry["dates"][1])
        draft["work_experience"].append({
            "title": title["value"] or "", "company": company["value"] or "",
            "location": entry["location"], "employment_type": "",
            "start_month": start_month, "start_year": start_year,
            "end_month": end_month, "end_year": end_year, "current": is_current,
            "description": "\n".join(entry["body"]),
            "picks": {"title": title, "company": company},
        })

    draft["education"] = []
    for number, entry in enumerate(education):
        institution = _pick_field(answers, f"edu{number}_institution", entry["headers"])
        degree = _pick_field(answers, f"edu{number}_degree", entry["degree_lines"])
        gpa = ""
        degree_text = degree["value"] or ""
        for line in entry["headers"] + entry["body"]:
            found = _GPA.search(line)
            if found:
                gpa = found.group(1)
                degree_text = _GPA.sub("", degree_text)
                break
        split = _DEGREE_SPLIT.match(degree_text.strip())
        # A range is start and end. A lone date ("May 2026") is a graduation date, so it is the end.
        first_date, second_date = entry["dates"]
        start_month, start_year, _ = _split_date(first_date if second_date else None)
        end_month, end_year, is_current = _split_date(second_date or first_date)
        draft["education"].append({
            "institution": institution["value"] or "",
            "degree": split.group("degree") if split else degree_text.strip(),
            "field_of_study": split.group("field").strip() if split else "",
            "gpa": gpa,
            "start_month": start_month, "start_year": start_year,
            "end_month": end_month, "end_year": end_year, "current": is_current,
            "picks": {"institution": institution, "degree": degree},
        })

    certifications = []
    for text in sections["certifications"]:
        line = _BULLET.sub("", text).strip()
        if line:
            certifications.append(line)
    draft["certifications"] = _dedupe(certifications)

    draft["projects"] = []
    for line in sections["projects"]:
        # A project starts at a short non-bullet line that is not a sentence; everything else
        # under it (bullets, wrapped text) is its description.
        looks_like_project_header = not _BULLET.match(line) and len(line) <= 80 and not line.endswith(".")
        if looks_like_project_header:
            draft["projects"].append({"name": line.split(" | ")[0].strip(), "description": ""})
        elif draft["projects"]:
            piece = _BULLET.sub("", line).strip()
            joined = f"{draft['projects'][-1]['description']}\n{piece}"
            draft["projects"][-1]["description"] = joined.strip()

    stored_path, _created = _store_resume_file(pdf_bytes)
    draft["resume_path"] = str(stored_path)
    db.set_setting("profile_draft", draft)
    return draft


def save_draft(fields: dict) -> None:
    """Overwrite the stored draft with the review screen's current state (called on every edit).

    A quit or crash before the user confirms then loses nothing. The draft never reaches
    `profile` until `save_profile`. `resume_path` is the server's, set by `parse_resume`: the screen's copy
    is ignored and the stored one carried over, so an edit cannot point the draft at another file.
    """
    fields = {key: value for key, value in fields.items() if key != "resume_path"}
    stored = db.get_setting("profile_draft") or {}
    if stored.get("resume_path"):
        fields["resume_path"] = stored["resume_path"]
    db.set_setting("profile_draft", fields)


# What each consent flag has the user agree to is versioned by the date of its copy: change the copy,
# bump the version, and the audit trail still says which wording a given flag was ticked under. A bump
# also re-asks: `consent_is_current` stops counting a flag ticked under older wording.
CONSENT_TOS_VERSION = "2026-10-28"
BACKGROUND_TOS_VERSION = "2026-10-28"
ARBITRATION_TOS_VERSION = "2026-10-28"
# flag column -> (version column, timestamp column, version constant); `save_consent` is their only writer.
_CONSENT_FLAGS = {
    "auto_consent_authorized": ("consent_tos_version", "consent_authorized_at", CONSENT_TOS_VERSION),
    "background_check_authorized": ("background_check_tos_version", "background_check_authorized_at",
                                    BACKGROUND_TOS_VERSION),
    "arbitration_authorized": ("arbitration_tos_version", "arbitration_authorized_at", ARBITRATION_TOS_VERSION),
}
# Every column `save_profile` must refuse: each flag, then its version column and its timestamp column.
_CONSENT_COLUMNS = frozenset({
    "auto_consent_authorized", "background_check_authorized", "arbitration_authorized",
    "consent_tos_version", "background_check_tos_version", "arbitration_tos_version",
    "consent_authorized_at", "background_check_authorized_at", "arbitration_authorized_at",
})

# `profile` columns stored as JSON text; `get_profile` decodes them back.
_PROFILE_JSON_COLUMNS = ("portfolio_links", "skills", "education", "work_experience", "certifications",
                         "languages", "projects", "work_preferences", "screener_answers")


def get_profile() -> dict | None:
    """The saved profile with its JSON columns decoded, or None before the first `save_profile`.

    Read by the Profile screen and by the app's first-run check (no profile means onboarding).
    The review screen's draft is separate: `get_draft`.
    """
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    profile = dict(row)
    for column in _PROFILE_JSON_COLUMNS:
        profile[column] = json.loads(profile[column])
    return profile


def get_draft() -> dict | None:
    """The review screen's saved state (`parse_resume`'s output as the user edited it), or None.

    Read by onboarding so a reopened app resumes an unfinished review; `save_profile` clears it.
    """
    return db.get_setting("profile_draft")


def _store_resume_file(pdf_bytes: bytes) -> tuple[Path, bool]:
    """Write a resume to `data_dir()/resumes/<sha256>.pdf` (mode 0600) and return `(path, created)`.

    Used by `parse_resume` (the draft's copy) and `save_profile` (a new upload); the same bytes land on the
    same file, so `created` is False when an earlier call already wrote it. The bytes go to a unique temp
    name (`tempfile.mkstemp`) that is then renamed, so a write that dies halfway (disk full) can never
    leave a truncated <sha256>.pdf that the next call with the same bytes would trust as complete, and two
    saves at once never share a temp file. Temp files a crash left behind are removed first, but only
    those older than a minute: a younger one may be another save's write in progress.
    """
    resumes = db.data_dir() / "resumes"
    resumes.mkdir(exist_ok=True)
    if os.name == "posix":
        os.chmod(resumes, 0o700)
    path = resumes / f"{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"
    stray_cutoff = time.time() - 60
    for stray in resumes.glob("*.tmp"):
        try:
            if stray.stat().st_mtime < stray_cutoff:
                stray.unlink()
        except FileNotFoundError:
            pass    # the save that owned it just renamed it
    if path.exists():
        return path, False
    descriptor, partial = tempfile.mkstemp(dir=resumes, suffix=".tmp")    # created with mode 0600
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pdf_bytes)
    os.replace(partial, path)
    return path, True


def save_profile(fields: dict, resume_bytes: bytes | None = None) -> None:
    """Write the confirmed profile and its resume; called only after the user confirms the review.

    With `resume_bytes=None` the stored resume is kept, so a phone number can change without uploading
    the PDF again (the Profile screen). A first save has none to keep, so it uses the PDF
    `parse_resume` stored for the draft (`settings['profile_draft']['resume_path']`), which is how a
    reopened review confirms without the file; with neither it raises ValueError.

    `fields` keys must be `profile` columns other than `id` and the two timestamps, and must include a non-empty `email`
    and `full_name` (both NOT NULL), else ValueError before anything is written. List and dict
    values are stored as JSON text. The PDF goes to `data_dir()/resumes/<sha256>.pdf` with mode
    0600 (it holds the same PII as the database file), `resume_path` is set to it, row `id = 1` is
    upserted with only the given columns and `settings['profile_draft']` is deleted in one
    transaction, and the previous resume file is removed only after the commit, unless an approved,
    unfinished application still needs it (`sweep_resumes`). If the
    transaction fails the draft is kept and a file this call created is removed.
    """
    conn = db.get_db()
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile)")} - {"id", "created_at", "updated_at"}
    finally:
        conn.close()
    unknown = sorted(set(fields) - columns)
    if unknown:
        raise ValueError(f"unknown profile fields: {unknown}")
    consent_fields = sorted(set(fields) & _CONSENT_COLUMNS)
    if consent_fields:
        raise ValueError(f"consent fields are saved with save_consent, not here: {consent_fields}")
    for required in ("email", "full_name"):
        if not str(fields.get(required) or "").strip():
            raise ValueError(f"{required} is required")
    new_path = None
    created = False
    if resume_bytes is None:
        # The resume is never a profile field: only a new upload moves `resume_path`.
        fields = {column: value for column, value in fields.items() if column != "resume_path"}
        conn = db.get_db()
        try:
            kept = conn.execute("SELECT resume_path FROM profile WHERE id = 1").fetchone()
        finally:
            conn.close()
        if kept is None or not kept["resume_path"]:
            draft = db.get_setting("profile_draft") or {}
            draft_resume = Path(draft.get("resume_path") or "")
            # Only a file `parse_resume` put in the resumes folder counts; the draft is otherwise free text.
            if draft_resume.parent != db.data_dir() / "resumes" or not draft_resume.is_file():
                raise ValueError("a resume is required")
            new_path = draft_resume
    elif len(resume_bytes) > RESUME_MAX_BYTES or not resume_bytes.startswith(b"%PDF-"):
        raise ValueError(RESUME_RULE)

    values = {}
    for column, value in fields.items():
        if isinstance(value, list):
            # The parse draft's entries carry a `picks` dict for the review screen; the stored
            # profile keeps the editor's shape only.
            entries = []
            for entry in value:
                if isinstance(entry, dict):
                    entry = {key: item for key, item in entry.items() if key != "picks"}
                entries.append(entry)
            value = json.dumps(entries)
        elif isinstance(value, dict):
            value = json.dumps(value)
        elif isinstance(value, bool):
            value = int(value)
        values[column] = value
    if resume_bytes is not None:
        # After `values` is built, so a field that cannot be encoded as JSON raises before any file exists.
        new_path, created = _store_resume_file(resume_bytes)
    if new_path is not None:
        values["resume_path"] = str(new_path)

    names = list(values)
    placeholders = ", ".join("?" for _ in names)
    assignments = ", ".join(f"{name} = excluded.{name}" for name in names)

    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT INTO profile (id, {', '.join(names)}) VALUES (1, {placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {assignments}, "
            f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            [values[name] for name in names],
        )
        conn.execute("DELETE FROM settings WHERE key = 'profile_draft'")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if created:
            new_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    sweep_resumes()


def save_consent(flags: dict) -> None:
    """Set the consent flags the user ticked on the Permissions step or the Profile screen.

    `flags` holds exactly the three keys of `_CONSENT_FLAGS`, as booleans; any other key, or no profile row
    yet, raises ValueError before anything is written. A flag going 0 -> 1 also stamps its version constant
    and the current time, which `local_formfill` reads into the fill audit as the evidence of what was agreed.
    A flag saved true that is already 1 but stamped with an older version is stamped again: the user ticked it
    under the wording now on screen, so that is agreeing to the new text (`consent_is_current`).
    A flag going 1 -> 0 clears only itself and keeps the version and time as the record of what was once
    agreed; any other flag saved at the value it already has changes nothing, so its time stays the first
    agreement.
    All of it is one transaction, and no other function writes these nine columns (`save_profile` refuses
    them), so the evidence cannot be recorded two ways.
    """
    unknown = sorted(set(flags) - set(_CONSENT_FLAGS))
    if unknown:
        raise ValueError(f"unknown consent flags: {unknown}")
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("save the profile first")
        for flag, wanted in flags.items():
            version_column, stamp_column, version = _CONSENT_FLAGS[flag]
            wanted_value = 1 if wanted else 0
            stale = wanted and row[flag] == 1 and row[version_column] != version
            if wanted_value == row[flag] and not stale:
                continue
            # The f-strings below take flag and column names only from `_CONSENT_FLAGS`; `flag` was checked
            # against it above, so nothing from the request reaches the SQL text.
            if wanted:
                conn.execute(
                    f"UPDATE profile SET {flag} = 1, {version_column} = ?, "
                    f"{stamp_column} = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = 1",
                    (version,),
                )
            else:
                conn.execute(f"UPDATE profile SET {flag} = 0 WHERE id = 1")
        conn.execute("UPDATE profile SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = 1")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def consent_is_current(profile: dict, flag: str) -> bool:
    """Whether consent `flag` is set AND was agreed under the wording now in force.

    Read by `local_formfill._consent_values`, which ticks nothing on a stale agreement, and by
    `local_batch._inputs`, so the Approve summary shows only what the filler will use. A flag ticked
    under older copy is not an agreement to the current copy; `save_consent` re-stamps it when the user
    ticks it again. `profile` is a profile dict (live or a batch snapshot's).
    """
    version_column, _stamp_column, version = _CONSENT_FLAGS[flag]
    return bool(profile.get(flag)) and profile.get(version_column) == version


def sweep_resumes(min_age_s: float = 0) -> int:
    """Delete resume files nothing needs any more; returns how many.

    Called by `save_profile` after its commit and by `local_queues.run_all` every few minutes. A file
    in `data_dir()/resumes` is kept while it is the profile's current resume or the snapshot of an
    approval batch still has an unfinished application: pending, running, submitting or
    blocked, because the worker attaches the snapshot's resume, not the live profile's. The file the
    current draft points at (`parse_resume`) is kept too, so a reopened review can still confirm. A new resume
    saved mid-batch therefore leaves the old file in place until the last of those applications settles.
    `min_age_s` spares files modified more recently than that: the periodic caller passes 60 because a
    concurrent `save_profile` writes its PDF before it commits the row that points at it. `save_profile`
    itself calls with 0, after its own commit.
    """
    resumes = db.data_dir() / "resumes"
    if not resumes.is_dir():
        return 0
    conn = db.get_db()
    try:
        profile_row = conn.execute("SELECT resume_path FROM profile WHERE id = 1").fetchone()
        # Plain SQL: the snapshots of batches that still have an unfinished application.
        #   approval_batches WHERE EXISTS an application in that batch not yet applied, failed or cancelled
        snapshots = conn.execute(
            "SELECT snapshot FROM approval_batches b WHERE EXISTS ("
            "  SELECT 1 FROM job_applications a WHERE a.approval_batch_id = b.id "
            "  AND a.status IN ('pending', 'in_progress', 'submitting', 'blocked_needs_input'))").fetchall()
    finally:
        conn.close()

    keep = set()
    if profile_row is not None and profile_row["resume_path"]:
        keep.add(Path(profile_row["resume_path"]))
    draft = db.get_setting("profile_draft") or {}
    if draft.get("resume_path"):
        keep.add(Path(draft["resume_path"]))
    for row in snapshots:
        resume = json.loads(row["snapshot"]).get("resume")
        if resume:
            keep.add(Path(resume["path"]))

    recent = time.time() - min_age_s
    deleted = 0
    for path in resumes.glob("*.pdf"):
        if path in keep:
            continue
        try:
            modified = path.stat().st_mtime
        except FileNotFoundError:
            continue    # another sweep deleted it between the listing and here
        if modified <= recent:
            path.unlink(missing_ok=True)
            deleted += 1
    return deleted


# normalized question -> shape, for `list_global_questions` and `save_global_answer`.
_GLOBAL_SHAPES = {_normalize_question(question): shape for question, _, shape in GLOBAL_SEED}


def seed_global_questions() -> None:
    """Insert the `GLOBAL_SEED` rows unanswered; rows already present (answered or not) are kept.

    Called by onboarding. The global partial unique index on `normalized_question`
    (0001_init.sql) makes a second call a no-op.
    """
    conn = db.get_db()
    try:
        for question, question_type, _ in GLOBAL_SEED:
            conn.execute(
                "INSERT OR IGNORE INTO application_qna_bank "
                "(original_question, normalized_question, question_type, scope, is_answered) "
                "VALUES (?, ?, ?, 'global', 0)",
                (question, _normalize_question(question), question_type),
            )
    finally:
        conn.close()


def list_global_questions() -> list[dict]:
    """The global rows of `application_qna_bank` as `{normalized_question, question, question_type, field_shape, answer}`.

    `answer` is None until `save_global_answer`. `field_shape` is the seed's shape for that question,
    else `GLOBAL_TEXT_SHAPE`. Read by onboarding and Settings so a reopened app shows what was
    answered, with the right control; ordered as seeded (`GLOBAL_SEED`).
    """
    conn = db.get_db()
    try:
        rows = conn.execute(
            "SELECT normalized_question, original_question, question_type, answer_value, is_answered "
            "FROM application_qna_bank WHERE scope = 'global' ORDER BY id").fetchall()
    finally:
        conn.close()
    questions = []
    for row in rows:
        questions.append({
            "normalized_question": row["normalized_question"], "question": row["original_question"],
            "question_type": row["question_type"],
            "field_shape": _GLOBAL_SHAPES.get(row["normalized_question"], GLOBAL_TEXT_SHAPE),
            "answer": row["answer_value"] if row["is_answered"] else None,
        })
    return questions


def save_global_answer(normalized_question: str, value: str) -> None:
    """Store the user's answer on a global row and mark it answered; ValueError for an unknown question.

    The value must fit the question's shape (`GLOBAL_SEED`, else `GLOBAL_TEXT_SHAPE`): one of the
    options for `single`, digits only for `number`, at most `max_length` characters for `text`. The
    API maps the ValueError to a 400 whose message the onboarding screen shows. An answer saved before
    a shape existed is not re-checked; it stays until the user changes it.
    """
    shape = _GLOBAL_SHAPES.get(normalized_question, GLOBAL_TEXT_SHAPE)
    if shape["kind"] == "single" and value not in shape["options"]:
        raise ValueError(f"choose one of: {', '.join(shape['options'])}")
    if shape["kind"] == "number" and not (value.isascii() and value.isdigit()):
        raise ValueError("enter whole US dollars per year, digits only (for example 120000)")
    if shape["kind"] == "text" and len(value) > shape["max_length"]:
        raise ValueError(f"answers can be at most {shape['max_length']} characters; yours is {len(value)}")
    conn = db.get_db()
    try:
        cursor = conn.execute(
            "UPDATE application_qna_bank SET answer_value = ?, is_answered = 1, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE scope = 'global' AND normalized_question = ?",
            (value, normalized_question),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"no global question {normalized_question!r}")
    finally:
        conn.close()


def build_prepared() -> int:
    """Create a `prepared` `job_applications` row for every active listing that has none; returns how many.

    Called after `ingest_run` finishes and before the approval screen. Listings go
    oldest `posted_at` first, and each is compared with the listings already applied to
    (`_APPLIED_STATUSES`) plus the ones prepared earlier in this call, so two copies in one batch
    are never both unflagged. The evidence is stored on the row for the approval screen:
    - strong: an earlier listing has the same `repost_key` (the same apply URL). Only the same URL
      proves the same posting; same company, title and location also fits adjacent teams hiring
      for one role.
    - weak: an earlier listing within 12 months has the same `company_key` and the same
      fingerprint (hash of title, location, arrangement, type, years, visa, pay, skills and
      education). The prose summaries are left out because a repost's new job id gets a fresh
      actor summary that rarely matches word for word. No fingerprint without `key_skills`: the
      hash would shrink to title and location and flag adjacent teams.
    """
    conn = db.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Listings that already have an application in an "applied before" status, in the same
        # columns as the new ones so one loop handles both.
        #   listings JOIN job_applications ON job_applications.listing_id = listings.id
        status_placeholders = ", ".join("?" * len(_APPLIED_STATUSES))
        applied_before = conn.execute(
            "SELECT listings.* FROM listings "
            "JOIN job_applications ON job_applications.listing_id = listings.id "
            f"WHERE job_applications.status IN ({status_placeholders}) "
            "ORDER BY listings.posted_at, listings.id",
            _APPLIED_STATUSES,
        ).fetchall()
        # Active listings with no application row yet (the LEFT JOIN finds no match).
        #   listings LEFT JOIN job_applications ON job_applications.listing_id = listings.id
        new_listings = conn.execute(
            "SELECT listings.* FROM listings "
            "LEFT JOIN job_applications ON job_applications.listing_id = listings.id "
            "WHERE listings.is_active = 1 AND job_applications.id IS NULL "
            "ORDER BY listings.posted_at, listings.id"
        ).fetchall()

        new_ids = set()
        for listing in new_listings:
            new_ids.add(listing["id"])
        by_repost_key = {}
        by_fingerprint = {}
        created = 0
        for listing in applied_before + new_listings:
            posted = datetime.datetime.fromisoformat(listing["posted_at"])
            fingerprint = None
            skills = json.loads(listing["key_skills"] or "[]")
            if skills:
                education = json.loads(listing["education"] or "[]")
                # The fingerprint: the listing's own attributes joined with "|" (a missing one is
                # empty), with skills and education lowercased and sorted so their order and case
                # in the actor's output do not matter.
                skills_text = ",".join(sorted(skill.lower() for skill in skills))
                education_text = ",".join(sorted(entry.lower() for entry in education))
                parts = [listing["normalized_title"], listing["location"], listing["work_arrangement"],
                         listing["employment_type"], listing["experience_years"], listing["visa_sponsorship"],
                         listing["salary_min"], listing["salary_max"], skills_text, education_text]
                joined = "|".join("" if part is None else str(part) for part in parts)
                fingerprint = hashlib.sha256(joined.encode()).hexdigest()
            fingerprint_key = (listing["company_key"], fingerprint)

            if listing["id"] in new_ids:
                repost_match = None
                repost_of = by_repost_key.get(listing["repost_key"])
                if repost_of is not None:
                    repost_match = "strong"
                elif fingerprint is not None:
                    for earlier_posted, earlier_id in by_fingerprint.get(fingerprint_key, []):
                        if abs(posted - earlier_posted) <= _WEAK_WINDOW:
                            repost_match = "weak"
                            repost_of = earlier_id
                conn.execute(
                    "INSERT INTO job_applications (listing_id, ats, status, repost_match, repost_of_listing_id) "
                    "VALUES (?, ?, 'prepared', ?, ?)",
                    (listing["id"], listing["source"], repost_match, repost_of if repost_match else None),
                )
                created += 1

            if listing["repost_key"] is not None:
                by_repost_key[listing["repost_key"]] = listing["id"]
            if fingerprint is not None:
                if fingerprint_key not in by_fingerprint:
                    by_fingerprint[fingerprint_key] = []
                by_fingerprint[fingerprint_key].append((posted, listing["id"]))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return created
