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
    discovery_summary,
    flag_shared_profile_urls,
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
    ("Jordan Vega", "jordan vega"),
    ("Jordan Vega, MD", "jordan vega"),
    ("Jordan Vega MD, MPH", "jordan vega"),
    ("  JORDAN   VEGA  ", "jordan vega"),
    ("Dr. Jordan Vega", "jordan vega"),
    ("Riley L. Chen, Ph.D.", "riley l chen"),
    ("José Ramírez", "jose ramirez"),
    ("Mary O'Brien", "mary obrien"),
    ("Jane Smith-Jones", "jane smithjones"),
    ("Vega, Jordan", "vega jordan"),
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
    """'Riley Chen' and 'Riley L. Chen' share a first/last key."""
    assert name_keys("Riley Chen")[1] == name_keys("Riley L. Chen")[1]


@pytest.mark.unit
def test_name_keys_ignore_name_order():
    """Directories that print 'Last, First' still match 'First Last'."""
    assert name_keys("Vega, Jordan")[1] == name_keys("Jordan Vega")[1]


@pytest.mark.unit
def test_name_keys_ignore_trailing_initial_in_inverted_names():
    """'Chen, Riley L' (directory) still matches 'Riley Chen' (web search)."""
    assert name_keys("Chen, Riley L")[1] == name_keys("Riley Chen")[1]


@pytest.mark.unit
def test_name_keys_keep_single_letter_names_when_thats_all_there_is():
    assert name_keys("A B")[1] == "a|b"


@pytest.mark.unit
def test_name_keys_distinguish_different_people():
    assert name_keys("Jordan Vega")[1] != name_keys("Casey Morgan")[1]


@pytest.mark.unit
def test_name_keys_single_token_name():
    full, pair = name_keys("Cher")
    assert full == pair == "cher"


# ── to_record ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_to_record_builds_full_record():
    record = to_record(
        {"full_name": " Jordan Vega ", "title": " Professor ",
         "profile_url": "(https://profiles.stanford.edu/jordan-vega)"},
        _SCHOOL, SOURCE_UNIVERSITY,
    )
    assert record["full_name"] == "Jordan Vega"
    assert record["title"] == "Professor"
    assert record["profile_url"] == "https://profiles.stanford.edu/jordan-vega"
    assert record["school"] == _SCHOOL
    assert record["subspecialty"] is None
    assert record["email"] is None


@pytest.mark.unit
def test_to_record_accepts_name_alias():
    record = to_record({"name": "Jordan Vega"}, _SCHOOL, SOURCE_WEB_SEARCH)
    assert record["full_name"] == "Jordan Vega"


@pytest.mark.unit
@pytest.mark.parametrize("item", [
    None,
    "Jordan Vega",
    ["Jordan Vega"],
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
        {"full_name": "Jordan Vega", "title": "", "profile_url": "", "email": "  "},
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
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Casey Morgan", "title": "Professor"}]),
    ], _SCHOOL)
    assert {r["full_name"] for r in merged} == {"Jordan Vega", "Casey Morgan"}


@pytest.mark.unit
def test_merge_dedupes_same_person_across_sources():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega"}]),
        (SOURCE_HOSPITAL, [{"full_name": "Jordan Vega"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Jordan Vega, MD"}]),
    ], _SCHOOL)
    assert len(merged) == 1


@pytest.mark.unit
def test_merge_dedupes_middle_initial_variant():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Riley L. Chen", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Riley Chen", "title": "Prof of Pediatrics"}]),
    ], _SCHOOL)
    assert len(merged) == 1
    assert merged[0]["full_name"] == "Riley L. Chen"   # directory name wins
    assert merged[0]["title"] == "Professor"


@pytest.mark.unit
def test_merge_dedupes_within_a_single_source():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega"}, {"full_name": "Jordan Vega, MD"}]),
    ], _SCHOOL)
    assert len(merged) == 1


@pytest.mark.unit
def test_university_fields_beat_hospital_and_web_search():
    merged = merge_faculty([
        (SOURCE_WEB_SEARCH, [{"full_name": "Jordan Vega", "title": "Pediatrician",
                              "profile_url": "https://example.org/vega"}]),
        (SOURCE_HOSPITAL, [{"full_name": "Jordan Vega", "title": "Attending",
                            "profile_url": "https://stanfordchildrens.org/vega"}]),
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega", "title": "Professor of Pediatrics",
                              "profile_url": "https://profiles.stanford.edu/jordan-vega"}]),
    ], _SCHOOL)
    assert len(merged) == 1
    assert merged[0]["title"] == "Professor of Pediatrics"
    assert merged[0]["profile_url"] == "https://profiles.stanford.edu/jordan-vega"


