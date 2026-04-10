"""
Capture h-index fixture for one faculty member via Scopus (NETWORK-ALLOWED — run once).

Usage:
    python -m tools.capture_fixture_hindex \
        --name "David Maahs" --school "Stanford" \
        --out tests/fixtures/hindex/david_maahs.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.live import LiveHIndexProvider


def capture(name: str, school: str, out_path: Path, scopus_api_key: str | None = None) -> dict:
    """Capture h-index fixture via Scopus. Returns the fixture dict."""
    print(f"  [hindex] Fetching h-index for {name} ({school})...")
    provider = LiveHIndexProvider(scopus_api_key or os.environ.get("SCOPUS_API_KEY"))
    hindex_data = provider.get_hindex(name, school)

    fixture = {
        "name": name,
        "school": school,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **hindex_data,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fixture, indent=2))

    h = hindex_data.get("h_index")
    print(f"  [hindex] h-index={h} -> {out_path}")
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture h-index fixture via Scopus.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--school", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    capture(args.name, args.school, Path(args.out))


if __name__ == "__main__":
    main()
