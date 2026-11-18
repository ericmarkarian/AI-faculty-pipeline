"""
Tests for GenericFacultyScraper source orchestration.

The behavior under test: Agent 1 runs BOTH the Playwright directory scrape and
the GPT-4o web search on every school and unions the results, instead of using
web search only as a fallback when the browser fetch fails. Playwright, OpenAI
and the network are all stubbed out.
"""
import pytest

from providers import generic_faculty_scraper as gfs
from providers.faculty_merge import SOURCE_UNIVERSITY, SOURCE_WEB_SEARCH

_SCHOOL = "Stanford University School of Medicine"
_DISCIPLINE = "Pediatric Endocrinology"


@pytest.fixture
def scraper(monkeypatch, tmp_path):
    """A scraper with one configured university URL and no OpenAI client work."""
    monkeypatch.setattr(gfs.OpenAI, "__init__", lambda self, **kw: None)
    instance = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    instance._urls = {_DISCIPLINE: {_SCHOOL: {"university_url": "https://med.stanford.edu/peds"}}}
    return instance


def _stub_sources(monkeypatch, *, html_names=(), web_names=(), html_error=None, web_error=None):
    """Stub the Playwright fetch + both extraction paths with fixed rosters."""
    calls: dict[str, int] = {"html": 0, "extract": 0, "web": 0}

    def fake_fetch(url):
        calls["html"] += 1
        if html_error:
            raise html_error
        return "<html><body>directory</body></html>"

    def fake_extract(text, school, discipline, client, model="gpt-4o"):
        calls["extract"] += 1
        return [{"full_name": n, "title": "Professor"} for n in html_names]

    def fake_web(school, discipline, client):
        calls["web"] += 1
        if web_error:
            raise web_error
        return [{"full_name": n, "title": "Professor"} for n in web_names]

    monkeypatch.setattr(gfs, "_fetch_html_playwright", fake_fetch)
    monkeypatch.setattr(gfs, "_extract_via_gpt4o", fake_extract)
    monkeypatch.setattr(gfs, "_extract_via_web_search", fake_web)
    monkeypatch.setattr(gfs, "_pagination_urls", lambda html, url: [])
    return calls


