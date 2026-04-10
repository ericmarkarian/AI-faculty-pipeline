"""
Capture NIH grants fixture for one faculty member (NETWORK-ALLOWED — run once).

Usage:
    python -m tools.capture_fixture_nih \
        --name "David Maahs" --school "Stanford" \
        --out tests/fixtures/nih/david_maahs.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.live import LiveNihProvider


def capture(name: str, school: str, out_path: Path) -> dict:
    """Capture NIH grants fixture. Returns the fixture dict."""
    print(f"  [nih] Fetching NIH grants for {name} ({school})...")
    provider = LiveNihProvider()
    grants = provider.get_grants(name, school)

    fixture = {
        "name": name,
        "school": school,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "grants": grants,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fixture, indent=2))
    print(f"  [nih] {len(grants)} grants -> {out_path}")
    for g in grants[:3]:
        print(f"        [{g['project_num']}] {g['title'][:70]}")
    if len(grants) > 3:
        print(f"        ... and {len(grants) - 3} more")
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture NIH grants fixture (live network call).")
    parser.add_argument("--name", required=True)
    parser.add_argument("--school", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    capture(args.name, args.school, Path(args.out))


if __name__ == "__main__":
    main()
