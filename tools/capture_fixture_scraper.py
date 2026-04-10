"""
Capture scraper fixture for a school/discipline (NETWORK-ALLOWED — run once).

Usage:
    python -m tools.capture_fixture_scraper \
        --school "Stanford" --discipline "Pediatric Endocrinology" \
        --out tests/fixtures/scraper/stanford_pediatric_endocrinology.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers.playwright_scraper import PlaywrightScraperProvider, _BROWSE_URL_MAP


def capture(school: str, discipline: str, out_path: Path) -> dict:
    provider = PlaywrightScraperProvider(request_delay=0.0)
    faculty = provider.get_faculty(school, discipline)

    source_url = _BROWSE_URL_MAP.get((school, discipline), "unknown")
    fixture = {
        "school": school,
        "discipline": discipline,
        "source_url": source_url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "total_on_page": None,  # unknown without re-parsing; live provider returns filtered count
        "faculty": faculty,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fixture, indent=2))
    print(f"  [scraper] {len(faculty)} faculty -> {out_path}")
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture scraper fixture (live network call).")
    parser.add_argument("--school", required=True)
    parser.add_argument("--discipline", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    capture(args.school, args.discipline, Path(args.out))


if __name__ == "__main__":
    main()
