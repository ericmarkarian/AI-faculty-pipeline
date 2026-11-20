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
import threading
import time

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

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

# Largest pasted page accepted by /api/roster-paste (kept in step with pasted_roster.MAX_PASTE_CHARS;
# not imported from there so this process never loads the pipeline's dependencies).
_MAX_PASTE_CHARS = 3_000_000


def _pipeline_child(kind: str, payload: dict, mpq) -> None:
    """Entry point that runs in the spawned process; streams results back via mpq."""
    def on_enriched(rec, completed, total):
        mpq.put({"type": "faculty", "data": rec, "completed": completed, "total": total})

    def on_qc_complete(rec):
        mpq.put({"type": "qc_update", "data": rec})

    def on_discovery(report):
        # Per-school discovery outcome — lets the UI say which schools came back
        # empty or had a directory page that could not be read.
        mpq.put({"type": "discovery", "data": report})

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
                on_discovery=on_discovery,
                pasted_rosters=payload.get("pasted_rosters"),
            )
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        mpq.put({"type": "error", "message": str(exc)})
    finally:
        mpq.put({"type": "__child_done__"})


def _paste_child(payload: dict, mpq) -> None:
    """Runs in a spawned process: read one pasted roster page and send back the people."""
    try:
        if payload.get("api_key"):
            os.environ["OPENAI_API_KEY"] = payload["api_key"]
        import observe
        from openai import OpenAI
        from providers.pasted_roster import read_pasted_roster

        client = observe.instrument_client(OpenAI(api_key=os.environ.get("OPENAI_API_KEY")))
        result = read_pasted_roster(
            payload["html"], payload["text"], payload["url"],
            payload["school"], payload["discipline"], client,
        )
        mpq.put({"ok": True, **result})
    except Exception as exc:  # noqa: BLE001 — every failure goes back to the browser as a message
        mpq.put({"ok": False, "error": str(exc)})


def _call_in_child(target, payload: dict, timeout: float = 240.0) -> dict:
    """Run `target(payload, queue)` in a fresh process and return the one message it sends.

    Same reason as the pipeline runs: the OpenAI client and HTML parsing load in a
    process that exits, so the long-lived web worker stays small, and a per-request
    API key never touches this process's environment.
    """
    mpq = _MP_CTX.Queue()
    proc = _MP_CTX.Process(target=target, args=(payload, mpq), daemon=True)
    proc.start()
    try:
        deadline = time.time() + timeout
        while True:
            try:
                return mpq.get(timeout=1)
            except queue.Empty:
                if not proc.is_alive():
                    try:
                        return mpq.get(timeout=1)   # it may have answered just before exiting
                    except queue.Empty:
                        return {"ok": False, "error": "the reader stopped unexpectedly"}
                if time.time() > deadline:
                    return {"ok": False, "error": "reading the page took too long"}
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()


# ─────────────────────────────────────────────
# Run persistence
# The browser's live SSE view is just a viewer: the server keeps its own copy of
# every run's records so closing the tab loses nothing. /api/last-run serves the
# copy back, and completed runs are snapshotted to disk so they survive a worker
# restart (though not a redeploy — Render's disk is ephemeral).
# ─────────────────────────────────────────────

_LAST_RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_last_runs_lock = threading.Lock()
_last_runs: dict = {"search": None, "names": None}


def _last_run_file(kind: str) -> str:
    return os.path.join(_LAST_RUN_DIR, f"last_run_{kind}.json")


def _start_run_state(kind: str, **meta) -> dict:
    state = {"status": "running", "started_at": time.time(), "progress": None,
             "error": None, "records": {}, **meta}
    with _last_runs_lock:
        _last_runs[kind] = state
    return state


def _run_state_apply(state: dict, msg: dict) -> None:
    """Fold one pipeline message into the server-side copy of the run."""
    mtype = msg.get("type")
    data = msg.get("data")
    # "faculty" / "qc_update" carry one record (a dict); "discovery" carries a list,
    # one summary per school — so never assume the payload is a dict.
    name = data.get("full_name") if isinstance(data, dict) else None
    with _last_runs_lock:
        if mtype == "faculty" and name:
            state["records"][name] = data
            state["progress"] = {"done": msg.get("completed"), "total": msg.get("total")}
        elif mtype == "qc_update" and name:
            # Real runs re-send the full record here; merge so a badge-only
            # update never erases fields we already have.
            state["records"][name] = {**state["records"].get(name, {}), **data}
        elif mtype == "discovery":
            state["discovery"] = data if isinstance(data, list) else []
        elif mtype == "error":
            state["error"] = msg.get("message")


def _finish_run_state(kind: str, state: dict) -> None:
    with _last_runs_lock:
        state["status"] = "error" if state["error"] else "completed"
        snapshot = {k: (list(v.values()) if k == "records" else v) for k, v in state.items()}
    try:
        with open(_last_run_file(kind), "w") as f:
            json.dump(snapshot, f)
    except OSError:
        pass  # persistence is best-effort; the in-memory copy still serves


def _load_persisted_runs() -> None:
    """On boot, reload completed runs so a worker restart doesn't lose them."""
    for kind in _last_runs:
        try:
            with open(_last_run_file(kind), "r") as f:
                snap = json.load(f)
            snap["records"] = {r.get("full_name"): r for r in snap.get("records", [])
                               if r.get("full_name")}
            if snap.get("status") == "running":  # died mid-run with the old worker
                snap["status"] = "error"
                snap["error"] = snap.get("error") or "server restarted mid-run"
            _last_runs[kind] = snap
        except (OSError, ValueError):
            continue


