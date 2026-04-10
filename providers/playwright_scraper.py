"""
PlaywrightScraperProvider — scrapes faculty directly from department directory pages.

Replaces Agent 1's GPT-4o web_search_preview call with deterministic HTML parsing.
Uses requests + BeautifulSoup against server-side-rendered Stanford Profiles pages.

Browse URL pattern:
  https://profiles.stanford.edu/browse/{school-path}/{division}?p=1&ps=100
"""
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from providers.base import ScraperProvider, FacultyRecord

# Maps (school, discipline) -> Stanford Profiles browse URL (ps=100 fetches all at once)
_BROWSE_URL_MAP: dict[tuple[str, str], str] = {
    ("Stanford", "Pediatric Endocrinology"):
        "https://profiles.stanford.edu/browse/school-of-medicine/pediatrics/endocrinology?p=1&ps=100",
}

# Faculty appointments contain "professor", "instructor", or "lecturer" as full words.
# "Academic Prog Prof 3" uses the abbreviation "Prof", not the full word — correctly excluded.
_FACULTY_RE = re.compile(r"\b(professor|instructor|lecturer)\b", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FacultyPipelineBot/1.0; "
        "+https://github.com/your-org/ai-faculty-pipeline)"
    )
}


def _is_faculty(title: str) -> bool:
    return bool(_FACULTY_RE.search(title))


def _parse_html(html: str, school: str) -> list[FacultyRecord]:
    """Parse Stanford Profiles search result HTML into FacultyRecord list."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[FacultyRecord] = []

    for holder in soup.select("li.mini-profile-holder"):
        name_el = holder.select_one("h4")
        title_el = holder.select_one("h5")
        link_el = holder.select_one("a")

        if not name_el or not title_el:
            continue

        full_name = name_el.get_text(strip=True)
        title = title_el.get_text(strip=True)
        profile_url = (
            "https://profiles.stanford.edu" + link_el["href"]
            if link_el and link_el.get("href")
            else None
        )

        if not _is_faculty(title):
            continue

        records.append(
            FacultyRecord(
                full_name=full_name,
                title=title,
                profile_url=profile_url,
                school=school,
                subspecialty=None,
                email=None,
            )
        )

    return records


class PlaywrightScraperProvider(ScraperProvider):
    """Live provider — fetches and parses the department directory page."""

    def __init__(self, request_delay: float = 1.0) -> None:
        self._delay = request_delay

    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        url = _BROWSE_URL_MAP.get((school, discipline))
        if not url:
            raise ValueError(
                f"No browse URL configured for ({school!r}, {discipline!r}). "
                f"Add an entry to _BROWSE_URL_MAP in providers/playwright_scraper.py."
            )

        time.sleep(self._delay)
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()

        records = _parse_html(resp.text, school)
        print(
            f"  [Scraper] {school} {discipline}: "
            f"found {len(records)} faculty via {url}"
        )
        return records
