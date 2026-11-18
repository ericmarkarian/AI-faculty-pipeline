"""
Tests for how discovery problems reach the person reading the results.

A school whose directory page was blocked, or that came back empty, used to look
exactly like a school with no faculty. These tests cover the path from Agent 1 to
the run log, the alerts and the UI payload, plus two related guards: a shared roster
page is never read as one person's profile, and results are labelled with the
institution the user actually picked.
"""
import os

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test")  # module import needs a key present

import observe


class _Provider:
    """Scraper stand-in that returns a fixed roster and a fixed per-source report."""

    def __init__(self, records, sources):
        self._records, self._sources = records, sources

    def get_faculty(self, school, discipline):
        self.last_report = {"school": school, "sources": self._sources, "web_search_found": len(self._records)}
        return [{**r, "school": school} for r in self._records]


@pytest.fixture
def fs(monkeypatch, tmp_path):
    import faculty_search
    monkeypatch.chdir(tmp_path)  # logs/ and output files land in the temp dir
    monkeypatch.setattr(faculty_search.time, "sleep", lambda *a, **k: None)
    observe.start()  # the agents log and count metrics; run_pipeline normally starts this
    return faculty_search


# ── Agent 1 ───────────────────────────────────────────────────────────────────

def test_agent1_records_a_blocked_directory_for_the_school(fs):
    provider = _Provider(
        records=[{"full_name": "Casey Morgan", "title": None, "discovery_source": "web_search"}],
        sources=[{"type": "hospital", "url": "https://h.example/team", "status": "blocked",
                  "found": 0, "detail": "HTTP 403"}],
    )
    faculty = fs.agent1_scrape("School A", scraper=provider, discipline="Test")

    assert [f["full_name"] for f in faculty] == ["Casey Morgan"]
    summary = fs._discovery_reports["School A"]
    assert summary["found"] == 1
    assert summary["issues"] == ["Blocked: hospital page https://h.example/team (HTTP 403)"]


def test_agent1_ignores_a_stale_report_left_over_from_another_school(fs):
    provider = _Provider(records=[{"full_name": "Casey Morgan"}], sources=[])
    provider.get_faculty = lambda school, discipline: [{"full_name": "Casey Morgan", "school": school}]
    provider.last_report = {"school": "Some Other School",
                            "sources": [{"type": "hospital", "url": "u", "status": "blocked", "detail": "HTTP 403"}]}
    fs.agent1_scrape("School A", scraper=provider, discipline="Test")
    assert fs._discovery_reports["School A"]["issues"] == []


def test_agent1_flags_people_whose_profile_link_is_the_roster_itself(fs):
    provider = _Provider(
        records=[
            {"full_name": "Jordan Vega", "title": "Professor", "profile_url": "https://h.example/team#"},
            {"full_name": "Casey Morgan", "title": None, "profile_url": "https://h.example/team#"},
            {"full_name": "Riley Chen", "title": None, "profile_url": "https://h.example/doctors/riley-chen"},
        ],
        sources=[{"type": "hospital", "url": "https://h.example/team", "status": "ok", "found": 3, "detail": ""}],
    )
    faculty = fs.agent1_scrape("School A", scraper=provider, discipline="Test")
    assert {f["full_name"]: bool(f.get("profile_url_shared")) for f in faculty} == {
        "Jordan Vega": True, "Casey Morgan": True, "Riley Chen": False,
    }


# ── Agent 2 ───────────────────────────────────────────────────────────────────

class _Stub:
    def get_grants(self, name, school): return []
    def get_roles(self, name, school, profile_url=""): return []
    def get_hindex(self, name, school): return {"h_index": None}
    def get_email(self, name, school): return None


def _enrich(fs, faculty):
    stub = _Stub()
    return fs.agent2_enrich(faculty, nih_provider=stub, roles_provider=stub,
                            hindex_provider=stub, email_provider=stub, discipline="Test")


def test_enrichment_leaves_a_shared_roster_page_alone(fs, monkeypatch):
    """Reading the roster as a profile gave everyone on it the first person's title."""
    scraped: list[str] = []
    monkeypatch.setattr(fs, "_get_profile_scraper", lambda url: scraped.append(url))

    person = {"full_name": "Jordan Vega", "school": "School A", "title": "Associate Professor",
              "profile_url": "https://h.example/team#", "profile_url_shared": True}
    enriched = _enrich(fs, person)

    assert scraped == []
    assert enriched["title"] == "Associate Professor"


