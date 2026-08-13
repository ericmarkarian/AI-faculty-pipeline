"""
Tests for providers/faculty_merge.py — the pure merge layer that unions faculty
found by the Playwright directory scrape and the GPT-4o web search.

These tests are offline (no network, no browser): they exercise name
normalization, cross-source deduplication, and field precedence only.
"""
import pytest

from providers.faculty_merge import (
    SOURCE_HOSPITAL,
    SOURCE_UNIVERSITY,
    SOURCE_UNKNOWN,
    SOURCE_WEB_SEARCH,
    dedupe_by_name,
    merge_faculty,
    name_keys,
    normalize_name,
    source_breakdown,
    to_record,
)

_SCHOOL = "Stanford University School of Medicine"


# ── normalize_name ────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("Tandy Aye", "tandy aye"),
    ("Tandy Aye, MD", "tandy aye"),
    ("Tandy Aye MD, MPH", "tandy aye"),
    ("  TANDY   AYE  ", "tandy aye"),
    ("Dr. Tandy Aye", "tandy aye"),
    ("Anna L. Gloyn, Ph.D.", "anna l gloyn"),
    ("José Ramírez", "jose ramirez"),
    ("Mary O'Brien", "mary obrien"),
    ("Jane Smith-Jones", "jane smithjones"),
    ("Aye, Tandy", "aye tandy"),
    ("", ""),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.unit
def test_normalize_name_handles_none():
    assert normalize_name(None) == ""


# ── name_keys ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_name_keys_ignore_middle_initial():
    """'Anna Gloyn' and 'Anna L. Gloyn' share a first/last key."""
    assert name_keys("Anna Gloyn")[1] == name_keys("Anna L. Gloyn")[1]


@pytest.mark.unit
def test_name_keys_ignore_name_order():
    """Directories that print 'Last, First' still match 'First Last'."""
    assert name_keys("Aye, Tandy")[1] == name_keys("Tandy Aye")[1]


@pytest.mark.unit
def test_name_keys_ignore_trailing_initial_in_inverted_names():
    """'Gloyn, Anna L' (directory) still matches 'Anna Gloyn' (web search)."""
    assert name_keys("Gloyn, Anna L")[1] == name_keys("Anna Gloyn")[1]


@pytest.mark.unit
def test_name_keys_keep_single_letter_names_when_thats_all_there_is():
    assert name_keys("A B")[1] == "a|b"


@pytest.mark.unit
def test_name_keys_distinguish_different_people():
    assert name_keys("Tandy Aye")[1] != name_keys("David Maahs")[1]


@pytest.mark.unit
def test_name_keys_single_token_name():
    full, pair = name_keys("Cher")
    assert full == pair == "cher"


# ── to_record ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_to_record_builds_full_record():
    record = to_record(
        {"full_name": " Tandy Aye ", "title": " Professor ",
         "profile_url": "(https://profiles.stanford.edu/tandy-aye)"},
        _SCHOOL, SOURCE_UNIVERSITY,
    )
    assert record["full_name"] == "Tandy Aye"
    assert record["title"] == "Professor"
    assert record["profile_url"] == "https://profiles.stanford.edu/tandy-aye"
    assert record["school"] == _SCHOOL
    assert record["subspecialty"] is None
    assert record["email"] is None


@pytest.mark.unit
def test_to_record_accepts_name_alias():
    record = to_record({"name": "Tandy Aye"}, _SCHOOL, SOURCE_WEB_SEARCH)
    assert record["full_name"] == "Tandy Aye"


@pytest.mark.unit
@pytest.mark.parametrize("item", [
    None,
    "Tandy Aye",
    ["Tandy Aye"],
    {},
    {"full_name": "   "},
    {"full_name": "-"},
    {"full_name": "Jane Doe", "title": "Professor of Pediatrics, by courtesy"},
    {"full_name": "Jane Doe", "title": "Courtesy Appointment, Pediatrics"},
])
def test_to_record_rejects_invalid_items(item):
    assert to_record(item, _SCHOOL, SOURCE_WEB_SEARCH) is None


@pytest.mark.unit
def test_to_record_blanks_become_none():
    record = to_record(
        {"full_name": "Tandy Aye", "title": "", "profile_url": "", "email": "  "},
        _SCHOOL, SOURCE_UNIVERSITY,
    )
    assert record["title"] is None
    assert record["profile_url"] is None
    assert record["email"] is None


