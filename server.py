"""
Flask SSE server for ATLAS — Academic Talent Location And Sourcing.

Usage:
    python3 server.py
    # then open http://localhost:5050
"""
import json
import multiprocessing as mp
import os
import queue
import random
import threading
import time
from collections import OrderedDict

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

load_dotenv()

# NOTE: faculty_search is intentionally NOT imported here. It pulls in pandas,
# playwright, openai and the providers (~180MB) which the parent never uses — all
# pipeline work happens in the child (_pipeline_child imports it there). Importing it
# in the long-lived gunicorn worker would double the memory baseline (parent + child).


# ─────────────────────────────────────────────
# Subprocess-per-run
# Each pipeline run executes in a FRESH process so all of its memory (Chromium,
# parsed HTML, glibc fragmentation) is returned to the OS when it exits. The
# long-lived gunicorn worker stays lean and never accumulates across runs, and an
# out-of-memory run kills only the child — the server survives and reports it.
# ─────────────────────────────────────────────

# "spawn" (not fork): forking a threaded gunicorn worker can deadlock on inherited
# locks. spawn starts a clean interpreter that re-imports the app.
_MP_CTX = mp.get_context("spawn")


def _pipeline_child(kind: str, payload: dict, mpq) -> None:
    """Entry point that runs in the spawned process; streams results back via mpq."""
    def on_enriched(rec, completed, total):
        mpq.put({"type": "faculty", "data": rec, "completed": completed, "total": total})

    def on_qc_complete(rec):
        mpq.put({"type": "qc_update", "data": rec})

    try:
        # A per-request key (multi-user deployments) must land in the environment
        # BEFORE faculty_search builds its OpenAI client at import time. The child
        # is a fresh spawned process, so this never leaks across users or runs.
        if payload.get("api_key"):
            os.environ["OPENAI_API_KEY"] = payload["api_key"]
        # Inside try so an import-time failure (e.g. missing API key) reaches the
        # UI as its real error instead of "exited unexpectedly (likely out of memory)".
        import faculty_search

        if kind == "names":
            faculty_search.run_pipeline_from_records(
                payload["records"], discipline=payload["discipline"],
                on_enriched=on_enriched, on_qc_complete=on_qc_complete,
                considerations=payload["considerations"],
            )
        else:
            faculty_search.run_pipeline(
                schools=payload["schools"], discipline=payload["discipline"],
                on_enriched=on_enriched, on_qc_complete=on_qc_complete,
                considerations=payload["considerations"],
            )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        mpq.put({"type": "error", "message": str(exc)})
    finally:
        mpq.put({"type": "__child_done__"})


def _spawn_and_forward(kind: str, payload: dict, sse_queue: "queue.Queue") -> None:
    """Run the pipeline in a subprocess; forward its messages to the SSE queue.
    Always emits exactly one terminal {"type": "done"}; survives child OOM."""
    proc = None
    try:
        mpq = _MP_CTX.Queue()
        proc = _MP_CTX.Process(target=_pipeline_child, args=(kind, payload, mpq), daemon=True)
        proc.start()
        while True:
            try:
                msg = mpq.get(timeout=5)
            except queue.Empty:
                if not proc.is_alive():
                    # child died without signaling — almost always an OOM kill
                    sse_queue.put({"type": "error",
                                   "message": "pipeline process exited unexpectedly (likely out of memory)"})
                    break
                continue
            if msg.get("type") == "__child_done__":
                break
            sse_queue.put(msg)
    except Exception as exc:  # noqa: BLE001
        sse_queue.put({"type": "error", "message": f"failed to start pipeline: {exc}"})
    finally:
        if proc is not None:
            proc.join(timeout=20)
            if proc.is_alive():
                proc.terminate()
        sse_queue.put({"type": "done"})

