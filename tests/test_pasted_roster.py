"""
Tests for rosters a person pastes in when the pipeline cannot read a page itself.

Covers the three places a paste travels through: reading the pasted page into people
(providers.pasted_roster), using a saved roster during a run (GenericFacultyScraper),
and the server endpoint the app calls. All names are made up.
"""
import datetime
import os
import queue
import threading

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test")  # module import needs a key present

from providers import generic_faculty_scraper as gfs
from providers import pasted_roster
from providers.faculty_merge import SOURCE_PASTED, discovery_summary
from providers.pasted_roster import clean_pasted_rosters, is_stale, read_pasted_roster

_SCHOOL = "School A"
_DISCIPLINE = "Pediatric Endocrinology"
_URL = "https://h.example/endocrinology/our-team"

_PAGE_HTML = """
<h1>Meet the team</h1>
<h2>Physicians and Researchers</h2>
<h3><a href="/doctors/vega-jordan">Jordan Vega, MD</a></h3>
<h3><a href="/doctors/morgan-casey">Casey Morgan, MD</a></h3>
<h2>Fellows</h2>
<h3><a href="/doctors/okafor-sam">Sam Okafor, MD</a></h3>
<h2>Clinical and Professional Staff</h2>
<h3>Devon Hale</h3>
"""
_EVERYONE = [
    {"full_name": "Jordan Vega, MD", "title": None, "profile_url": "https://h.example/doctors/vega-jordan"},
    {"full_name": "Casey Morgan, MD", "title": None, "profile_url": "https://h.example/doctors/morgan-casey"},
    {"full_name": "Sam Okafor, MD", "title": None, "profile_url": "https://h.example/doctors/okafor-sam"},
    {"full_name": "Devon Hale", "title": None, "profile_url": None},
]


def _stub_model(monkeypatch, people=_EVERYONE, fail=None):
    """Replace the model call. Records the page text it was given."""
    seen: dict = {}

    def fake_extract(text, school, discipline, client, model="gpt-4o", errors=None):
        seen.update(text=text, school=school, discipline=discipline)
        if fail:
            errors.append(fail)
            return []
        return [dict(p) for p in people]

    monkeypatch.setattr(gfs, "_extract_via_gpt4o", fake_extract)
    return seen


# ── reading a pasted page ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_a_pasted_page_is_read_like_a_scraped_one(monkeypatch):
    """Formatted paste: headings reach the model, and fellows and staff are left out by section."""
    seen = _stub_model(monkeypatch)
    result = read_pasted_roster(_PAGE_HTML, "Meet the team ...", _URL, _SCHOOL, _DISCIPLINE, client=None)

    assert "\n## Fellows\n" in seen["text"]
    assert "[https://h.example/doctors/vega-jordan]" in seen["text"]      # relative links made absolute
    assert [p["full_name"] for p in result["people"]] == ["Jordan Vega, MD", "Casey Morgan, MD"]
    assert result["people"][0] == {"full_name": "Jordan Vega, MD", "title": None,
                                   "profile_url": "https://h.example/doctors/vega-jordan"}
    assert result["left_out"] == {"trainee": 1, "staff": 1}
    assert result["used_formatting"] is True


@pytest.mark.unit
def test_plain_text_is_used_when_there_is_no_formatted_copy(monkeypatch):
    seen = _stub_model(monkeypatch, people=[{"full_name": "Jordan Vega", "title": "Professor"},
                                           {"full_name": "Sam Okafor", "title": "Clinical Fellow"}])
    plain = "Our team\nJordan Vega, Professor\nSam Okafor, Clinical Fellow\n"
    result = read_pasted_roster("", plain, _URL, _SCHOOL, _DISCIPLINE, client=None)

    assert seen["text"] == plain.strip()
    assert [p["full_name"] for p in result["people"]] == ["Jordan Vega"]   # titles still screen
    assert result["used_formatting"] is False


@pytest.mark.unit
def test_a_formatted_copy_that_lost_the_page_falls_back_to_plain_text(monkeypatch):
    seen = _stub_model(monkeypatch, people=[{"full_name": "Jordan Vega", "title": "Professor"}])
    plain = "Jordan Vega, Professor of Pediatrics. " * 20
    result = read_pasted_roster("<div><img src='team.png'></div>", plain, _URL, _SCHOOL, _DISCIPLINE, client=None)
    assert seen["text"] == plain.strip() and result["used_formatting"] is False