# ── merge_faculty ─────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_merge_empty_sources():
    assert merge_faculty([], _SCHOOL) == []
    assert merge_faculty([(SOURCE_UNIVERSITY, [])], _SCHOOL) == []


@pytest.mark.unit
def test_merge_unions_faculty_across_sources():
    """The whole point: web search adds people the directory scrape missed."""
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "David Maahs", "title": "Professor"}]),
    ], _SCHOOL)
    assert {r["full_name"] for r in merged} == {"Tandy Aye", "David Maahs"}


@pytest.mark.unit
def test_merge_dedupes_same_person_across_sources():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye"}]),
        (SOURCE_HOSPITAL, [{"full_name": "Tandy Aye"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Tandy Aye, MD"}]),
    ], _SCHOOL)
    assert len(merged) == 1


@pytest.mark.unit
def test_merge_dedupes_middle_initial_variant():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Anna L. Gloyn", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Anna Gloyn", "title": "Prof of Pediatrics"}]),
    ], _SCHOOL)
    assert len(merged) == 1
    assert merged[0]["full_name"] == "Anna L. Gloyn"   # directory name wins
    assert merged[0]["title"] == "Professor"


@pytest.mark.unit
def test_merge_dedupes_within_a_single_source():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye"}, {"full_name": "Tandy Aye, MD"}]),
    ], _SCHOOL)
    assert len(merged) == 1


@pytest.mark.unit
def test_university_fields_beat_hospital_and_web_search():
    merged = merge_faculty([
        (SOURCE_WEB_SEARCH, [{"full_name": "Tandy Aye", "title": "Pediatrician",
                              "profile_url": "https://example.org/aye"}]),
        (SOURCE_HOSPITAL, [{"full_name": "Tandy Aye", "title": "Attending",
                            "profile_url": "https://stanfordchildrens.org/aye"}]),
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye", "title": "Professor of Pediatrics",
                              "profile_url": "https://profiles.stanford.edu/tandy-aye"}]),
    ], _SCHOOL)
    assert len(merged) == 1
    assert merged[0]["title"] == "Professor of Pediatrics"
    assert merged[0]["profile_url"] == "https://profiles.stanford.edu/tandy-aye"


@pytest.mark.unit
def test_lower_priority_source_fills_missing_fields_only():
    """Web search never overwrites directory data, but does fill its gaps."""
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye", "title": "Professor",
                              "profile_url": None}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Tandy Aye", "title": "Pediatrician",
                              "profile_url": "https://profiles.stanford.edu/tandy-aye",
                              "email": "aye@stanford.edu",
                              "subspecialty": "Diabetes"}]),
    ], _SCHOOL)
    assert merged[0]["title"] == "Professor"
    assert merged[0]["profile_url"] == "https://profiles.stanford.edu/tandy-aye"
    assert merged[0]["email"] == "aye@stanford.edu"
    assert merged[0]["subspecialty"] == "Diabetes"


@pytest.mark.unit
def test_precedence_is_independent_of_source_order():
    args = [{"full_name": "Tandy Aye", "title": "Professor"}], [{"full_name": "Tandy Aye", "title": "Attending"}]
    forward = merge_faculty([(SOURCE_UNIVERSITY, args[0]), (SOURCE_HOSPITAL, args[1])], _SCHOOL)
    reverse = merge_faculty([(SOURCE_HOSPITAL, args[1]), (SOURCE_UNIVERSITY, args[0])], _SCHOOL)
    assert forward[0]["title"] == reverse[0]["title"] == "Professor"


@pytest.mark.unit
def test_legacy_unknown_source_outranks_web_search():
    merged = merge_faculty([
        (SOURCE_UNKNOWN, [{"full_name": "Tandy Aye", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Tandy Aye", "title": "Pediatrician"}]),
    ], _SCHOOL)
    assert merged[0]["title"] == "Professor"


@pytest.mark.unit
def test_discovery_source_records_every_contributor():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Tandy Aye"}, {"full_name": "David Maahs"}]),
    ], _SCHOOL)
    by_name = {r["full_name"]: r for r in merged}
    assert by_name["Tandy Aye"]["discovery_source"] == "university+web_search"
    assert by_name["David Maahs"]["discovery_source"] == "web_search"


@pytest.mark.unit
def test_merge_preserves_first_seen_order():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "B Person"}, {"full_name": "A Person"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "C Person"}]),
    ], _SCHOOL)
    assert [r["full_name"] for r in merged] == ["B Person", "A Person", "C Person"]


