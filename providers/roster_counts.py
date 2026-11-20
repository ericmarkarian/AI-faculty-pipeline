"""
How many people each saved directory page listed the last time it read well.

A directory URL is saved once and then trusted. When the page later moves, gets
replaced by an overview, or the site starts hiding part of the list, the scraper
still "succeeds" — with a fraction of the people. Remembering the count turns that
silent shrinkage into something the run can report.

The file (data/roster_counts.json) is shaped
    {discipline: {school: {url: {"count": 34, "checked": "2026-11-19"}}}}
and is committed with a few known counts; runs add to it as they go. On a host with
an ephemeral disk the additions last until the next deploy, which is still enough to
compare one run against the last.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path

# A page must list at least this many people before its count is worth remembering
# on its own, and before a later shortfall against it is reported.
MIN_COUNT = 5
# A page listing fewer than this share of its remembered count is read as shrunk.
SHORTFALL = 0.6


def is_short(found: int, expected: int | None) -> bool:
    """True when a page lists so few people that it probably is not the full roster."""
    if expected:
        return found < SHORTFALL * expected
    return found < MIN_COUNT


class RosterCounts:
    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # A damaged counts file must never stop a search — carry on without history.
            print(f"    [RosterCounts] Ignoring unreadable {self._path}: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, discipline: str, school: str, url: str) -> dict | None:
        """{"count": int, "checked": "YYYY-MM-DD"} for a page, or None when unknown."""
        entry = self._data.get(discipline, {}).get(school, {}).get(url)
        if isinstance(entry, dict) and isinstance(entry.get("count"), int) and entry["count"] > 0:
            return entry
        return None

    def note(self, discipline: str, school: str, url: str, found: int,
             confirmed: bool = False, today: str | None = None) -> bool:
        """Remember a good read of a page. Returns True when the file changed.

        The count only ever goes up: a low read is what this file exists to catch,
        so it must not overwrite the number it is compared against. Counts below
        MIN_COUNT are kept only when `confirmed` — the caller looked for a fuller
        roster elsewhere and found none, so the division really is that small.
        """
        if found < 1 or (found < MIN_COUNT and not confirmed):
            return False
        known = self.get(discipline, school, url)
        if known and known["count"] >= found:
            return False
        self._data.setdefault(discipline, {}).setdefault(school, {})[url] = {
            "count": found,
            "checked": today or datetime.date.today().isoformat(),
        }
        return self._save()

    def _save(self) -> bool:
        """Write atomically — a run killed mid-write must not leave half a file."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".roster_counts.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._data, f, indent=2, sort_keys=True)
                    f.write("\n")
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            print(f"    [RosterCounts] Could not save {self._path}: {exc}")
            return False
        return True
