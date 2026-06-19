"""
Flask SSE server for ATLAS — Academic Talent Location And Sourcing.

Usage:
    python3 server.py
    # then open http://localhost:5050
"""
import json
import os
import queue
import threading

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file

load_dotenv()

import faculty_search  # noqa: E402 — must come after load_dotenv

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

    with _run_lock:
        if _pipeline_running:
            return jsonify({"error": "Pipeline already running"}), 409
        _pipeline_running = True

    data           = request.json or {}
    schools        = _normalize_schools(data.get("schools") or ["Stanford"])
    discipline     = data.get("discipline") or "Pediatric Endocrinology"
    considerations = data.get("considerations") or ""
    _run_queue = queue.Queue()

    def on_enriched(faculty_record: dict, completed: int, total: int):
        _run_queue.put({
            "type":      "faculty",
            "data":      faculty_record,
            "completed": completed,
            "total":     total,
        })

    def on_qc_complete(faculty_record: dict):
        _run_queue.put({
            "type": "qc_update",
            "data": faculty_record,
        })

    def run():
        global _pipeline_running
        try:
            faculty_search.run_pipeline(
                schools=schools,
                discipline=discipline,
                on_enriched=on_enriched,
                on_qc_complete=on_qc_complete,
                considerations=considerations,
            )
        except Exception as exc:
            _run_queue.put({"type": "error", "message": str(exc)})
        finally:
            _run_queue.put({"type": "done"})
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

    with _names_run_lock:
        if _names_pipeline_running:
            return jsonify({"error": "Names pipeline already running"}), 409
        _names_pipeline_running = True

    data           = request.json or {}
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

    def on_enriched(faculty_record: dict, completed: int, total: int):
        _names_run_queue.put({
            "type":      "faculty",
            "data":      faculty_record,
            "completed": completed,
            "total":     total,
        })

    def on_qc_complete(faculty_record: dict):
        _names_run_queue.put({
            "type": "qc_update",
            "data": faculty_record,
        })

    def run():
        global _names_pipeline_running
        try:
            faculty_search.run_pipeline_from_records(
                records,
                discipline=discipline,
                on_enriched=on_enriched,
                on_qc_complete=on_qc_complete,
                considerations=considerations,
            )
        except Exception as exc:
            _names_run_queue.put({"type": "error", "message": str(exc)})
        finally:
            _names_run_queue.put({"type": "done"})
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