# Map LCME/AOA school names (as displayed in the UI) → keys used in data/faculty_urls.json.
_SCHOOL_NAME_MAP: dict[str, str] = {
    # Exact matches (LCME name == JSON key)
    "Stanford University School of Medicine":                                 "Stanford University School of Medicine",
    "University of California, San Francisco School of Medicine":             "University of California San Francisco School of Medicine",
    "University of California San Francisco School of Medicine":              "University of California San Francisco School of Medicine",
    "Harvard Medical School":                                                 "Harvard Medical School",
    "Johns Hopkins University School of Medicine":                            "Johns Hopkins School of Medicine",
    "Johns Hopkins School of Medicine":                                       "Johns Hopkins School of Medicine",
    "University of Pennsylvania Perelman School of Medicine":                 "University of Pennsylvania Perelman School of Medicine",
    "Columbia University Vagelos College of Physicians and Surgeons":         "Columbia University Vagelos College of Physicians and Surgeons",
    "Duke University School of Medicine":                                     "Duke University School of Medicine",
    "Washington University in St. Louis School of Medicine":                  "Washington University in St. Louis School of Medicine",
    "University of Michigan Medical School":                                  "University of Michigan Medical School",
    "Yale School of Medicine":                                                "Yale School of Medicine",
    "University of Florida College of Medicine":                              "University of Florida College of Medicine",
    # Compound (LCME name → JSON key with affiliated hospital)
    "University of Colorado School of Medicine":                              "University of Colorado School of Medicine / Children's Hospital Colorado",
    "University of Cincinnati College of Medicine":                           "University of Cincinnati College of Medicine / Cincinnati Children's",
    "Baylor College of Medicine":                                             "Baylor College of Medicine / Texas Children's Hospital",
    "University of Washington School of Medicine":                            "University of Washington School of Medicine / Seattle Children's",
    "George Washington University School of Medicine and Health Sciences":    "Children's National Hospital / George Washington University",
    "Northwestern University Feinberg School of Medicine":                    "Northwestern University Feinberg School of Medicine / Lurie Children's",
    "Keck School of Medicine of USC":                                         "Keck School of Medicine USC / Children's Hospital Los Angeles",
    "Keck School of Medicine of the University of Southern California":       "Keck School of Medicine USC / Children's Hospital Los Angeles",
    "Emory University School of Medicine":                                    "Emory University School of Medicine / Children's Healthcare of Atlanta",
    "Ohio State University College of Medicine":                              "Ohio State University College of Medicine / Nationwide Children's",
    "The Ohio State University College of Medicine":                          "Ohio State University College of Medicine / Nationwide Children's",
    # Pipeline hospital names (from PIPELINE_SCHOOLS in frontend)
    "Texas Children's Hospital":                                              "Baylor College of Medicine / Texas Children's Hospital",
    "Cincinnati Children's Hospital":                                         "University of Cincinnati College of Medicine / Cincinnati Children's",
    "Seattle Children's Hospital":                                            "University of Washington School of Medicine / Seattle Children's",
    "Children's National Hospital":                                           "Children's National Hospital / George Washington University",
    "Lurie Children's Hospital":                                              "Northwestern University Feinberg School of Medicine / Lurie Children's",
    "Nationwide Children's Hospital":                                         "Ohio State University College of Medicine / Nationwide Children's",
    "Children's Hospital Los Angeles":                                        "Keck School of Medicine USC / Children's Hospital Los Angeles",
    "Children's Hospital Colorado":                                           "University of Colorado School of Medicine / Children's Hospital Colorado",
    "NIH / NICHD":                                                            "NIH National Institute of Child Health and Human Development (NICHD)",
    # Pediatric cardiology hospital names
    "Boston Children's Hospital":                                             "Harvard Medical School",
    "Children's Hospital of Philadelphia":                                    "University of Pennsylvania Perelman School of Medicine",
    "Rady Children's Hospital":                                               "UC San Diego School of Medicine / Rady Children's Hospital",
    "Duke Children's Hospital and Health Center":                             "Duke University School of Medicine",
    "Cleveland Clinic Children's":                                            "Cleveland Clinic Lerner College of Medicine",
    "New York-Presbyterian Children's Hospital":                              "Columbia University Vagelos College of Physicians and Surgeons",
    "UPMC Children's Hospital of Pittsburgh":                                 "University of Pittsburgh School of Medicine / UPMC Children's Hospital",
    "Lucile Packard Children's Hospital Stanford":                            "Stanford University School of Medicine",
    "Johns Hopkins Children's Center":                                        "Johns Hopkins School of Medicine",
    "C.S. Mott Children's Hospital":                                          "University of Michigan Medical School",
    "UCSF Benioff Children's Hospitals":                                      "University of California San Francisco School of Medicine",
    "UCLA Mattel Children's Hospital":                                        "David Geffen School of Medicine at UCLA / Mattel Children's Hospital",
    "Monroe Carell Jr. Children's Hospital at Vanderbilt":                    "Vanderbilt University School of Medicine / Monroe Carell Jr. Children's",
    "Children's Healthcare of Atlanta":                                       "Emory University School of Medicine / Children's Healthcare of Atlanta",
    "MUSC Children's Heart Program":                                          "Medical University of South Carolina",
    "Levine Children's Hospital":                                             "Atrium Health / Levine Children's Hospital",
    "Children's Medical Center Dallas":                                       "UT Southwestern Medical Center / Children's Medical Center Dallas",
    "Dell Children's Medical Center":                                         "Dell Medical School, University of Texas at Austin / Dell Children's",
    "Intermountain Primary Children's Hospital":                              "University of Utah School of Medicine / Intermountain Primary Children's",
    "Children's Mercy Kansas City":                                           "University of Missouri-Kansas City School of Medicine / Children's Mercy",
    "Nemours Children's Hospital":                                            "Nemours Children's Health",
    # US News top-40 additions
    "UF Health Shands Children's Hospital":                                   "University of Florida College of Medicine",
    "Loma Linda University Children's Hospital":                              "Loma Linda University School of Medicine",
    "Children's Memorial Hermann Hospital":                                   "UTHealth Houston McGovern Medical School",
    "Children's Hospital of Alabama":                                         "University of Alabama at Birmingham Heersink School of Medicine",
    "Rainbow Babies and Children's Hospital":                                 "Case Western Reserve University School of Medicine",
    "Riley Hospital for Children":                                            "Indiana University School of Medicine",
    "Arkansas Children's Hospital":                                           "University of Arkansas for Medical Sciences College of Medicine",
    "Mayo Clinic-Children's Minnesota Cardiovascular Collaborative":          "Mayo Clinic Alix School of Medicine / Children's Minnesota",
    "Virginia Congenital Cardiac Collaborative":                              "University of Virginia School of Medicine",
    "Ochsner Children's Hospital":                                            "Ochsner Clinical School / LSU Health New Orleans",
    "Hassenfeld Children's Hospital at NYU Langone":                          "NYU Grossman School of Medicine",
    "Oklahoma Children's Hospital OU Health":                                 "University of Oklahoma College of Medicine",
    "Johns Hopkins All Children's Hospital":                                  "Johns Hopkins School of Medicine",
}


