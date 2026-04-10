"""Shared fixture loaders for golden set tests."""
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent
_FIXTURES_ROLES = _ROOT / "fixtures" / "roles"
_FIXTURES_NIH = _ROOT / "fixtures" / "nih"
_FIXTURES_HINDEX = _ROOT / "fixtures" / "hindex"


def slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace(",", "")


def load_golden_set() -> list[dict]:
    with open(_ROOT / "golden_set.yaml") as f:
        return yaml.safe_load(f)["faculty"]


def load_roles_fixture(person: dict) -> dict:
    """Load full roles fixture dict (includes possible_miss, miss_detection, roles)."""
    path = _FIXTURES_ROLES / f"{slug(person['name'])}.json"
    if not path.exists():
        pytest.skip(
            f"Roles fixture missing for {person['name']}. "
            f"Run: python -m tools.capture_fixture_roles "
            f"--name \"{person['name']}\" --school \"{person['school']}\" --out {path}"
        )
    return json.loads(path.read_text())


def load_roles(person: dict) -> list[dict]:
    """Load just the roles list from the fixture."""
    return load_roles_fixture(person)["roles"]


def load_grants(person: dict) -> list[dict]:
    path = _FIXTURES_NIH / f"{slug(person['name'])}.json"
    if not path.exists():
        pytest.skip(
            f"NIH fixture missing for {person['name']}. "
            f"Run: python -m tools.capture_fixture_nih "
            f"--name \"{person['name']}\" --school \"{person['school']}\" --out {path}"
        )
    return json.loads(path.read_text())["grants"]


def load_hindex(person: dict) -> dict:
    """Load h-index fixture dict."""
    path = _FIXTURES_HINDEX / f"{slug(person['name'])}.json"
    if not path.exists():
        pytest.skip(
            f"H-index fixture missing for {person['name']}. "
            f"Run: python -m tools.capture_fixture_hindex "
            f"--name \"{person['name']}\" --school \"{person['school']}\" --out {path}"
        )
    return json.loads(path.read_text())
