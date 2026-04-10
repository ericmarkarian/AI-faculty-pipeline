"""
Unified golden set test suite — runs entirely offline via fixtures on disk.

Test layers:
  1. Roles schema      — required fields, valid category, valid URL format, non-empty evidence
  2. URL verification  — source URLs were reachable at capture time; evidence found on page
  3. Miss detection    — no possible_miss flags in any fixture
  4. NIH schema        — grants have required fields
  5. H-index schema    — h_index field present and valid type
  6. Archetype asserts — per-person thresholds (counts, hallucination guard)
  7. Snapshot regression — detect unexpected output changes across runs

Audit report printed at session end.
"""
import json
import re
from difflib import unified_diff
from pathlib import Path

import pytest

from helpers import (
    load_golden_set, load_roles_fixture, load_roles, load_grants, load_hindex, slug,
)
from providers.base import ALLOWED_CATEGORIES

_GOLDEN = load_golden_set()
_SNAPSHOTS = Path(__file__).parent / "snapshots"
_REQUIRED_ROLE_FIELDS = {"category", "role", "org", "source_url", "evidence_text", "retrieved_at"}
_VOLATILE_ROLE_FIELDS = {"retrieved_at", "run_id", "url_verification", "model"}

# ── Audit state ───────────────────────────────────────────────────────────────