@pytest.mark.unit
def test_nothing_pasted_and_an_unreachable_model_are_errors_not_empty_rosters(monkeypatch):
    _stub_model(monkeypatch, fail="Connection error.")
    with pytest.raises(ValueError, match="Nothing readable"):
        read_pasted_roster("  ", "\n", _URL, _SCHOOL, _DISCIPLINE, client=None)
    with pytest.raises(RuntimeError, match="Connection error"):
        read_pasted_roster("", "Jordan Vega, Professor", _URL, _SCHOOL, _DISCIPLINE, client=None)


@pytest.mark.unit
def test_a_page_that_really_lists_nobody_is_an_empty_roster(monkeypatch):
    _stub_model(monkeypatch, people=[])
    result = read_pasted_roster("", "Directions and parking", _URL, _SCHOOL, _DISCIPLINE, client=None)
    assert result["people"] == [] and result["left_out"] == {}


@pytest.mark.unit
def test_extraction_reports_why_a_chunk_failed_when_asked():
    class _Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("Incorrect API key provided")

    errors: list = []
    assert gfs._extract_via_gpt4o("Jordan Vega, Professor", _SCHOOL, _DISCIPLINE, _Client, errors=errors) == []
    assert errors == ["Incorrect API key provided"]
    assert gfs._extract_via_gpt4o("Jordan Vega, Professor", _SCHOOL, _DISCIPLINE, _Client) == []   # old callers unchanged


# ── rosters coming back from the browser ──────────────────────────────────────

def _roster(**overrides):
    base = {"school": _SCHOOL, "discipline": _DISCIPLINE, "url": _URL, "type": "hospital",
            "captured": "2026-11-20",
            "people": [{"full_name": "Jordan Vega, MD", "title": None, "profile_url": "https://h.example/doctors/vega"},
                       {"full_name": "Casey Morgan, MD", "title": "Professor"}]}
    return {**base, **overrides}


@pytest.mark.unit
def test_a_well_formed_roster_passes_through():
    assert clean_pasted_rosters([_roster()]) == [{
        "school": _SCHOOL, "discipline": _DISCIPLINE, "url": _URL, "type": "hospital", "captured": "2026-11-20",
        "people": [{"full_name": "Jordan Vega, MD", "title": None, "profile_url": "https://h.example/doctors/vega"},
                   {"full_name": "Casey Morgan, MD", "title": "Professor", "profile_url": None}],
    }]


@pytest.mark.unit
@pytest.mark.parametrize("raw", [
    None, "rosters", {"school": _SCHOOL}, [None, 3, "x"],
    [_roster(school="")], [_roster(url="javascript:alert(1)")], [_roster(url="ftp://h.example/x")],
    [_roster(people=[])], [_roster(people="Jordan Vega")], [_roster(people=[{"title": "Professor"}])],
])
def test_malformed_rosters_are_dropped_without_raising(raw):
    assert clean_pasted_rosters(raw) == []


@pytest.mark.unit
def test_unsafe_or_oversized_fields_are_cleaned():
    cleaned = clean_pasted_rosters([_roster(
        type="made-up", captured="yesterday",
        people=[{"full_name": "Jordan Vega", "title": "T" * 900, "profile_url": "javascript:alert(1)"},
                {"full_name": "Jordan Vega"},                       # the same person twice
                {"full_name": "N" * 500},                           # not a name
                {"full_name": "Casey Morgan", "extra": {"nested": True}}],
    )])[0]
    assert cleaned["type"] == "unknown" and cleaned["captured"] is None
    assert [p["full_name"] for p in cleaned["people"]] == ["Jordan Vega", "Casey Morgan"]
    assert len(cleaned["people"][0]["title"]) == 300 and cleaned["people"][0]["profile_url"] is None
    assert set(cleaned["people"][1]) == {"full_name", "title", "profile_url"}


@pytest.mark.unit
@pytest.mark.parametrize("captured,stale", [
    ("2026-11-20", False), ("2026-06-01", False), ("2026-05-01", True), (None, False), ("not a date", False),
])
def test_a_roster_goes_stale_after_about_six_months(captured, stale):
    assert is_stale(captured, today=datetime.date(2026, 11, 21)) is stale


# ── using a pasted roster during a run ────────────────────────────────────────

@pytest.fixture
def scraper(monkeypatch, tmp_path):
    monkeypatch.setattr(gfs.OpenAI, "__init__", lambda self, **kw: None)
    instance = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    instance._urls = {_DISCIPLINE: {_SCHOOL: {"hospital_url": _URL}}}
    return instance


