"""
Flask SSE server for the Faculty Intelligence pipeline.

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
}


def _normalize_schools(schools: list[str]) -> list[str]:
    return [_SCHOOL_NAME_MAP.get(s, s) for s in schools]

app = Flask(__name__)

_run_lock   = threading.Lock()
_run_queue: queue.Queue | None = None
_pipeline_running = False


def _make_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.route("/")
def index():
    resp = send_file("medfaculty.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/run", methods=["POST"])
def start_run():
    global _run_queue, _pipeline_running

    with _run_lock:
        if _pipeline_running:
            return jsonify({"error": "Pipeline already running"}), 409
        _pipeline_running = True

    data       = request.json or {}
    schools    = _normalize_schools(data.get("schools") or ["Stanford"])
    discipline = data.get("discipline") or "Pediatric Endocrinology"
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"[Server] Starting at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