def _normalize_schools(schools: list[str]) -> list[str]:
    return [_SCHOOL_NAME_MAP.get(s, s) for s in schools]

app = Flask(__name__)

_run_lock   = threading.Lock()
_run_queue: queue.Queue | None = None
_pipeline_running = False

_names_run_lock = threading.Lock()
_names_run_queue: queue.Queue | None = None
_names_pipeline_running = False


def _make_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


_SITE_PASSWORD = os.environ.get("SITE_PASSWORD")

def _check_auth(req) -> bool:
    if not _SITE_PASSWORD:
        return True  # no password set — open access (local dev)
    auth = req.authorization
    return bool(auth and auth.password == _SITE_PASSWORD)

@app.before_request
def require_auth():
    if not _check_auth(request):
        return Response(
            "Access denied.",
            401,
            {"WWW-Authenticate": 'Basic realm="ATLAS"'},
        )


# ─────────────────────────────────────────────
# OpenAI API key management
# The UI shows a first-open popup asking for the user's own OpenAI key. The key
# is verified against the OpenAI API, kept in this process's environment (the
# spawned pipeline children inherit it), and persisted to .env so it survives
# restarts — the popup only appears until a working key has been saved.
# ─────────────────────────────────────────────

_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _save_key_to_env_file(key: str) -> None:
    lines: list[str] = []
    if os.path.exists(_ENV_FILE):
        with open(_ENV_FILE, "r") as f:
            lines = [l for l in f.read().splitlines()
                     if not l.strip().startswith("OPENAI_API_KEY=")]
    lines.append(f"OPENAI_API_KEY={key}")
    with open(_ENV_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


@app.route("/api/key-status")
def key_status():
    return jsonify({"configured": bool(os.environ.get("OPENAI_API_KEY"))})


@app.route("/api/key", methods=["POST"])
def set_key():
    key = ((request.json or {}).get("key") or "").strip()
    # Copy-paste from a browser or email can smuggle in newlines, spaces, or
    # smart quotes; real OpenAI keys are a single ASCII token.
    key = "".join(key.split())
    if not key:
        return jsonify({"error": "No key provided."}), 400
    if not key.isascii():
        return jsonify({"error": "That doesn't look like a valid key — it contains unexpected characters. "
                                 "Re-copy it from platform.openai.com and try again."}), 400
    last_exc: Exception | None = None
    for attempt in range(2):  # one retry absorbs transient network blips
        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            break
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(1)
    else:
        print(f"[key check] could not reach OpenAI: {last_exc!r}")
        return jsonify({"error": "Could not reach OpenAI to verify the key — check your internet connection "
                                 "and try again."}), 502
    if resp.status_code == 401:
        return jsonify({"error": "OpenAI rejected this key. Double-check it and try again."}), 400
    if resp.status_code != 200:
        return jsonify({"error": f"Unexpected response from OpenAI (HTTP {resp.status_code})."}), 502
    # On Render (or any shared deployment — Render sets RENDER=true) the key must
    # stay per-visitor: the browser stores it and sends it with each run request.
    # Never put it in the server environment there, where it would silently pay
    # for every other visitor's runs.
    if not os.environ.get("RENDER"):
        os.environ["OPENAI_API_KEY"] = key
        try:
            _save_key_to_env_file(key)
        except OSError:
            pass  # key still works for this session even if .env is unwritable
    return jsonify({"ok": True})


_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(_ASSETS_DIR, filename)


@app.route("/")
def index():
    with open("medfaculty.html", "r") as f:
        html = f.read()
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/run", methods=["POST"])
def start_run():
    global _run_queue, _pipeline_running

    data    = request.json or {}
    api_key = (data.get("api_key") or "").strip() or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "No OpenAI API key saved — add your key to run a search.",
                        "code": "no_api_key"}), 400

    with _run_lock:
        if _pipeline_running:
            return jsonify({"error": "Pipeline already running"}), 409
        _pipeline_running = True

    schools        = _normalize_schools(data.get("schools") or ["Stanford"])
    discipline     = data.get("discipline") or "Pediatric Endocrinology"
    considerations = data.get("considerations") or ""
    _run_queue = queue.Queue()

    def run():
        global _pipeline_running
        try:
            _spawn_and_forward(
                "search",
                {"schools": schools, "discipline": discipline, "considerations": considerations,
                 "api_key": api_key},
                _run_queue,
            )
        finally:
            _pipeline_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