def _stub_page(monkeypatch, *, people=(), error=None, web=()):
    def fake_fetch(url):
        if error:
            raise error
        return "<html><body>team</body></html>"

    monkeypatch.setattr(gfs, "_fetch_html_playwright", fake_fetch)
    monkeypatch.setattr(gfs, "_extract_via_gpt4o",
                        lambda text, school, discipline, client, model="gpt-4o", errors=None:
                        [{"full_name": n, "title": "Professor"} for n in people])
    monkeypatch.setattr(gfs, "_extract_via_web_search",
                        lambda school, discipline, client: [{"full_name": n, "title": "Professor"} for n in web])
    monkeypatch.setattr(gfs, "_pagination_urls", lambda html, url: [])


@pytest.mark.unit
def test_a_pasted_roster_stands_in_for_a_blocked_page(scraper, monkeypatch):
    _stub_page(monkeypatch, error=gfs.DirectoryBlocked("HTTP 403"), web=["Jordan Vega"])
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster()]))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    by_name = {r["full_name"]: r for r in records}
    assert set(by_name) == {"Jordan Vega, MD", "Casey Morgan, MD"}
    assert by_name["Jordan Vega, MD"]["discovery_source"] == "pasted+web_search"    # one person, both sources
    assert by_name["Jordan Vega, MD"]["profile_url"] == "https://h.example/doctors/vega"
    assert by_name["Casey Morgan, MD"]["discovery_source"] == SOURCE_PASTED

    source = scraper.last_report["sources"][0]
    assert source["status"] == "pasted" and source["found"] == 2 and source["detail"] == "HTTP 403"
    summary = discovery_summary(_SCHOOL, records, scraper.last_report)
    assert summary["issues"] == []                                                  # the gap is covered
    assert summary["notes"] == [
        "Could not read the hospital page (HTTP 403); used the roster pasted on 2026-11-20 (2 people)"]


@pytest.mark.unit
def test_a_page_that_reads_well_is_preferred_to_the_paste(scraper, monkeypatch):
    live = ["Riley Chen", "Sam Okafor", "Avery Quinn", "Devon Hale", "Morgan Ellis"]
    _stub_page(monkeypatch, people=live)
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster()]))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert sorted(r["full_name"] for r in records) == sorted(live)
    assert "pasted" not in scraper.last_report["sources"][0]


@pytest.mark.unit
def test_a_page_that_reads_short_is_topped_up_from_the_paste(scraper, monkeypatch):
    _stub_page(monkeypatch, people=["Riley Chen"])
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster()]))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert {r["full_name"] for r in records} == {"Riley Chen", "Jordan Vega, MD", "Casey Morgan, MD"}
    source = scraper.last_report["sources"][0]
    assert source["status"] == "ok" and source["found"] == 3
    assert discovery_summary(_SCHOOL, records, scraper.last_report)["notes"] == [
        "Added the roster pasted on 2026-11-20 for the hospital page (2 people)"]


@pytest.mark.unit
def test_a_roster_pasted_from_another_page_of_the_school_is_still_used(scraper, monkeypatch):
    _stub_page(monkeypatch, people=["Riley Chen", "Sam Okafor", "Avery Quinn", "Devon Hale", "Morgan Ellis"])
    other = "https://research.h.example/people"
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster(url=other, type="university")]))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert len(records) == 7
    assert scraper.last_report["sources"][1] == {
        "type": "university", "url": other, "status": "pasted", "found": 2, "detail": "",
        "pasted": {"on": "2026-11-20", "found": 2, "stale": False},
    }


@pytest.mark.unit
def test_rosters_for_another_school_or_specialty_are_ignored(scraper, monkeypatch):
    _stub_page(monkeypatch, error=gfs.DirectoryBlocked("HTTP 403"))
    scraper.use_pasted_rosters(clean_pasted_rosters([
        _roster(school="School B"), _roster(discipline="Pediatric Cardiology")]))
    assert scraper.get_faculty(_SCHOOL, _DISCIPLINE) == []
    assert scraper.last_report["sources"][0]["status"] == "blocked"


@pytest.mark.unit
def test_an_old_pasted_roster_is_used_but_flagged(scraper, monkeypatch):
    _stub_page(monkeypatch, error=gfs.DirectoryBlocked("HTTP 403"))
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster(captured="2020-01-15")]))
    records = scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    assert len(records) == 2
    assert discovery_summary(_SCHOOL, records, scraper.last_report)["issues"] == [
        f"The roster pasted for the hospital page is old (2020-01-15) — open {_URL} and paste it again"]