@pytest.mark.unit
def test_merge_skips_invalid_items_without_failing():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [None, "junk", {"full_name": ""}, {"full_name": "Tandy Aye"}]),
    ], _SCHOOL)
    assert [r["full_name"] for r in merged] == ["Tandy Aye"]


@pytest.mark.unit
def test_merge_does_not_mutate_inputs():
    university = [{"full_name": "Tandy Aye", "title": "Professor"}]
    web = [{"full_name": "Tandy Aye", "email": "aye@stanford.edu"}]
    merge_faculty([(SOURCE_UNIVERSITY, university), (SOURCE_WEB_SEARCH, web)], _SCHOOL)
    assert university == [{"full_name": "Tandy Aye", "title": "Professor"}]
    assert web == [{"full_name": "Tandy Aye", "email": "aye@stanford.edu"}]


@pytest.mark.unit
def test_unknown_source_label_is_accepted_at_lowest_priority():
    """An unrecognized label must not crash the merge."""
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye", "title": "Professor"}]),
        ("some_new_source", [{"full_name": "Tandy Aye", "title": "Other"},
                             {"full_name": "New Person"}]),
    ], _SCHOOL)
    by_name = {r["full_name"]: r for r in merged}
    assert by_name["Tandy Aye"]["title"] == "Professor"
    assert "New Person" in by_name


# ── dedupe_by_name ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_dedupe_keeps_first_occurrence():
    records = [
        {"full_name": "Tandy Aye", "school": _SCHOOL, "title": "Professor"},
        {"full_name": "Tandy Aye, MD", "school": _SCHOOL, "title": "Attending"},
        {"full_name": "David Maahs", "school": _SCHOOL},
    ]
    deduped = dedupe_by_name(records)
    assert [r["full_name"] for r in deduped] == ["Tandy Aye", "David Maahs"]
    assert deduped[0]["title"] == "Professor"


@pytest.mark.unit
def test_dedupe_is_scoped_per_school():
    """The same person can legitimately appear under two schools (joint appointment)."""
    records = [
        {"full_name": "Tandy Aye", "school": "Stanford"},
        {"full_name": "Tandy Aye", "school": "UCSF"},
    ]
    assert len(dedupe_by_name(records)) == 2


@pytest.mark.unit
def test_dedupe_matches_middle_initial_and_inverted_names():
    records = [
        {"full_name": "Anna L. Gloyn", "school": _SCHOOL},
        {"full_name": "Gloyn, Anna", "school": _SCHOOL},
    ]
    assert len(dedupe_by_name(records)) == 1


@pytest.mark.unit
def test_dedupe_drops_unnamed_records():
    records = [{"school": _SCHOOL}, {"full_name": "  ", "school": _SCHOOL},
               {"full_name": "Tandy Aye", "school": _SCHOOL}]
    assert [r["full_name"] for r in dedupe_by_name(records)] == ["Tandy Aye"]


@pytest.mark.unit
def test_dedupe_does_not_mutate_input():
    records = [{"full_name": "Tandy Aye", "school": _SCHOOL}]
    dedupe_by_name(records)
    assert records == [{"full_name": "Tandy Aye", "school": _SCHOOL}]


# ── source_breakdown ──────────────────────────────────────────────────────────

@pytest.mark.unit
def test_source_breakdown_counts_records_per_source():
    records = [
        {"full_name": "A", "discovery_source": "university"},
        {"full_name": "B", "discovery_source": "university+web_search"},
        {"full_name": "C", "discovery_source": "web_search"},
        {"full_name": "D"},
    ]
    assert source_breakdown(records) == {
        "university": 1, "university+web_search": 1, "web_search": 1, "unknown": 1,
    }


@pytest.mark.unit
def test_source_breakdown_of_empty_list():
    assert source_breakdown([]) == {}


@pytest.mark.unit
def test_every_record_carries_the_school():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Tandy Aye", "school": "Wrong School"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "David Maahs"}]),
    ], _SCHOOL)
    assert all(r["school"] == _SCHOOL for r in merged)
