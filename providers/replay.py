"""
Replay providers — load pre-captured fixtures from disk. Zero network calls.
Default for all tests. Raises FileNotFoundError if fixture is missing (run capture tools first).
"""
import json
from pathlib import Path

from providers.base import (
    NihProvider, RolesProvider, HIndexProvider, ScraperProvider,
    Grant, RoleEvidence, HIndexEvidence, FacultyRecord,
)

_FIXTURES_ROOT = Path(__file__).parent.parent / "tests" / "fixtures"


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_").replace(".", "").replace(",", "")


def _load_fixture(path: Path, capture_cmd: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Fixture missing: {path}\nRun: {capture_cmd}")
    return json.loads(path.read_text())


class ReplayNihProvider(NihProvider):
    def __init__(self, fixtures_dir: Path | None = None):
        self._dir = fixtures_dir or (_FIXTURES_ROOT / "nih")

    def get_grants(self, name: str, school: str) -> list[Grant]:
        path = self._dir / f"{_slug(name)}.json"
        data = _load_fixture(
            path,
            f'python -m tools.capture_fixture_nih --name "{name}" --school "{school}" --out {path}',
        )
        return data["grants"]


class ReplayRolesProvider(RolesProvider):
    def __init__(self, fixtures_dir: Path | None = None):
        self._dir = fixtures_dir or (_FIXTURES_ROOT / "roles")

    def get_roles(self, name: str, school: str, profile_url: str = "") -> list[RoleEvidence]:
        path = self._dir / f"{_slug(name)}.json"
        data = _load_fixture(
            path,
            f'python -m tools.capture_fixture_roles --name "{name}" --school "{school}" --out {path}',
        )
        return data["roles"]


class ReplayHIndexProvider(HIndexProvider):
    def __init__(self, fixtures_dir: Path | None = None):
        self._dir = fixtures_dir or (_FIXTURES_ROOT / "hindex")

    def get_hindex(self, name: str, school: str) -> HIndexEvidence:
        path = self._dir / f"{_slug(name)}.json"
        data = _load_fixture(
            path,
            f'python -m tools.capture_fixture_hindex --name "{name}" --school "{school}" --out {path}',
        )
        return data


def _scraper_slug(school: str, discipline: str) -> str:
    return f"{_slug(school)}_{_slug(discipline)}"


class ReplayScraperProvider(ScraperProvider):
    def __init__(self, fixtures_dir: Path | None = None):
        self._dir = fixtures_dir or (_FIXTURES_ROOT / "scraper")

    def get_faculty(self, school: str, discipline: str) -> list[FacultyRecord]:
        slug = _scraper_slug(school, discipline)
        path = self._dir / f"{slug}.json"
        data = _load_fixture(
            path,
            f'python -m tools.capture_fixture_scraper --school "{school}" --discipline "{discipline}" --out {path}',
        )
        return data["faculty"]
