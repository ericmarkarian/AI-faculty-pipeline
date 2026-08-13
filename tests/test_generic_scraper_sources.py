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
    calls = _stub_sources(monkeypatch, html_names=["Tandy Aye"], web_names=["David Maahs"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert calls["html"] == 1 and calls["web"] == 1
    assert {r["full_name"] for r in records} == {"Tandy Aye", "David Maahs"}


@pytest.mark.unit
def test_web_search_runs_even_when_directory_scrape_succeeds(scraper, monkeypatch):
    """The old behavior — web search only on failure — is what caused batch/manual drift."""
    calls = _stub_sources(monkeypatch, html_names=["Tandy Aye"], web_names=[])
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["web"] == 1


@pytest.mark.unit
def test_overlapping_people_are_deduplicated(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Anna L. Gloyn"], web_names=["Anna Gloyn, PhD"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert len(records) == 1
    assert records[0]["full_name"] == "Anna L. Gloyn"
    assert records[0]["discovery_source"] == f"{SOURCE_UNIVERSITY}+{SOURCE_WEB_SEARCH}"


@pytest.mark.unit
def test_web_search_results_survive_a_failed_directory_fetch(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_error=RuntimeError("browser crashed"),
                  web_names=["David Maahs"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert [r["full_name"] for r in records] == ["David Maahs"]


@pytest.mark.unit
def test_directory_results_survive_a_failed_web_search(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Tandy Aye"], web_error=RuntimeError("rate limited"))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert [r["full_name"] for r in records] == ["Tandy Aye"]


@pytest.mark.unit
def test_web_search_runs_when_no_directory_url_is_known(scraper, monkeypatch):
    """No configured or discoverable URL used to mean zero faculty; now web search still runs."""
    calls = _stub_sources(monkeypatch, web_names=["David Maahs"])
    monkeypatch.setattr(scraper, "_get_url_sources", lambda school, discipline: [])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["html"] == 0 and calls["web"] == 1
    assert [r["full_name"] for r in records] == ["David Maahs"]


@pytest.mark.unit
def test_web_search_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(gfs.OpenAI, "__init__", lambda self, **kw: None)
    instance = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"),
                                         request_delay=0.0, use_web_search=False)
    instance._urls = {_DISCIPLINE: {_SCHOOL: {"university_url": "https://med.stanford.edu/peds"}}}
    calls = _stub_sources(monkeypatch, html_names=["Tandy Aye"], web_names=["David Maahs"])

    records = instance.get_faculty(_SCHOOL, _DISCIPLINE)
    assert calls["web"] == 0
    assert [r["full_name"] for r in records] == ["Tandy Aye"]


@pytest.mark.unit
def test_records_carry_school_and_discovery_source(scraper, monkeypatch):
    _stub_sources(monkeypatch, html_names=["Tandy Aye"], web_names=["David Maahs"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert all(r["school"] == _SCHOOL for r in records)
    assert {r["discovery_source"] for r in records} == {SOURCE_UNIVERSITY, SOURCE_WEB_SEARCH}


# ── web-search response parsing ───────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected_names", [
    ('[{"full_name": "Tandy Aye"}]', ["Tandy Aye"]),
    ('```json\n[{"full_name": "Tandy Aye"}]\n```', ["Tandy Aye"]),
    ('Here you go:\n[{"full_name": "Tandy Aye"}]', ["Tandy Aye"]),
    # truncated output — keep the complete prefix instead of losing the whole roster
    ('[{"full_name": "Tandy Aye"}, {"full_name": "David Ma', ["Tandy Aye"]),
    ("no json at all", []),
    ("", []),
])
def test_parse_faculty_array_is_tolerant(raw, expected_names):
    items = gfs._parse_faculty_array(raw)
    assert [i["full_name"] for i in items] == expected_names
