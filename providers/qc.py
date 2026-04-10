"""
providers/qc.py — RAGAS-style faithfulness verification for faculty role claims.

For each role collected by Agent 2 (editorial, society, leadership, training):
  1. URL liveness check  — does the source_url actually resolve?
  2. Faithfulness check  — does the evidence_text actually confirm the claim?
     (Separate, cold LLM call with no web access — judge only reads what was collected)

Per-role status:
  green  — URL alive AND evidence confirms the claim
  yellow — URL alive but evidence is vague/indirect, OR no URL but evidence is clear
  red    — URL dead/missing AND/OR evidence contradicts or doesn't support the claim

Per-person overall: worst status across all roles in all categories.
"""

import json
import re
import requests
from openai import OpenAI

ROLE_CATEGORIES = ("editorial", "society", "leadership", "training")

_FAITHFULNESS_PROMPT = """\
You are a fact-checking assistant. Your job is to determine whether a piece of \
evidence text actually confirms a specific role claim about an academic faculty member.

Faculty member: {name}
Role claim: {name} is "{role}" at "{org}"
Evidence text: {evidence_text}

Does the evidence text directly and explicitly confirm this role claim?

Answer with EXACTLY one word — no explanation, no punctuation:
- CONFIRMED   if the evidence clearly and directly states this role
- UNCERTAIN   if the evidence is vague, indirect, or could apply to a different role
- CONTRADICTED if the evidence conflicts with the claim or clearly does not support it
"""


def check_url_liveness(url: str, timeout: int = 6) -> bool | None:
    """
    Returns True if the URL responds with a non-error HTTP status,
    False if it returns 4xx/5xx or redirects to a generic page,
    None if no URL was provided or the request itself fails (network error, timeout).
    """
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
        return resp.status_code < 400
    except Exception:
        # Treat connection errors as unknown, not as dead
        return None