@pytest.mark.unit
def test_lower_priority_source_fills_missing_fields_only():
    """Web search never overwrites directory data, but does fill its gaps."""
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega", "title": "Professor",
                              "profile_url": None}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Jordan Vega", "title": "Pediatrician",
                              "profile_url": "https://profiles.stanford.edu/jordan-vega",
                              "email": "vega@stanford.edu",
                              "subspecialty": "Diabetes"}]),
    ], _SCHOOL)
    assert merged[0]["title"] == "Professor"
    assert merged[0]["profile_url"] == "https://profiles.stanford.edu/jordan-vega"
    assert merged[0]["email"] == "vega@stanford.edu"
    assert merged[0]["subspecialty"] == "Diabetes"


@pytest.mark.unit
def test_precedence_is_independent_of_source_order():
    args = [{"full_name": "Jordan Vega", "title": "Professor"}], [{"full_name": "Jordan Vega", "title": "Attending"}]
    forward = merge_faculty([(SOURCE_UNIVERSITY, args[0]), (SOURCE_HOSPITAL, args[1])], _SCHOOL)
    reverse = merge_faculty([(SOURCE_HOSPITAL, args[1]), (SOURCE_UNIVERSITY, args[0])], _SCHOOL)
    assert forward[0]["title"] == reverse[0]["title"] == "Professor"


@pytest.mark.unit
def test_legacy_unknown_source_outranks_web_search():
    merged = merge_faculty([
        (SOURCE_UNKNOWN, [{"full_name": "Jordan Vega", "title": "Professor"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Jordan Vega", "title": "Pediatrician"}]),
    ], _SCHOOL)
    assert merged[0]["title"] == "Professor"


@pytest.mark.unit
def test_discovery_source_records_every_contributor():
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Jordan Vega"}, {"full_name": "Casey Morgan"}]),
    ], _SCHOOL)
    by_name = {r["full_name"]: r for r in merged}
    assert by_name["Jordan Vega"]["discovery_source"] == "university+web_search"
    assert by_name["Casey Morgan"]["discovery_source"] == "web_search"


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
        (SOURCE_UNIVERSITY, [None, "junk", {"full_name": ""}, {"full_name": "Jordan Vega"}]),
    ], _SCHOOL)
    assert [r["full_name"] for r in merged] == ["Jordan Vega"]


@pytest.mark.unit
def test_merge_does_not_mutate_inputs():
    university = [{"full_name": "Jordan Vega", "title": "Professor"}]
    web = [{"full_name": "Jordan Vega", "email": "vega@stanford.edu"}]
    merge_faculty([(SOURCE_UNIVERSITY, university), (SOURCE_WEB_SEARCH, web)], _SCHOOL)
    assert university == [{"full_name": "Jordan Vega", "title": "Professor"}]
    assert web == [{"full_name": "Jordan Vega", "email": "vega@stanford.edu"}]


@pytest.mark.unit
def test_unknown_source_label_is_accepted_at_lowest_priority():
    """An unrecognized label must not crash the merge."""
    merged = merge_faculty([
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega", "title": "Professor"}]),
        ("some_new_source", [{"full_name": "Jordan Vega", "title": "Other"},
                             {"full_name": "New Person"}]),
    ], _SCHOOL)
    by_name = {r["full_name"]: r for r in merged}
    assert by_name["Jordan Vega"]["title"] == "Professor"
    assert "New Person" in by_name


# ── dedupe_by_name ────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_dedupe_keeps_first_occurrence():
    records = [
        {"full_name": "Jordan Vega", "school": _SCHOOL, "title": "Professor"},
        {"full_name": "Jordan Vega, MD", "school": _SCHOOL, "title": "Attending"},
        {"full_name": "Casey Morgan", "school": _SCHOOL},
    ]
    deduped = dedupe_by_name(records)
    assert [r["full_name"] for r in deduped] == ["Jordan Vega", "Casey Morgan"]
    assert deduped[0]["title"] == "Professor"