# ─────────────────────────────────────────────
# Demo mode: replay a pre-captured run through the same SSE queue so the UI
# behaves exactly like a live run without waiting on scraping/enrichment.
# ─────────────────────────────────────────────

_DEMO_FILE = os.path.join("data", "demo", "pediatric_endocrinology_stanford_harvard_hopkins.json")

# Keys stripped from the initial "faculty" event so QC badges stay pending
# until the replayed QC phase, matching real-run behavior.
_DEMO_QC_KEYS = ("qc", "qc_nih", "qc_hindex", "qc_roles", "qc_overall")


def _replay_demo(records: list, q: "queue.Queue") -> None:
    """Push faculty/qc_update messages with pacing that mimics a live run."""
    by_school: "OrderedDict[str, list]" = OrderedDict()
    for rec in records:
        by_school.setdefault(rec.get("school", ""), []).append(rec)

    total, done = len(records), 0
    # ~22s enrichment phase regardless of record count, clamped to feel live.
    delay = min(0.8, max(0.3, 22.0 / max(total, 1)))
    time.sleep(1.5)  # scrape phase
    for school_recs in by_school.values():  # one school streams at a time
        for rec in school_recs:
            done += 1
            data = {k: v for k, v in rec.items() if k not in _DEMO_QC_KEYS}
            q.put({"type": "faculty", "data": data, "completed": done, "total": total})
            time.sleep(random.uniform(delay * 0.7, delay * 1.3))
    for rec in records:  # QC phase — badges flip one by one
        q.put({"type": "qc_update",
               "data": {"full_name": rec.get("full_name"),
                        "qc": rec.get("qc") or {"overall": "yellow"}}})
        time.sleep(random.uniform(0.08, 0.2))