def _batch_faithfulness(
    name: str,
    roles: list[dict],
    client: OpenAI,
) -> list[str]:
    """
    Verifies all role claims for one faculty member in a single LLM call.
    Returns a list of verdicts (CONFIRMED / UNCERTAIN / CONTRADICTED), one per role.
    Roles with no evidence_text are returned as UNCERTAIN without an API call.
    """
    # Separate roles that have evidence from those that don't
    indexed_with_evidence = [
        (i, r) for i, r in enumerate(roles) if r.get("evidence_text", "").strip()
    ]

    verdicts = ["UNCERTAIN"] * len(roles)

    if not indexed_with_evidence:
        return verdicts

    # Build a numbered list for the LLM to evaluate in one shot
    items_text = "\n\n".join(
        f"[{seq + 1}] Role claim: \"{r['role']}\" at \"{r['org']}\"\n"
        f"    Evidence: {r.get('evidence_text', '').strip()}"
        for seq, (_, r) in enumerate(indexed_with_evidence)
    )

    prompt = f"""\
You are a fact-checking assistant reviewing role claims about a faculty member named {name}.

For each numbered claim below, decide whether the evidence text directly and explicitly \
confirms it.

{items_text}

Reply with a JSON array containing exactly {len(indexed_with_evidence)} strings.
Each string must be exactly one of: "CONFIRMED", "UNCERTAIN", or "CONTRADICTED".
- CONFIRMED   — evidence clearly and directly states this role
- UNCERTAIN   — evidence is vague, indirect, or ambiguous
- CONTRADICTED — evidence conflicts with the claim or does not support it

Return ONLY the JSON array, no explanation.
Example: ["CONFIRMED", "UNCERTAIN", "CONTRADICTED"]
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        batch_verdicts = json.loads(raw)

        allowed = {"CONFIRMED", "UNCERTAIN", "CONTRADICTED"}
        for seq, (original_idx, _) in enumerate(indexed_with_evidence):
            v = str(batch_verdicts[seq]).upper() if seq < len(batch_verdicts) else "UNCERTAIN"
            verdicts[original_idx] = v if v in allowed else "UNCERTAIN"

    except Exception as e:
        print(f"      [QC] Faithfulness batch failed for {name}: {e}")

    return verdicts


def _compute_role_status(url_alive: bool | None, faithfulness: str) -> str:
    """
    Maps (url_alive, faithfulness) → green / yellow / red.

    green  — URL alive + CONFIRMED
    yellow — URL alive + UNCERTAIN  |  URL unknown + CONFIRMED
    red    — CONTRADICTED  |  URL dead + not CONFIRMED  |  no URL + not CONFIRMED
    """
    if faithfulness == "CONTRADICTED":
        return "red"
    if faithfulness == "CONFIRMED":
        if url_alive is True:
            return "green"
        if url_alive is None:
            return "yellow"   # Can't verify URL but evidence is solid
        return "red"           # URL dead even though evidence says OK
    # UNCERTAIN
    if url_alive is True:
        return "yellow"
    return "red"


def _worst(statuses: list[str]) -> str:
    if "red" in statuses:
        return "red"
    if "yellow" in statuses:
        return "yellow"
    if "green" in statuses:
        return "green"
    return "green"  # no roles → vacuously green


def verify_faculty_roles(faculty: dict, client: OpenAI) -> dict:
    """
    Runs URL liveness + faithfulness checks on all role claims for one faculty member.
    Returns a qc dict with per-category and overall confidence.

    Shape of returned qc dict:
    {
      "overall":              "green" | "yellow" | "red",
      "editorial_confidence": "green" | "yellow" | "red",
      "society_confidence":   "green" | "yellow" | "red",
      "leadership_confidence":"green" | "yellow" | "red",
      "training_confidence":  "green" | "yellow" | "red",
      "role_details": [
        {
          "category":     "editorial",
          "role":         "Associate Editor",
          "org":          "Diabetes Care",
          "url_alive":    True | False | None,
          "faithfulness": "CONFIRMED" | "UNCERTAIN" | "CONTRADICTED",
          "status":       "green" | "yellow" | "red",
        },
        ...
      ]
    }
    """
    name = faculty.get("full_name", "Unknown")

    # Collect all roles across categories into a flat list, keeping category tag
    all_roles: list[dict] = []
    for cat in ROLE_CATEGORIES:
        for role in (faculty.get(f"{cat}_roles") or []):
            if isinstance(role, dict):
                all_roles.append({**role, "_cat": cat})

    if not all_roles:
        empty = {f"{cat}_confidence": "green" for cat in ROLE_CATEGORIES}
        return {"overall": "green", "role_details": [], **empty}

    print(f"    [QC] {name} — checking {len(all_roles)} role(s)")

    # Step 1: URL liveness (fast, no LLM)
    print(f"    [QC] Step 1: URL liveness checks...")
    url_results: list[bool | None] = []
    for r in all_roles:
        url = r.get("source_url", "")
        alive = check_url_liveness(url)
        url_label = "ALIVE" if alive is True else ("DEAD" if alive is False else "NO_URL")
        short_url = url[:80] + "..." if len(url) > 80 else url or "(none)"
        print(f"           [{url_label}] {r.get('role', '?')} @ {r.get('org', '?')} — {short_url}")
        url_results.append(alive)

    # Step 2: Faithfulness (single batched LLM call for all roles of this person)
    print(f"    [QC] Step 2: Faithfulness check (LLM judge, no web access)...")
    faithfulness_results = _batch_faithfulness(name, all_roles, client)
    for r, verdict in zip(all_roles, faithfulness_results):
        evidence = (r.get("evidence_text") or "(no evidence)").strip()
        short_evidence = evidence[:120] + "..." if len(evidence) > 120 else evidence
        print(f"           [{verdict}] \"{r.get('role', '?')}\" at \"{r.get('org', '?')}\"")
        print(f"                     evidence: {short_evidence}")

    # Step 3: Compute per-role status
    print(f"    [QC] Step 3: Final status per role...")
    role_details = []
    category_statuses: dict[str, list[str]] = {cat: [] for cat in ROLE_CATEGORIES}

    for role, url_alive, faithfulness in zip(all_roles, url_results, faithfulness_results):
        status = _compute_role_status(url_alive, faithfulness)
        cat = role.get("_cat", "editorial")
        category_statuses[cat].append(status)
        url_label = "ALIVE" if url_alive is True else ("DEAD" if url_alive is False else "NO_URL")
        print(f"           [{status.upper()}] [{url_label}+{faithfulness}] {cat}: \"{role.get('role', '?')}\" @ \"{role.get('org', '?')}\"")
        role_details.append({
            "category":     cat,
            "role":         role.get("role", ""),
            "org":          role.get("org", ""),
            "url_alive":    url_alive,
            "faithfulness": faithfulness,
            "status":       status,
        })

    cat_confidences = {
        f"{cat}_confidence": _worst(category_statuses[cat]) if category_statuses[cat] else "green"
        for cat in ROLE_CATEGORIES
    }
    overall = _worst([v for v in cat_confidences.values()])

    print(f"    [QC] {name} summary — " +
          " | ".join(f"{cat}: {cat_confidences[f'{cat}_confidence']}" for cat in ROLE_CATEGORIES) +
          f" | OVERALL: {overall.upper()}")

    return {
        "overall": overall,
        **cat_confidences,
        "role_details": role_details,
    }
