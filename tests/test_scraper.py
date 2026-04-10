"""
Tests for PlaywrightScraperProvider / ReplayScraperProvider.

All tests run offline via the fixture at tests/fixtures/scraper/.
Network is blocked by conftest.py's block_network fixture.
"""
import json
import re
from pathlib import Path

import pytest

from providers.replay import ReplayScraperProvider
from providers.playwright_scraper import _FACULTY_RE, _is_faculty, _parse_html

_SCHOOL = "Stanford"
_DISCIPLINE = "Pediatric Endocrinology"
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "scraper" / "stanford_pediatric_endocrinology.json"

_REQUIRED_FIELDS = {"full_name", "title", "profile_url", "school"}

# Known faculty that must appear in the fixture
_REQUIRED_NAMES = {
    "Tandy Aye",
    "David Maahs",
    "Anna L Gloyn",
    "Holly Cooper",
    "Darrell Wilson",
    "Bruce Buckingham",
}

# Staff/trainees that must NOT appear after filtering
_EXCLUDED_NAMES = {
    "Bonita Baker",           # Clinical Research Coordinator
    "TAMADHER ALGHUNAIM",     # Postdoctoral Scholar
    "Franziska K Bishop",     # Academic Prog Prof 3 (staff)
    "Erlynn Dela Cruz",       # Administrative Associate
    "Christopher Chow-Parmer",  # Fellow in Pediatrics (trainee)
}


# ── ReplayScraperProvider ─────────────────────────────────────────────────────

def test_fixture_exists():
    assert _FIXTURE_PATH.exists(), (
        f"Fixture missing: {_FIXTURE_PATH}\n"
        "Run: python -m tools.capture_fixture_scraper "
        "--school Stanford --discipline 'Pediatric Endocrinology' "
        "--out tests/fixtures/scraper/stanford_pediatric_endocrinology.json"
    )


def test_replay_provider_returns_list():
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    assert isinstance(faculty, list)
    assert len(faculty) > 0


def test_replay_provider_count():
    """Fixture should have a reasonable faculty count (not empty, not inflated)."""
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    assert 20 <= len(faculty) <= 60, (
        f"Expected 20-60 faculty, got {len(faculty)}. "
        "Check fixture filtering logic."
    )


def test_required_fields_present():
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    failures = []
    for record in faculty:
        missing = _REQUIRED_FIELDS - set(record.keys())
        if missing:
            failures.append(f"{record.get('full_name', '?')}: missing {missing}")
    assert not failures, "\n".join(failures)


def test_school_field_populated():
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    assert all(f["school"] == _SCHOOL for f in faculty)


def test_profile_urls_are_stanford():
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    bad_urls = [
        f["full_name"]
        for f in faculty
        if f.get("profile_url") and "profiles.stanford.edu" not in f["profile_url"]
    ]
    assert not bad_urls, f"Non-Stanford profile URLs: {bad_urls}"


def test_required_names_present():
    provider = ReplayScraperProvider()
    names = {f["full_name"] for f in provider.get_faculty(_SCHOOL, _DISCIPLINE)}
    missing = _REQUIRED_NAMES - names
    assert not missing, f"Required faculty missing from fixture: {missing}"


def test_excluded_names_absent():
    provider = ReplayScraperProvider()
    names = {f["full_name"] for f in provider.get_faculty(_SCHOOL, _DISCIPLINE)}
    present = _EXCLUDED_NAMES & names
    assert not present, f"Non-faculty entries leaked into fixture: {present}"


def test_all_titles_are_faculty_level():
    provider = ReplayScraperProvider()
    faculty = provider.get_faculty(_SCHOOL, _DISCIPLINE)
    non_faculty = [f for f in faculty if not _is_faculty(f.get("title", ""))]
    assert not non_faculty, (
        "Non-faculty titles found in fixture:\n"
        + "\n".join(f"  {f['full_name']}: {f['title']}" for f in non_faculty)
    )


# ── _is_faculty filter unit tests ─────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Assistant Professor of Pediatrics (Endocrinology)", True),
    ("Clinical Associate Professor, Pediatrics - Endocrinology", True),
    ("Professor of Pediatrics (Endocrinology) at the Lucile Salter Packard Children's Hospital, Emeritus", True),
    ("Adjunct Clinical Assistant Professor, Pediatrics - Endocrinology", True),
    ("Instructor, Pediatrics - Endocrinology", True),
    ("Lucile Salter Packard Professor of Pediatrics", True),
    # Should be excluded:
    ("Academic Prog Prof 3, Pediatrics - Endocrinology", False),
    ("Postdoctoral Scholar, Endocrinology and Metabolism", False),
    ("Clinical Research Coordinator, Pediatrics - Endocrinology", False),
    ("Administrative Associate, Pediatrics - Endocrinology", False),
    ("Life Science Research Professional, Pediatrics - Endocrinology", False),
    ("Clinical Rsch Coord Assoc, Pediatrics - Endocrinology", False),
    ("Affiliate, Pediatrics - Endocrinology", False),
    ("Biostatistician 2, Pediatrics - Endocrinology", False),
])
def test_is_faculty_filter(title: str, expected: bool):
    assert _is_faculty(title) == expected, (
        f"_is_faculty({title!r}) should be {expected}"
    )


# ── _parse_html unit test ─────────────────────────────────────────────────────

_MINIMAL_HTML = """
<html><body>
  <ul>
    <li class="mini-profile-holder">
      <div class="mini-profile">
        <a href="/david-maahs"><h4>David Maahs</h4><h5>Professor of Pediatrics</h5></a>
      </div>
    </li>
    <li class="mini-profile-holder">
      <div class="mini-profile">
        <a href="/bonita-baker"><h4>Bonita Baker</h4><h5>Clinical Research Coordinator</h5></a>
      </div>
    </li>
    <li class="mini-profile-holder">
      <div class="mini-profile">
        <a href="/holly-cooper"><h4>Holly Cooper</h4><h5>Clinical Assistant Professor, Pediatrics</h5></a>
      </div>
    </li>
  </ul>
</body></html>
"""


def test_parse_html_filters_correctly():
    records = _parse_html(_MINIMAL_HTML, "Stanford")
    names = [r["full_name"] for r in records]
    assert "David Maahs" in names
    assert "Holly Cooper" in names
    assert "Bonita Baker" not in names, "Coordinator should be filtered out"


def test_parse_html_profile_url_format():
    records = _parse_html(_MINIMAL_HTML, "Stanford")
    for r in records:
        assert r["profile_url"].startswith("https://profiles.stanford.edu/"), (
            f"Bad profile URL: {r['profile_url']}"
        )


def test_parse_html_school_set():
    records = _parse_html(_MINIMAL_HTML, "Stanford")
    assert all(r["school"] == "Stanford" for r in records)
