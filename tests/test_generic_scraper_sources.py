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



# ── reading a page by its sections ────────────────────────────────────────────

@pytest.mark.unit
def test_headings_survive_as_their_own_lines():
    html = """<html><body><nav><h2>Menu</h2></nav>
      <h1>Meet the team</h1>
      <h2>Fellows</h2><ul><li>Sam Okafor</li></ul>
      <h3><a href="/doctors/vega">Jordan  Vega, MD</a></h3>
    </body></html>"""
    text = gfs._html_to_text(html, base_url="https://h.example/team")
    assert text.splitlines()[0] == "# Meet the team"
    assert "\n## Fellows\n" in text and "Sam Okafor" in text
    assert "\n### Jordan Vega, MD [https://h.example/doctors/vega]" in text    # link kept, spacing tidied
    assert "Menu" not in text                                                  # navigation is still dropped


def _stub_pages(monkeypatch, pages, web=()):
    """Stub fetch + extraction per URL. pages: url -> (html, [people]); a missing URL fails to load.

    Pages are built with _page(), which stamps each one with its URL so the extractor
    stub can tell from the text alone which page it was handed.
    """
    fetched: list[str] = []

    def fake_fetch(url):
        fetched.append(url)
        if url not in pages:
            raise RuntimeError("HTTP 404")
        return pages[url][0]

    def fake_extract(text, school, discipline, client, model="gpt-4o"):
        for url, (_html, people) in pages.items():
            if f"page-id:{url}|" in text:
                return [dict(p) for p in people]
        return []

    monkeypatch.setattr(gfs, "_fetch_html_playwright", fake_fetch)
    monkeypatch.setattr(gfs, "_extract_via_gpt4o", fake_extract)
    monkeypatch.setattr(gfs, "_extract_via_web_search", lambda school, discipline, client: [dict(p) for p in web])
    monkeypatch.setattr(gfs, "_pagination_urls", lambda html, url: [])
    return fetched


def _page(url, links=""):
    return f"<html><body><p>page-id:{url}|</p>{links}</body></html>"


def _faculty(*names):
    return [{"full_name": n, "title": "Assistant Professor"} for n in names]


_SAVED = "https://med.stanford.edu/peds"
_ROSTER = "https://med.stanford.edu/peds/endocrinology/faculty"
_ROSTER_LINK = '<a href="/peds/endocrinology/faculty">Faculty</a>'
_SIX = ("Casey Morgan", "Riley Chen", "Sam Okafor", "Avery Quinn", "Devon Hale", "Morgan Ellis")


@pytest.mark.unit
def test_a_thin_page_is_followed_to_the_roster_it_links_to(scraper, monkeypatch):
    """The saved URL is a division overview naming only the chief; the roster is one click away."""
    fetched = _stub_pages(monkeypatch, {
        _SAVED: (_page(_SAVED, _ROSTER_LINK), _faculty("Jordan Vega")),
        _ROSTER: (_page(_ROSTER), _faculty("Jordan Vega", *_SIX)),
    })
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert fetched == [_SAVED, _ROSTER]
    assert sorted(r["full_name"] for r in records) == sorted(("Jordan Vega", *_SIX))
    source = scraper.last_report["sources"][0]
    assert source["status"] == "ok" and source["found"] == 7
    assert source["followed"] == {"url": _ROSTER, "found": 7, "own_found": 1}


@pytest.mark.unit
def test_a_page_that_already_lists_the_faculty_is_not_followed_further(scraper, monkeypatch):
    fetched = _stub_pages(monkeypatch, {
        _SAVED: (_page(_SAVED, _ROSTER_LINK), _faculty(*_SIX)),
        _ROSTER: (_page(_ROSTER), _faculty(*_SIX, "Jordan Vega")),
    })
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert fetched == [_SAVED]
    assert "followed" not in scraper.last_report["sources"][0]


@pytest.mark.unit
def test_a_linked_page_that_is_no_better_or_will_not_load_changes_nothing(scraper, monkeypatch):
    other = "https://med.stanford.edu/peds/endocrinology/team"
    links = _ROSTER_LINK + '<a href="/peds/endocrinology/team">Our team</a>'
    fetched = _stub_pages(monkeypatch, {
        _SAVED: (_page(_SAVED, links), _faculty("Jordan Vega", "Casey Morgan")),
        other: (_page(other), _faculty("Jordan Vega")),          # _ROSTER is absent: it fails to load
    })
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert set(fetched) == {_SAVED, _ROSTER, other}
    assert sorted(r["full_name"] for r in records) == ["Casey Morgan", "Jordan Vega"]
    source = scraper.last_report["sources"][0]
    assert source["found"] == 2 and "followed" not in source


