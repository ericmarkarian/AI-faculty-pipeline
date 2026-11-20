"""
A roster a person pasted in, for directory pages the pipeline cannot read itself.

Some sites refuse the pipeline's browser (see DirectoryBlocked). The pipeline reports
that and does not try to get around it. A person can still open the page, copy it and
paste it into the app; this module turns that paste into a roster.

Two halves:
  * read_pasted_roster() — pasted page -> people. The same text conversion, extraction
    and section screening a scraped page goes through, so fellows and staff are left
    out the same way.
  * clean_pasted_rosters() — rosters the browser sends back with a later run. The app
    keeps them in the visitor's own browser, not on the server (the server's disk does
    not survive a deploy, and the repo is published), so they arrive as untrusted
    input and are checked field by field before the pipeline uses them.
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from providers.faculty_merge import SOURCE_HOSPITAL, SOURCE_PASTED, SOURCE_UNIVERSITY, SOURCE_UNKNOWN, dedupe_by_name, to_record
from providers.roster_reading import count_by_category, screen_roster

MAX_PASTE_CHARS = 3_000_000   # a whole page copied with its formatting is a few hundred KB
MAX_PEOPLE = 500
STALE_AFTER_DAYS = 180        # rosters change slowly, but not never

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAGE_TYPES = (SOURCE_UNIVERSITY, SOURCE_HOSPITAL, SOURCE_UNKNOWN)


def _clean_people(items: Any) -> list[dict]:
    """Normalise to {"full_name", "title", "profile_url"}; drop junk, repeats and overflow."""
    people: list[dict] = []
    for item in items if isinstance(items, list) else []:
        record = to_record(item, "", SOURCE_PASTED)
        if record is None or len(record["full_name"]) > 200:
            continue
        title = record.get("title")
        url = record.get("profile_url")
        people.append({
            "full_name": record["full_name"],
            "title": title[:300] if title else None,
            "profile_url": url if url and url.startswith(("http://", "https://")) and len(url) <= 500 else None,
        })
    return dedupe_by_name(people)[:MAX_PEOPLE]


def read_pasted_roster(html: str, text: str, url: str, school: str, discipline: str,
                       client, model: str = "gpt-4o") -> dict:
    """Extract the faculty listed on a pasted page.

    html — the clipboard's formatted copy of the page, when the browser provided one.
           It keeps headings and profile links, which plain text loses.
    text — the plain-text copy, used when there is no usable formatted one.

    Returns {"people", "left_out", "chars", "used_formatting"}. Raises ValueError when
    nothing readable was pasted and RuntimeError when the model could not be reached.
    """
    from providers import generic_faculty_scraper as gfs  # deferred: gfs is heavy to import

    plain = (text or "").strip()
    page_text = gfs._html_to_text(html, base_url=url or "") if (html or "").strip() else ""
    used_formatting = True
    if len(page_text) < 0.5 * len(plain):
        # No formatted copy, or it lost most of the page (some sites copy as an image map).
        page_text, used_formatting = plain, False
    if not page_text.strip():
        raise ValueError("Nothing readable was pasted.")

    errors: list[str] = []
    extracted = gfs._extract_via_gpt4o(page_text, school, discipline, client, model, errors=errors)
    kept, left_out = screen_roster(extracted, page_text)
    people = _clean_people(kept)
    if not people and errors:
        raise RuntimeError(errors[0])
    return {
        "people": people,
        "left_out": count_by_category(left_out),
        "chars": len(page_text),
        "used_formatting": used_formatting,
    }


def is_stale(captured: str | None, today: datetime.date | None = None) -> bool:
    """True when a pasted roster is old enough that the page should be pasted again."""
    if not captured:
        return False
    try:
        taken = datetime.date.fromisoformat(captured)
    except ValueError:
        return False
    return ((today or datetime.date.today()) - taken).days > STALE_AFTER_DAYS


def clean_pasted_rosters(raw: Any) -> list[dict]:
    """Validate rosters sent by the browser; anything malformed is dropped, never raised.

    Each kept roster is {"school", "discipline", "url", "type", "captured", "people"}.
    """
    rosters: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        school, discipline, url = entry.get("school"), entry.get("discipline"), entry.get("url")
        if not all(isinstance(v, str) and v.strip() for v in (school, discipline, url)):
            continue
        if not url.startswith(("http://", "https://")) or len(url) > 1000:
            continue
        people = _clean_people(entry.get("people"))
        if not people:
            continue
        captured = entry.get("captured")
        page_type = entry.get("type")
        rosters.append({
            "school": school.strip()[:300],
            "discipline": discipline.strip()[:200],
            "url": url.strip(),
            "type": page_type if page_type in _PAGE_TYPES else SOURCE_UNKNOWN,
            "captured": captured if isinstance(captured, str) and _DATE_RE.match(captured) else None,
            "people": people,
        })
    return rosters[:200]