@pytest.mark.unit
def test_runs_both_sources_and_unions_results(scraper, monkeypatch):
    calls = _stub_sources(monkeypatch, html_names=["Jordan Vega"], web_names=["Casey Morgan"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert calls["html"] == 1 and calls["web"] == 1
    assert {r["full_name"] for r in records} == {"Jordan Vega", "Casey Morgan"}


@pytest.mark.unit
def test_web_search_runs_even_when_directory_scrape_succeeds(scraper, monkeypatch):
    """The old behavior — web search only on failure — is what caused batch/manual drift."""
    calls = _stub_sources(monkeypatch, html_names=["Jordan Vega"], web_names=[])
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["web"] == 1


@pytest.mark.unit
def test_overlapping_people_are_deduplicated(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Riley L. Chen"], web_names=["Riley Chen, PhD"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert len(records) == 1
    assert records[0]["full_name"] == "Riley L. Chen"
    assert records[0]["discovery_source"] == f"{SOURCE_UNIVERSITY}+{SOURCE_WEB_SEARCH}"


@pytest.mark.unit
def test_web_search_results_survive_a_failed_directory_fetch(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_error=RuntimeError("browser crashed"),
                  web_names=["Casey Morgan"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert [r["full_name"] for r in records] == ["Casey Morgan"]


@pytest.mark.unit
def test_directory_results_survive_a_failed_web_search(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Jordan Vega"], web_error=RuntimeError("rate limited"))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert [r["full_name"] for r in records] == ["Jordan Vega"]


@pytest.mark.unit
def test_web_search_runs_when_no_directory_url_is_known(scraper, monkeypatch):
    """No configured or discoverable URL used to mean zero faculty; now web search still runs."""
    calls = _stub_sources(monkeypatch, web_names=["Casey Morgan"])
    monkeypatch.setattr(scraper, "_get_url_sources", lambda school, discipline: [])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["html"] == 0 and calls["web"] == 1
    assert [r["full_name"] for r in records] == ["Casey Morgan"]


@pytest.mark.unit
def test_web_search_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(gfs.OpenAI, "__init__", lambda self, **kw: None)
    instance = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"),
                                         request_delay=0.0, use_web_search=False)
    instance._urls = {_DISCIPLINE: {_SCHOOL: {"university_url": "https://med.stanford.edu/peds"}}}
    calls = _stub_sources(monkeypatch, html_names=["Jordan Vega"], web_names=["Casey Morgan"])

    records = instance.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["web"] == 0
    assert [r["full_name"] for r in records] == ["Jordan Vega"]


@pytest.mark.unit
def test_records_carry_school_and_discovery_source(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Jordan Vega"], web_names=["Casey Morgan"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert all(r["school"] == _SCHOOL for r in records)
    assert {r["discovery_source"] for r in records} == {SOURCE_UNIVERSITY, SOURCE_WEB_SEARCH}


# ── web-search response parsing ───────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected_names", [
    ('[{"full_name": "Jordan Vega"}]', ["Jordan Vega"]),
    ('```json\n[{"full_name": "Jordan Vega"}]\n```', ["Jordan Vega"]),
    ('Here you go:\n[{"full_name": "Jordan Vega"}]', ["Jordan Vega"]),
    # truncated output — keep the complete prefix instead of losing the whole roster
    ('[{"full_name": "Jordan Vega"}, {"full_name": "Casey Ma', ["Jordan Vega"]),
    ("no json at all", []),
    ("", []),
])
def test_parse_faculty_array_is_tolerant(raw, expected_names):
    items = gfs._parse_faculty_array(raw)
    assert [i["full_name"] for i in items] == expected_names


# ── blocked / empty directory pages ───────────────────────────────────────────

_ROSTER_TEXT = "Jordan Vega, MD Professor of Pediatrics View profile " * 120  # ~6,000 chars


@pytest.mark.unit
@pytest.mark.parametrize("status,text,blocked", [
    (200, _ROSTER_TEXT, False),
    (None, _ROSTER_TEXT, False),                                   # status unknown (timeout) — judge by content
    (403, "403 Forbidden Error 54113 Varnish cache server", True),
    (200, "Just a moment...", True),                               # Cloudflare challenge served with 200
    (None, "Attention Required! | Cloudflare Sorry, you have been blocked", True),
    (404, "Page not found. Try searching instead. " * 100, True),  # a long 404 page is still not a roster
    (500, "Something went wrong", True),
    (403, _ROSTER_TEXT, False),                                    # challenge cleared, real page rendered
    (200, _ROSTER_TEXT + " Visitors without a badge are denied access denied entry.", False),
])
def test_block_reason_separates_block_pages_from_rosters(status, text, blocked):
    assert bool(gfs._block_reason(status, text)) is blocked


@pytest.mark.unit
def test_blocked_directory_is_reported_and_web_search_still_contributes(scraper, monkeypatch):
    """A block page used to parse to "zero faculty" and vanish; now the school is flagged."""
    _stub_sources(monkeypatch, html_error=gfs.DirectoryBlocked("HTTP 403"), web_names=["Casey Morgan"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert [r["full_name"] for r in records] == ["Casey Morgan"]
    report = scraper.last_report
    assert report["school"] == _SCHOOL
    assert report["web_search_found"] == 1
    assert report["sources"] == [{
        "type": SOURCE_UNIVERSITY, "url": "https://med.stanford.edu/peds",
        "status": "blocked", "found": 0, "detail": "HTTP 403",
    }]


@pytest.mark.unit
def test_directory_page_listing_nobody_is_reported_as_empty(scraper, monkeypatch):
    """An overview page with no names loads fine — the report must still say it gave nothing."""
    _stub_sources(monkeypatch, html_names=[], web_names=["Casey Morgan"])
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert [s["status"] for s in scraper.last_report["sources"]] == ["empty"]


@pytest.mark.unit
def test_readable_directory_is_reported_ok_with_its_count(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Jordan Vega", "Riley Chen"], web_names=[])
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    source = scraper.last_report["sources"][0]
    assert (source["status"], source["found"]) == ("ok", 2)