@app.route("/api/run-demo", methods=["POST"])
def start_demo_run():
    global _run_queue, _pipeline_running

    # Validate the demo file BEFORE taking the lock so a bad file can never
    # leave _pipeline_running wedged.
    try:
        with open(_DEMO_FILE, "r") as f:
            records = json.load(f)
        if not isinstance(records, list) or not records:
            raise ValueError("demo file is empty or not a list")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        return jsonify({"error": f"Demo data unavailable: {exc}"}), 500

    with _run_lock:
        if _pipeline_running:
            return jsonify({"error": "Pipeline already running"}), 409
        _pipeline_running = True

    _run_queue = queue.Queue()
    q = _run_queue

    def run():
        global _pipeline_running
        try:
            _replay_demo(records, q)
        except Exception as exc:  # noqa: BLE001
            q.put({"type": "error", "message": f"demo replay failed: {exc}"})
        finally:
            q.put({"type": "done"})
            _pipeline_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/stream")
def stream():
    q = _run_queue

    def generate():
        if q is None:
            yield _make_sse({"type": "error", "message": "No pipeline running"})
            return
        while True:
            try:
                msg = q.get(timeout=30)
                yield _make_sse(msg)
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield _make_sse({"type": "ping"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


@app.route("/api/run-names", methods=["POST"])
def start_names_run():
    global _names_run_queue, _names_pipeline_running

    data    = request.json or {}
    api_key = (data.get("api_key") or "").strip() or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "No OpenAI API key saved — add your key to run a lookup.",
                        "code": "no_api_key"}), 400

    with _names_run_lock:
        if _names_pipeline_running:
            return jsonify({"error": "Names pipeline already running"}), 409
        _names_pipeline_running = True

    names          = data.get("names") or []
    discipline     = data.get("discipline") or ""
    considerations = data.get("considerations") or ""

    records = [
        {
            "full_name":   n.get("full_name", "").strip(),
            "title":       None,
            "subspecialty": None,
            "email":       None,
            "profile_url": None,
            "school":      n.get("institution", "").strip(),
        }
        for n in names
        if n.get("full_name", "").strip() and n.get("institution", "").strip()
    ]

    if not records:
        _names_pipeline_running = False
        return jsonify({"error": "No valid name + institution pairs provided"}), 400

    _names_run_queue = queue.Queue()

    def run():
        global _names_pipeline_running
        try:
            _spawn_and_forward(
                "names",
                {"records": records, "discipline": discipline, "considerations": considerations,
                 "api_key": api_key},
                _names_run_queue,
            )
        finally:
            _names_pipeline_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/stream-names")
def stream_names():
    q = _names_run_queue

    def generate():
        if q is None:
            yield _make_sse({"type": "error", "message": "No names pipeline running"})
            return
        while True:
            try:
                msg = q.get(timeout=30)
                yield _make_sse(msg)
                if msg["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield _make_sse({"type": "ping"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"[Server] Starting at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