@pytest.mark.unit
def test_dedupe_is_scoped_per_school():
    """The same person can legitimately appear under two schools (joint appointment)."""
    records = [
        {"full_name": "Jordan Vega", "school": "Stanford"},
        {"full_name": "Jordan Vega", "school": "UCSF"},
    ]
    assert len(dedupe_by_name(records)) == 2


@pytest.mark.unit
def test_dedupe_matches_middle_initial_and_inverted_names():
    records = [
        {"full_name": "Riley L. Chen", "school": _SCHOOL},
        {"full_name": "Chen, Riley", "school": _SCHOOL},
    ]
    assert len(dedupe_by_name(records)) == 1


@pytest.mark.unit
def test_dedupe_drops_unnamed_records():
    records = [{"school": _SCHOOL}, {"full_name": "  ", "school": _SCHOOL},
               {"full_name": "Jordan Vega", "school": _SCHOOL}]
    assert [r["full_name"] for r in dedupe_by_name(records)] == ["Jordan Vega"]


@pytest.mark.unit
def test_dedupe_does_not_mutate_input():
    records = [{"full_name": "Jordan Vega", "school": _SCHOOL}]
    dedupe_by_name(records)
    assert records == [{"full_name": "Jordan Vega", "school": _SCHOOL}]


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
        (SOURCE_UNIVERSITY, [{"full_name": "Jordan Vega", "school": "Wrong School"}]),
        (SOURCE_WEB_SEARCH, [{"full_name": "Casey Morgan"}]),
    ], _SCHOOL)
    assert all(r["school"] == _SCHOOL for r in merged)


# ── degree suffixes that used to defeat dedupe ────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("variant", [
    "Jordan Vega, MD, MHSc",
    "Jordan Vega, MD, MSCE",
    "Jordan Vega, MD, MS, FNLA",
    "Jordan Vega, MBBCh, FRCPC",
])
def test_less_common_degrees_do_not_split_one_person_in_two(variant):
    records = [
        {"full_name": "Jordan Vega", "school": _SCHOOL},
        {"full_name": variant, "school": _SCHOOL},
    ]
    assert len(dedupe_by_name(records)) == 1


# ── flag_shared_profile_urls ──────────────────────────────────────────────────

@pytest.mark.unit
def test_roster_page_used_as_everyones_profile_is_flagged():
    """'…/team#' for two people is the roster, not either person's page."""
    records = [
        {"full_name": "Jordan Vega", "profile_url": "https://h.example/endo/team#"},
        {"full_name": "Casey Morgan", "profile_url": "https://h.example/endo/team#"},
        {"full_name": "Riley Chen", "profile_url": "https://h.example/doctors/riley-chen"},
        {"full_name": "Sam Okafor", "profile_url": None},
    ]
    flagged = flag_shared_profile_urls(records)
    assert [bool(r.get("profile_url_shared")) for r in flagged] == [True, True, False, False]


@pytest.mark.unit
def test_directory_page_as_profile_is_flagged_even_for_one_person():
    records = [{"full_name": "Jordan Vega", "profile_url": "https://u.example/faculty.html#vega"}]
    flagged = flag_shared_profile_urls(records, directory_urls=["https://u.example/faculty.html"])
    assert flagged[0]["profile_url_shared"] is True


@pytest.mark.unit
def test_flag_shared_profile_urls_does_not_mutate_its_input():
    records = [
        {"full_name": "Jordan Vega", "profile_url": "https://h.example/team"},
        {"full_name": "Casey Morgan", "profile_url": "https://h.example/team/"},
    ]
    flag_shared_profile_urls(records)
    assert all("profile_url_shared" not in r for r in records)


# ── discovery_summary ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_discovery_summary_is_quiet_when_every_source_worked():
    records = [{"full_name": "Jordan Vega", "discovery_source": "university"}]
    report = {"sources": [{"type": SOURCE_UNIVERSITY, "url": "https://u.example/faculty",
                           "status": "ok", "found": 1, "detail": ""}]}
    summary = discovery_summary(_SCHOOL, records, report)
    assert summary["issues"] == []
    assert (summary["school"], summary["found"]) == (_SCHOOL, 1)
    assert summary["by_source"] == {"university": 1}


