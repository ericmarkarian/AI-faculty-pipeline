"""
Compare GPT-4o vs GPT-4o-mini for faculty extraction on a single school.
Usage: python tools/compare_extraction_models.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from providers.generic_faculty_scraper import GenericFacultyScraper

SCHOOL      = "Harvard Medical School"
DISCIPLINE  = "Pediatric Cardiology"

for model in ("gpt-4o", "gpt-4o-mini"):
    print(f"\n{'='*60}")
    print(f"  Model: {model}")
    print(f"{'='*60}")
    scraper = GenericFacultyScraper(extraction_model=model)
    faculty = scraper.get_faculty(SCHOOL, DISCIPLINE)
    print(f"\n  -> Extracted {len(faculty)} faculty")
    for f in faculty[:5]:
        print(f"     {f['full_name']} | {f['title']}")
    if len(faculty) > 5:
        print(f"     ... and {len(faculty) - 5} more")
