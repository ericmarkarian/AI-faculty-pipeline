"""
Tests for per-school enrichment.

1. test_enriches_one_school_at_a_time — structural: faculty are enriched grouped by
   school (no interleaving) and memory is released between schools. This is the
   property that bounds peak memory to a single school regardless of school count.

2. test_per_school_peak_memory — experiment: runs the REAL run_pipeline twice in
   fresh subprocesses with network mocked but realistic per-faculty work (parse a
   large HTML blob with BeautifulSoup, which is what actually grows RSS). One run
   splits the same faculty across many schools (per-school batching), the other puts
   them all in one school (all-at-once). It compares peak RSS (ru_maxrss). This
   measures whether per-school + gc/malloc_trim actually keeps the peak down.
"""
import os
import platform
import subprocess
import sys
import textwrap

os.environ.setdefault("OPENAI_API_KEY", "test")  # module import needs a key present

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_enriches_one_school_at_a_time(monkeypatch, tmp_path):
    import faculty_search as fs

    def fake_scrape(school, scraper=None, discipline=""):
        count = int(school.split("#")[1])
        return [{"full_name": f"P {school} {i}", "school": school} for i in range(count)]

    enrich_order: list[str] = []

    def fake_enrich(faculty, roles_provider=None, discipline="", **kw):
        enrich_order.append(faculty["school"])
        return {
            **faculty, "title": None, "email": None, "education": [], "h_index": None,
            "h_index_source": None, "nih_project_nums": "", "nih_grant_titles": "",
            "nih_grant_count": 0, "nih_institution_unconfirmed": False,
            "editorial_roles": [], "society_roles": [], "training_roles": [], "leadership_roles": [],
        }

    releases: list[int] = []
    monkeypatch.setattr(fs, "agent1_scrape", fake_scrape)
    monkeypatch.setattr(fs, "agent2_enrich", fake_enrich)
    monkeypatch.setattr(fs, "agent3_qc", lambda lst: [{**r, "qc": {}} for r in lst])
    monkeypatch.setattr(fs, "_release_memory", lambda: releases.append(1))
    monkeypatch.setattr(fs.time, "sleep", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)

    schools = ["A#10", "B#10", "C#10"]
    fs.run_pipeline(schools=schools, discipline="Test")

    # memory released exactly once per school
    assert len(releases) == len(schools)

    # schools are processed contiguously — school B never starts before A finishes
    contiguous: list[str] = []
    for s in enrich_order:
        if not contiguous or contiguous[-1] != s:
            contiguous.append(s)
    assert contiguous == schools, f"schools interleaved: {contiguous}"
    assert enrich_order.count("A#10") == 10

    # output written
    assert (tmp_path / "faculty_output.csv").exists()


# ── Peak-RSS experiment ───────────────────────────────────────────────────────

_WORKER = textwrap.dedent(
    """
    import os, sys, resource, platform
    os.environ.setdefault("OPENAI_API_KEY", "test")
    sys.path.insert(0, {repo!r})
    import faculty_search as fs

    # A chunky HTML blob; parsing it with BeautifulSoup per faculty is what grows RSS.
    _HTML = ("<html><body>" + "".join(
        f"<div class='c{{i}}'><p>Name {{i}}</p><a href='mailto:x{{i}}@y.edu'>e</a></div>"
        for i in range(8000)
    ) + "</body></html>")

    from bs4 import BeautifulSoup

    def fake_scrape(school, scraper=None, discipline=""):
        count = int(school.split("#")[1])
        return [{{"full_name": f"P {{school}} {{i}}", "school": school}} for i in range(count)]

    def fake_enrich(faculty, roles_provider=None, discipline="", **kw):
        # realistic transient: parse + extract text, then drop it
        soup = BeautifulSoup(_HTML, "html.parser")
        _ = soup.get_text(" ", strip=True)[:50]
        del soup
        return {{
            **faculty, "title": None, "email": None, "education": [], "h_index": None,
            "h_index_source": None, "nih_project_nums": "", "nih_grant_titles": "",
            "nih_grant_count": 0, "nih_institution_unconfirmed": False,
            "editorial_roles": [], "society_roles": [], "training_roles": [], "leadership_roles": [],
        }}

    fs.agent1_scrape = fake_scrape
    fs.agent2_enrich = fake_enrich
    fs.agent3_qc = lambda lst: [{{**r, "qc": {{}}}} for r in lst]
    fs.time.sleep = lambda *a, **k: None

    import tempfile
    os.chdir(tempfile.mkdtemp())

    schools = sys.argv[1].split(",")
    fs.run_pipeline(schools=schools, discipline="Test")

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if platform.system() == "Darwin" else peak / 1024
    print(f"PEAK_MB={{peak_mb:.1f}}")
    """
)


def _peak_mb(schools: str) -> float:
    out = subprocess.check_output(
        [sys.executable, "-c", _WORKER.format(repo=REPO), schools],
        env={**os.environ, "MAX_WORKERS": "4", "OPENAI_API_KEY": "test"},
        text=True,
    )
    line = [l for l in out.splitlines() if l.startswith("PEAK_MB=")][-1]
    return float(line.split("=", 1)[1])


def test_per_school_peak_memory():
    # Same 90 faculty either way: split across 6 schools vs all in one school.
    per_school = _peak_mb("S1#15,S2#15,S3#15,S4#15,S5#15,S6#15")
    all_at_once = _peak_mb("BIG#90")
    print(f"\n  per-school peak  = {per_school:.0f} MB")
    print(f"  all-at-once peak = {all_at_once:.0f} MB")
    # Per-school must not be worse; in practice it should be lower because memory is
    # released between schools. Lenient bound so the test documents rather than flakes.
    assert per_school <= all_at_once * 1.15, (
        f"per-school ({per_school:.0f}MB) worse than all-at-once ({all_at_once:.0f}MB)"
    )