@pytest.mark.unit
def test_discovery_summary_flags_a_school_with_no_faculty():
    assert discovery_summary(_SCHOOL, [])["issues"] == ["No faculty found"]


@pytest.mark.unit
def test_discovery_summary_names_blocked_failed_and_empty_pages():
    records = [{"full_name": "Casey Morgan", "discovery_source": "web_search"}]
    report = {"sources": [
        {"type": SOURCE_HOSPITAL, "url": "https://h.example/team", "status": "blocked", "found": 0, "detail": "HTTP 403"},
        {"type": SOURCE_UNIVERSITY, "url": "https://u.example/overview", "status": "empty", "found": 0, "detail": ""},
        {"type": SOURCE_UNKNOWN, "url": "https://x.example/dir", "status": "error", "found": 0, "detail": "timeout"},
    ]}
    assert discovery_summary(_SCHOOL, records, report)["issues"] == [
        "Blocked: hospital page https://h.example/team (HTTP 403)",
        "No faculty listed on: university page https://u.example/overview",
        "Could not load: directory page https://x.example/dir (timeout)",
    ]


@pytest.mark.unit
def test_discovery_summary_keeps_a_long_browser_error_to_one_short_line():
    """A browser launch failure arrives as a multi-line message with an ASCII box in it."""
    noisy = "BrowserType.launch: Executable doesn't exist at /cache/chromium\n╔════╗\n║ run: playwright install ║\n╚════╝"
    report = {"sources": [
        {"type": SOURCE_HOSPITAL, "url": "https://h.example/team", "status": "error", "found": 0, "detail": noisy},
        {"type": SOURCE_UNIVERSITY, "url": "https://u.example/f", "status": "error", "found": 0, "detail": "x" * 500},
    ]}
    summary = discovery_summary(_SCHOOL, [], report)

    assert summary["issues"][1] == (
        "Could not load: hospital page https://h.example/team "
        "(BrowserType.launch: Executable doesn't exist at /cache/chromium)"
    )
    assert [s["detail"] for s in summary["sources"]] == [
        "BrowserType.launch: Executable doesn't exist at /cache/chromium",
        "x" * 159 + "…",
    ]
    assert report["sources"][0]["detail"] == noisy  # input left untouched


@pytest.mark.unit
def test_discovery_summary_notes_who_was_left_out_and_which_link_was_followed():
    """Neither is a problem with the roster, so they are notes, not issues."""
    records = [{"full_name": "Casey Morgan", "discovery_source": SOURCE_UNIVERSITY}]
    report = {
        "sources": [{
            "type": SOURCE_UNIVERSITY, "url": "https://u.example/overview", "status": "ok", "found": 34, "detail": "",
            "followed": {"url": "https://u.example/overview/faculty", "found": 33, "own_found": 2},
            "left_out": {"trainee": 10, "staff": 1},
        }],
        "web_search_left_out": {"emeritus": 2},
    }
    summary = discovery_summary(_SCHOOL, records, report)

    assert summary["issues"] == []
    assert summary["notes"] == [
        "Read a roster linked from the university page: https://u.example/overview/faculty "
        "(33 people; the saved page lists 2)",
        "Left out 1 listed as non-physician staff on the university page",
        "Left out 10 listed as fellows or trainees on the university page",
        "Left out 2 returned by web search as emeritus or former",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("found,expected,flagged", [
    (20, 34, True),     # under 60% of what the page listed before
    (21, 34, False),
    (1, 3, False),      # too small a division for a shortfall to mean anything
])
def test_discovery_summary_flags_a_page_listing_far_fewer_people_than_expected(found, expected, flagged):
    records = [{"full_name": "Casey Morgan", "discovery_source": SOURCE_UNIVERSITY}]
    report = {"sources": [{"type": SOURCE_UNIVERSITY, "url": "https://u.example/faculty", "status": "ok",
                           "found": found, "detail": "", "expected": expected, "expected_checked": "2026-11-19"}]}
    summary = discovery_summary(_SCHOOL, records, report)

    expected_issues = [f"Fewer people than expected on: university page https://u.example/faculty "
                       f"({found} found; {expected} when checked 2026-11-19)"] if flagged else []
    assert summary["issues"] == expected_issues
    assert bool(summary["sources"][0].get("short")) is flagged
    assert "short" not in report["sources"][0]      # input left untouched