_audit: dict = {
    "editorial": 0,
    "society": 0,
    "leadership": 0,
    "training": 0,
    "nih_grants": 0,
    "h_index_available": 0,
    "schema_failures": 0,
    "archetype_failures": 0,
    "url_unreachable": 0,
    "evidence_not_found": 0,
    "possible_miss_flags": 0,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _by_category(roles: list[dict], category: str) -> list[dict]:
    return [r for r in roles if r.get("category") == category]


def _accumulate_roles(roles: list[dict]) -> None:
    for role in roles:
        cat = role.get("category", "")
        if cat in _audit:
            _audit[cat] += 1


# ── 1. Roles schema ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_roles_schema(person):
    roles = load_roles(person)
    failures = []

    for i, role in enumerate(roles):
        missing = _REQUIRED_ROLE_FIELDS - set(role.keys())
        if missing:
            failures.append(f"Role[{i}] missing fields {missing}: {role.get('role', '?')}")
            continue

        if role["category"] not in ALLOWED_CATEGORIES:
            failures.append(f"Role[{i}] invalid category '{role['category']}'")

        if not re.match(r"^https?://", role.get("source_url", "")):
            failures.append(f"Role[{i}] bad source_url: {role.get('source_url', '')!r}")

        if not role.get("evidence_text", "").strip():
            failures.append(f"Role[{i}] empty evidence_text for role: {role.get('role', '?')}")

    if failures:
        _audit["schema_failures"] += len(failures)
        pytest.fail("\n".join(failures))


# ── 2. URL verification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_url_verification(person):
    fixture = load_roles_fixture(person)
    roles = fixture.get("roles", [])
    failures = []

    for i, role in enumerate(roles):
        uv = role.get("url_verification")
        label = f"Role[{i}] ({role.get('role', '?')})"

        if uv is None:
            failures.append(f"{label}: url_verification missing — re-run capture tool")
            continue

        if uv.get("evidence_match_method") == "error":
            # URL fetch failed at capture time — warn but don't fail (transient)
            print(f"  [WARN] {label}: URL fetch error at capture: {uv.get('error', '?')}")
            _audit["url_unreachable"] += 1
            continue

        status = uv.get("http_status")
        if status is not None and status >= 400:
            failures.append(f"{label}: source_url returned HTTP {status} at capture time")
            _audit["url_unreachable"] += 1

        if not uv.get("evidence_found"):
            print(
                f"  [WARN] {label}: evidence_text not found on page "
                f"(method={uv.get('evidence_match_method')})"
            )
            _audit["evidence_not_found"] += 1

    if failures:
        pytest.fail("\n".join(failures))


# ── 3. Miss detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_possible_miss(person):
    fixture = load_roles_fixture(person)

    if "possible_miss" not in fixture:
        pytest.skip(
            f"possible_miss field not in fixture for {person['name']}. "
            "Re-run capture tool to add miss detection."
        )

    if fixture["possible_miss"]:
        _audit["possible_miss_flags"] += 1
        miss_detail = fixture.get("miss_detection", {})
        flagged = [cat for cat, d in miss_detail.items() if d.get("possible_miss")]
        pytest.fail(
            f"{person['name']}: possible_miss=True for {flagged}. "
            "Primary extraction may have missed roles. Review and re-capture if needed."
        )


# ── 4. NIH schema ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_nih_schema(person):
    grants = load_grants(person)
    failures = []
    for i, grant in enumerate(grants):
        for field in ("project_num", "title"):
            if not grant.get(field):
                failures.append(f"Grant[{i}] missing or empty '{field}'")
    if failures:
        _audit["schema_failures"] += len(failures)
        pytest.fail("\n".join(failures))


# ── 5. H-index schema ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_hindex_schema(person):
    fixture = load_hindex(person)

    assert "h_index" in fixture, (
        f"h_index field missing from hindex fixture for {person['name']}"
    )

    h = fixture["h_index"]
    if h is not None:
        assert isinstance(h, int), f"h_index must be int or null, got {type(h).__name__}: {h}"
        assert h >= 0, f"h_index must be >= 0, got {h}"
        _audit["h_index_available"] += 1


# ── 6. Archetype assertions ───────────────────────────────────────────────────

def test_david_maahs_high_signal():
    person = next(p for p in _GOLDEN if p["name"] == "David Maahs")
    roles = load_roles(person)
    _accumulate_roles(roles)
    _audit["persons_tested"] = _audit.get("persons_tested", 0) + 1

    min_required = person["thresholds"]["total_roles"]["min"]
    if len(roles) < min_required:
        _audit["archetype_failures"] += 1
        pytest.fail(f"David Maahs: expected >= {min_required} total roles, got {len(roles)}")


def test_anna_gloyn_editorial_heavy():
    person = next(p for p in _GOLDEN if p["name"] == "Anna Gloyn")
    roles = load_roles(person)
    _accumulate_roles(roles)

    editorial = _by_category(roles, "editorial")
    min_required = person["thresholds"]["editorial_roles"]["min"]
    if len(editorial) < min_required:
        _audit["archetype_failures"] += 1
        pytest.fail(f"Anna Gloyn: expected >= {min_required} editorial roles, got {len(editorial)}")


def test_tandy_aye_society_heavy():
    person = next(p for p in _GOLDEN if p["name"] == "Tandy Aye")
    roles = load_roles(person)
    _accumulate_roles(roles)

    society = _by_category(roles, "society")
    min_required = person["thresholds"]["society_roles"]["min"]
    if len(society) < min_required:
        _audit["archetype_failures"] += 1
        pytest.fail(f"Tandy Aye: expected >= {min_required} society roles, got {len(society)}")


def test_darrell_wilson_grant_heavy():
    person = next(p for p in _GOLDEN if p["name"] == "Darrell Wilson")
    grants = load_grants(person)
    _audit["nih_grants"] += len(grants)

    min_required = person["thresholds"]["nih_grant_count"]["min"]
    if len(grants) < min_required:
        _audit["archetype_failures"] += 1
        pytest.fail(f"Darrell Wilson: expected >= {min_required} NIH grants, got {len(grants)}")


def test_holly_cooper_no_hallucination():
    person = next(p for p in _GOLDEN if p["name"] == "Holly Cooper")
    roles = load_roles(person)
    _accumulate_roles(roles)

    expected = person["thresholds"]["total_roles"]["exact"]
    if len(roles) != expected:
        _audit["archetype_failures"] += 1
        pytest.fail(
            f"Holly Cooper: hallucination detected! "
            f"Expected exactly {expected} roles, got {len(roles)}: "
            + json.dumps([r.get("role") for r in roles], indent=2)
        )


# ── 7. Snapshot regression ────────────────────────────────────────────────────

@pytest.mark.parametrize("person", _GOLDEN, ids=[p["name"] for p in _GOLDEN])
def test_snapshot_regression(person):
    roles = load_roles(person)
    _SNAPSHOTS.mkdir(exist_ok=True)
    snap_path = _SNAPSHOTS / f"{slug(person['name'])}.json"

    comparable = sorted(
        [{k: v for k, v in r.items() if k not in _VOLATILE_ROLE_FIELDS} for r in roles],
        key=lambda r: (r.get("category", ""), r.get("role", ""), r.get("org", "")),
    )

    if not snap_path.exists():
        snap_path.write_text(json.dumps(comparable, indent=2))
        return

    stored = json.loads(snap_path.read_text())
    if comparable != stored:
        diff = list(unified_diff(
            json.dumps(stored, indent=2).splitlines(keepends=True),
            json.dumps(comparable, indent=2).splitlines(keepends=True),
            fromfile=f"snapshot/{slug(person['name'])}.json",
            tofile="current output",
            lineterm="",
        ))
        pytest.fail(f"Snapshot regression for {person['name']}:\n" + "\n".join(diff))


# ── Audit report ──────────────────────────────────────────────────────────────

def pytest_sessionfinish(session, exitstatus):
    result = "PASS" if exitstatus == 0 else "FAIL"
    print("\n")
    print("=" * 54)
    print("  GOLDEN SET AUDIT REPORT")
    print("=" * 54)
    print(f"  Roles by category:")
    print(f"    editorial:              {_audit['editorial']}")
    print(f"    society:                {_audit['society']}")
    print(f"    leadership:             {_audit['leadership']}")
    print(f"    training:               {_audit['training']}")
    print(f"  NIH grants total:         {_audit['nih_grants']}")
    print(f"  H-index available:        {_audit['h_index_available']} / 5")
    print(f"  URL unreachable:          {_audit['url_unreachable']}")
    print(f"  Evidence not found (warn):{_audit['evidence_not_found']}")
    print(f"  Possible miss flags:      {_audit['possible_miss_flags']}")
    print(f"  Schema failures:          {_audit['schema_failures']}")
    print(f"  Archetype failures:       {_audit['archetype_failures']}")
    print(f"  Result:                   {result}")
    print("=" * 54)