@pytest.mark.unit
def test_a_paste_sets_the_number_of_people_expected_on_that_page(scraper, monkeypatch, tmp_path):
    _stub_page(monkeypatch, error=gfs.DirectoryBlocked("HTTP 403"))
    people = [{"full_name": n} for n in ("Riley Chen", "Sam Okafor", "Avery Quinn", "Devon Hale", "Morgan Ellis", "Dana Brooks")]
    scraper.use_pasted_rosters(clean_pasted_rosters([_roster(people=people)]))
    scraper.get_faculty(_SCHOOL, _DISCIPLINE)

    later = gfs.GenericFacultyScraper(urls_path=str(tmp_path / "urls.json"), request_delay=0.0)
    assert later._counts.get(_DISCIPLINE, _SCHOOL, _URL)["count"] == 6


# ── the pipeline hands pasted rosters to the scraper ──────────────────────────

def test_run_pipeline_validates_pasted_rosters_before_the_scraper_sees_them(monkeypatch, tmp_path):
    import faculty_search as fs
    import observe

    class _Provider:
        received = "never called"
        def use_pasted_rosters(self, rosters):
            _Provider.received = rosters

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fs.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_scraper_provider", _Provider())
    monkeypatch.setattr(fs, "agent1_scrape", lambda school, scraper=None, discipline="": [])
    observe.start()

    fs.run_pipeline(schools=[_SCHOOL], discipline=_DISCIPLINE,
                    pasted_rosters=[_roster(), {"school": 7}, "junk"])
    assert [r["url"] for r in _Provider.received] == [_URL]

    fs.run_pipeline(schools=[_SCHOOL], discipline=_DISCIPLINE)      # none sent: any earlier ones are cleared
    assert _Provider.received == []


# ── the endpoint the app calls ────────────────────────────────────────────────

class _ThreadProcess:
    """Stand-in for a spawned process: runs the child entry point in a thread."""

    def __init__(self, target, args, daemon=None):
        self._thread = threading.Thread(target=target, args=args, daemon=True)

    def start(self): self._thread.start()
    def is_alive(self): return self._thread.is_alive()
    def join(self, timeout=None): self._thread.join(timeout)
    def terminate(self): pass


class _ThreadContext:
    Queue = staticmethod(queue.Queue)
    Process = _ThreadProcess


@pytest.fixture
def client(monkeypatch):
    import server
    monkeypatch.setattr(server, "_MP_CTX", _ThreadContext)
    return server.app.test_client()


def _post(client, **overrides):
    body = {"html": _PAGE_HTML, "text": "Meet the team", "url": _URL, "school": _SCHOOL, "discipline": _DISCIPLINE}
    return client.post("/api/roster-paste", json={**body, **overrides})


def test_the_endpoint_returns_the_people_found_on_a_pasted_page(client, monkeypatch):
    _stub_model(monkeypatch)
    resp = _post(client)

    assert resp.status_code == 200
    data = resp.get_json()
    assert [p["full_name"] for p in data["people"]] == ["Jordan Vega, MD", "Casey Morgan, MD"]
    assert data["left_out"] == {"trainee": 1, "staff": 1} and data["used_formatting"] is True
    assert datetime.date.fromisoformat(data["captured"])                    # today's date, for the browser to keep


@pytest.mark.parametrize("overrides,status,message", [
    ({"html": "", "text": "  "}, 400, "Nothing was pasted"),
    ({"school": ""}, 400, "Missing the institution"),
    ({"html": None, "text": ["not", "text"]}, 400, "Nothing was pasted"),
    ({"text": "x" * 3_000_001, "html": ""}, 413, "too much text"),
])
def test_the_endpoint_rejects_requests_it_cannot_read(client, monkeypatch, overrides, status, message):
    _stub_model(monkeypatch)
    resp = _post(client, **overrides)
    assert resp.status_code == status and message in resp.get_json()["error"]


def test_the_endpoint_says_so_when_the_model_cannot_be_reached(client, monkeypatch):
    _stub_model(monkeypatch, fail="Incorrect API key provided")
    resp = _post(client)
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "Could not read the pasted page: Incorrect API key provided"


def test_the_endpoint_needs_a_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    resp = _post(client)
    assert resp.status_code == 400 and resp.get_json()["code"] == "no_api_key"
    # A key sent with the request is enough — shared deployments never hold one themselves.
    _stub_model(monkeypatch)
    assert _post(client, api_key="sk-test-from-the-browser").status_code == 200