_load_persisted_runs()


def _spawn_and_forward(kind: str, payload: dict, sse_queue: "queue.Queue",
                       run_state: dict) -> None:
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
                    msg = {"type": "error",
                           "message": "pipeline process exited unexpectedly (likely out of memory)"}
                    _run_state_apply(run_state, msg)
                    sse_queue.put(msg)
                    break
                continue
            if msg.get("type") == "__child_done__":
                break
            _run_state_apply(run_state, msg)
            sse_queue.put(msg)
    except Exception as exc:  # noqa: BLE001
        msg = {"type": "error", "message": f"failed to start pipeline: {exc}"}
        _run_state_apply(run_state, msg)
        sse_queue.put(msg)
    finally:
        if proc is not None:
            proc.join(timeout=20)
            if proc.is_alive():
                proc.terminate()
        _finish_run_state(kind, run_state)
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


def _school_labels(selected: list[str]) -> dict[str, str]:
    """Pipeline school name -> the name the user picked for it in the UI.

    Records carry the pipeline name ("Harvard Medical School"); the user picked
    "Boston Children's Hospital". The UI and the CSV export show the picked name so
    a search for the hospital finds its rows. First pick wins when two selections
    resolve to the same pipeline school.
    """
    labels: dict[str, str] = {}
    for name in selected:
        labels.setdefault(_SCHOOL_NAME_MAP.get(name, name), name)
    return labels

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

    selected       = data.get("schools") or ["Stanford"]
    schools        = _normalize_schools(selected)
    school_labels  = _school_labels(selected)
    discipline     = data.get("discipline") or "Pediatric Endocrinology"
    considerations = data.get("considerations") or ""
    # Rosters the visitor pasted for pages the scraper could not read. Kept in their
    # browser and sent with each run; validated inside the pipeline before use.
    pasted_rosters = data.get("pasted_rosters") if isinstance(data.get("pasted_rosters"), list) else []
    _run_queue = queue.Queue()

    run_state = _start_run_state("search", discipline=discipline, schools=schools,
                                 school_labels=school_labels, discovery=[])

    def run():
        global _pipeline_running
        try:
            _spawn_and_forward(
                "search",
                {"schools": schools, "discipline": discipline, "considerations": considerations,
                 "api_key": api_key, "pasted_rosters": pasted_rosters},
                _run_queue,
                run_state,
            )
        finally:
            _pipeline_running = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"status": "started", "school_labels": school_labels})


@app.route("/api/roster-paste", methods=["POST"])
def roster_paste():
    """Read a roster page the visitor opened and pasted in, for a page the scraper could not read.

    Returns the people found; the browser keeps them and sends them back with later
    runs (see /api/run "pasted_rosters"). Nothing is stored on the server.
    """
    data = request.json or {}
    api_key = (data.get("api_key") or "").strip() or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return jsonify({"error": "No OpenAI API key saved — add your key to read a pasted page.",
                        "code": "no_api_key"}), 400

    fields = {k: data.get(k) if isinstance(data.get(k), str) else "" for k in
              ("html", "text", "url", "school", "discipline")}
    if not fields["school"].strip() or not fields["discipline"].strip():
        return jsonify({"error": "Missing the institution or specialty for this page."}), 400
    if not (fields["html"].strip() or fields["text"].strip()):
        return jsonify({"error": "Nothing was pasted."}), 400
    if len(fields["html"]) + len(fields["text"]) > _MAX_PASTE_CHARS:
        return jsonify({"error": "That is too much text to read in one go — copy just the part of the "
                                 "page that lists the team."}), 413

    result = _call_in_child(_paste_child, {**fields, "api_key": api_key})
    if not result.get("ok"):
        return jsonify({"error": f"Could not read the pasted page: {result.get('error') or 'unknown error'}"}), 502
    return jsonify({
        "people": result.get("people") or [],
        "left_out": result.get("left_out") or {},
        "used_formatting": bool(result.get("used_formatting")),
        "captured": time.strftime("%Y-%m-%d"),
    })


@app.route("/api/last-run")
def last_run():
    """The server's own copy of the most recent run — closing the tab loses nothing."""
    kind = request.args.get("kind", "search")
    with _last_runs_lock:
        state = _last_runs.get(kind)
        if not state:
            return jsonify({"status": "none"})
        return jsonify({
            "status":     state.get("status"),
            "discipline": state.get("discipline"),
            "schools":    state.get("schools"),
            "school_labels": state.get("school_labels") or {},
            "discovery":  state.get("discovery") or [],
            "started_at": state.get("started_at"),
            "progress":   state.get("progress"),
            "error":      state.get("error"),
            "records":    list(state.get("records", {}).values()),
        })


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

    run_state = _start_run_state("names", discipline=discipline,
                                 schools=sorted({r["school"] for r in records}))

    def run():
        global _names_pipeline_running
        try:
            _spawn_and_forward(
                "names",
                {"records": records, "discipline": discipline, "considerations": considerations,
                 "api_key": api_key},
                _names_run_queue,
                run_state,
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