def test_enrichment_still_reads_a_persons_own_profile_page(fs, monkeypatch):
    scraped: list[str] = []
    monkeypatch.setattr(fs, "_get_profile_scraper", lambda url: scraped.append(url))

    person = {"full_name": "Jordan Vega", "school": "School A", "title": "Associate Professor",
              "profile_url": "https://h.example/doctors/jordan-vega"}
    _enrich(fs, person)
    assert scraped == ["https://h.example/doctors/jordan-vega"]


# ── run_pipeline ──────────────────────────────────────────────────────────────

def test_run_pipeline_hands_back_one_discovery_entry_per_school(fs, monkeypatch):
    def fake_scrape(school, scraper=None, discipline=""):
        count = int(school.split("#")[1])
        return [{"full_name": f"P {school} {i}", "school": school} for i in range(count)]

    def fake_enrich(faculty, roles_provider=None, discipline="", **kw):
        return {**faculty, "title": None, "email": None, "education": [], "h_index": None,
                "h_index_source": None, "nih_project_nums": "", "nih_grant_titles": "",
                "nih_grant_count": 0, "nih_institution_unconfirmed": False,
                "editorial_roles": [], "society_roles": [], "training_roles": [], "leadership_roles": []}

    monkeypatch.setattr(fs, "agent1_scrape", fake_scrape)
    monkeypatch.setattr(fs, "agent2_enrich", fake_enrich)
    monkeypatch.setattr(fs, "agent3_qc", lambda lst: [{**r, "qc": {}} for r in lst])
    monkeypatch.setattr(fs, "_release_memory", lambda: None)

    seen: list[list[dict]] = []
    fs.run_pipeline(schools=["Full#3", "Empty#0"], discipline="Test", on_discovery=seen.append)

    assert len(seen) == 1
    by_school = {d["school"]: d for d in seen[0]}
    assert by_school["Full#3"]["found"] == 3 and by_school["Full#3"]["issues"] == []
    assert by_school["Empty#0"]["found"] == 0 and by_school["Empty#0"]["issues"] == ["No faculty found"]


def test_run_pipeline_reports_discovery_even_when_nothing_was_found(fs, monkeypatch):
    monkeypatch.setattr(fs, "agent1_scrape", lambda school, scraper=None, discipline="": [])
    seen: list[list[dict]] = []
    fs.run_pipeline(schools=["Empty#0"], discipline="Test", on_discovery=seen.append)
    assert seen and seen[0][0]["issues"] == ["No faculty found"]


# ── alerts ────────────────────────────────────────────────────────────────────

def test_alerts_name_each_school_that_was_empty_or_unread(monkeypatch):
    observe._reset_metrics()
    monkeypatch.setattr(observe, "log", lambda *a, **k: None)
    discovery = [
        {"school": "School A", "found": 12, "issues": [],
         "sources": [{"type": "university", "url": "https://a.example/faculty", "status": "ok"}]},
        {"school": "School B", "found": 0, "issues": ["No faculty found"],
         "sources": [{"type": "hospital", "url": "https://b.example/team", "status": "blocked", "detail": "HTTP 403"}]},
    ]
    alerts = observe.run_alert_checks(12, "Test", discovery=discovery)

    assert "ZERO_FACULTY: no Test faculty found for School B" in alerts
    assert "DIRECTORY_UNREAD: School B — https://b.example/team (HTTP 403)" in alerts
    assert not any("School A" in a for a in alerts)


def test_alert_checks_work_without_a_discovery_report(monkeypatch):
    observe._reset_metrics()
    monkeypatch.setattr(observe, "log", lambda *a, **k: None)
    alerts = observe.run_alert_checks(50, "Test")
    assert not any(a.startswith(("ZERO_FACULTY", "DIRECTORY_UNREAD")) for a in alerts)


# ── server: labels shown to the user ──────────────────────────────────────────

def test_results_are_labelled_with_the_institution_the_user_picked():
    """'Boston Children's Hospital' runs as 'Harvard Medical School' — show what was picked."""
    import server
    labels = server._school_labels([
        "Boston Children's Hospital", "Harvard Medical School", "Yale School of Medicine",
    ])
    assert labels == {
        "Harvard Medical School": "Boston Children's Hospital",   # first pick wins
        "Yale School of Medicine": "Yale School of Medicine",
    }