@pytest.mark.unit
def test_following_links_can_be_turned_off(monkeypatch, tmp_path):
    monkeypatch.setattr(gfs.OpenAI, "__init__", lambda self, **kw: None)
    instance = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0,
                                         use_web_search=False, follow_links=False)
    instance._urls = {_DISCIPLINE: {_SCHOOL: {"university_url": _SAVED}}}
    fetched = _stub_pages(monkeypatch, {
        _SAVED: (_page(_SAVED, _ROSTER_LINK), _faculty("Jordan Vega")),
        _ROSTER: (_page(_ROSTER), _faculty(*_SIX)),
    })
    assert [r["full_name"] for r in instance.get_faculty(_SCHOOL, _DISCIPLINE)] == ["Jordan Vega"]
    assert fetched == [_SAVED]


@pytest.mark.unit
def test_trainees_and_staff_are_left_out_and_counted(scraper, monkeypatch):
    people = _faculty(*_SIX) + [
        {"full_name": "Dana Brooks, MD", "title": "Clinical Fellow"},
        {"full_name": "Robin Castell, MD", "title": "Clinical Fellow"},
        {"full_name": "Jamie Park, CPNP", "title": None},
    ]
    _stub_pages(monkeypatch, {_SAVED: (_page(_SAVED), people)},
                web=[{"full_name": "Dana Brooks", "title": "Pediatric Endocrinology Fellow"},
                     {"full_name": "Jordan Vega", "title": "Professor"}])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert sorted(r["full_name"] for r in records) == sorted((*_SIX, "Jordan Vega"))
    report = scraper.last_report
    assert report["sources"][0]["found"] == 6
    assert report["sources"][0]["left_out"] == {"trainee": 2, "staff": 1}
    assert report["web_search_found"] == 1 and report["web_search_left_out"] == {"trainee": 1}


# ── remembering how many people a page lists ──────────────────────────────────

@pytest.mark.unit
def test_a_page_that_shrinks_is_reported_with_the_count_it_used_to_have(scraper, monkeypatch, tmp_path):
    from providers.faculty_merge import discovery_summary

    eight = (*_SIX, "Jordan Vega", "Dana Brooks")
    _stub_pages(monkeypatch, {_SAVED: (_page(_SAVED), _faculty(*eight))})
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert "expected" not in scraper.last_report["sources"][0]        # first read: nothing to compare with
    assert (tmp_path / "roster_counts.json").exists()                 # history sits beside the URL file

    # A later run — a new scraper, as each run is — finds only three people on the same page.
    later = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    later._urls = scraper._urls
    _stub_pages(monkeypatch, {_SAVED: (_page(_SAVED), _faculty(*eight[:3]))})
    records = later.get_faculty(_SCHOOL, _DISCIPLINE)

    source = later.last_report["sources"][0]
    assert source["found"] == 3 and source["expected"] == 8
    summary = discovery_summary(_SCHOOL, records, later.last_report)
    assert len(summary["issues"]) == 1
    assert summary["issues"][0].startswith(f"Fewer people than expected on: university page {_SAVED} (3 found; 8 when checked ")
    assert summary["sources"][0]["short"] is True

    # The short read must not overwrite the count it was measured against.
    again = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    assert again._counts.get(_DISCIPLINE, _SCHOOL, _SAVED)["count"] == 8


@pytest.mark.unit
def test_a_page_known_to_be_full_is_followed_when_it_comes_back_short(scraper, monkeypatch):
    """Five people passes for a roster on its own — not when the page used to list twenty."""
    scraper._counts.note(_DISCIPLINE, _SCHOOL, _SAVED, 20, today="2026-11-19")
    fetched = _stub_pages(monkeypatch, {
        _SAVED: (_page(_SAVED, _ROSTER_LINK), _faculty(*_SIX[:5])),
        _ROSTER: (_page(_ROSTER), _faculty(*_SIX, "Jordan Vega")),
    })
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert fetched == [_SAVED, _ROSTER]


@pytest.mark.unit
def test_a_small_division_is_looked_into_once_then_left_alone(scraper, monkeypatch, tmp_path):
    """Three faculty and a team link that shows the same three: remember it, stop re-checking."""
    pages = {
        _SAVED: (_page(_SAVED, _ROSTER_LINK), _faculty(*_SIX[:3])),
        _ROSTER: (_page(_ROSTER), _faculty(*_SIX[:3])),
    }
    first = _stub_pages(monkeypatch, pages)
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)
    assert first == [_SAVED, _ROSTER]

    later = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    later._urls = scraper._urls
    second = _stub_pages(monkeypatch, pages)
    later.get_faculty(_SCHOOL, _DISCIPLINE)
    assert second == [_SAVED]
    assert later.last_report["sources"][0]["expected"] == 3


@pytest.mark.unit
def test_a_blocked_page_reports_how_many_people_it_should_have(scraper, monkeypatch):
    from providers.faculty_merge import discovery_summary

    scraper._counts.note(_DISCIPLINE, _SCHOOL, _SAVED, 41, today="2026-11-20")
    _stub_sources(monkeypatch, html_error=gfs.DirectoryBlocked("HTTP 403"), web_names=["Jordan Vega"])
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert scraper.last_report["sources"][0]["expected"] == 41
    assert discovery_summary(_SCHOOL, records, scraper.last_report)["issues"] == [
        f"Blocked: university page {_SAVED} (HTTP 403); about 41 people expected there",
    ]
