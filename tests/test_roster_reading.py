"""
Tests for reading a roster page by its sections (providers.roster_reading).

A team page lists faculty alongside fellows, nurses and emeritus faculty. These cover
the rules that leave the non-faculty out, the rules that protect real faculty from
being left out by mistake, and the search for a "meet our team" link on a page that
is not the roster. All names are made up.
"""
import pytest

from providers.roster_reading import (
    EMERITUS, FACULTY, STAFF, TRAINEE,
    SectionMap, classify_heading, count_by_category, screen_roster, split_chunks,
    team_links, title_category,
)


# ── headings ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("heading,expected", [
    ("Fellows", TRAINEE),
    ("Clinical Fellows", TRAINEE),
    ("Current Residents", TRAINEE),
    ("Endocrine Nurses", STAFF),
    ("Clinical and Professional Staff", STAFF),
    ("Nurse Practitioners and Physician Assistants", STAFF),
    ("Social Work", STAFF),
    ("Professor Emeritus", EMERITUS),
    ("Emeritus Faculty", EMERITUS),
    ("Former Fellows", EMERITUS),
    ("Faculty", FACULTY),
    ("Physicians and Researchers", FACULTY),
    ("Division Chief", FACULTY),
    ("Meet Our Endocrinology Specialists", FACULTY),
    ("Medical Staff", FACULTY),
    # A heading that names faculty together with another group protects everyone under it.
    ("Faculty & Staff", FACULTY),
    ("Leadership and Fellows", FACULTY),
    # Says nothing about who is listed.
    ("Our Team", None),
    ("Psychologists", None),
    ("Contact Us", None),
    ("Fellowship Program", None),
    # A person, not a section — even when the surname is a section word.
    ("Marilyn Fellows, MD", None),
    ("Jordan Vega, MD, PhD", None),
    ("x" * 120, None),
])
def test_classify_heading(heading, expected):
    assert classify_heading(heading) == expected


# ── titles ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("title", [
    "Clinical Fellow",
    "Pediatric Endocrinology Fellow",
    "Fellow, Pediatric Endocrinology",
    "Postdoctoral Research Fellow",
    "Chief Resident",
    "Postdoc",
])
def test_training_posts_are_trainees(title):
    assert title_category(title) == TRAINEE


@pytest.mark.unit
@pytest.mark.parametrize("title", [
    "Program Director, Pediatric Endocrinology Fellowship",
    "Associate Program Director, Endocrinology Fellowship",
    "Fellow of the American Academy of Pediatrics",
    "Fellow, American College of Endocrinology",
    "Senior Fellow, Institute for Health Policy",
    "Instructor and Clinical Fellow",          # holds a faculty rank
    "Vice President for Research",             # "president" is not "resident"
    "Professor of Internal Medicine",          # "internal" is not "intern"
    "Associate Professor of Pediatrics",
    "Attending Physician",
    None,
])
def test_faculty_titles_are_never_read_as_trainees_or_staff(title):
    assert title_category(title) is None


@pytest.mark.unit
@pytest.mark.parametrize("title,name,expected", [
    ("Pediatric Nurse Practitioner", "Avery Quinn", STAFF),
    ("Clinical Instructor in Nursing; Pediatric Nurse Practitioner", "Avery Quinn", STAFF),
    ("Clinical Research Coordinator", "Avery Quinn", STAFF),
    ("Registered Dietitian", "Avery Quinn", STAFF),
    (None, "Avery Quinn, CDE, CPNP, APRN", STAFF),      # no title: the credentials say it
    (None, "Avery Quinn, PhD, RN", None),               # a doctorate keeps them
    (None, "Quinn, Pa Chia", None),                     # "Pa" is a given name, not "PA"
    ("Professor of Nursing; Nurse Scientist", "Avery Quinn", None),
    ("Professor Emeritus of Pediatrics", "Avery Quinn", EMERITUS),
    ("Emerita Professor", "Avery Quinn", EMERITUS),
])
def test_staff_and_emeritus_by_title_or_credentials(title, name, expected):
    assert title_category(title, name) == expected


# ── sections ──────────────────────────────────────────────────────────────────

_TEAM_PAGE = """# Meet the team

## Division Chief

### Jordan Vega, MD

## Physicians and Researchers

### Casey Morgan, MD

### Riley Chen, PhD

## Fellows

### Sam Okafor, MD

### Avery Quinn, DO

### Dana Brooks, MD

## Clinical and Professional Staff

### Devon Hale

### Morgan Ellis, MD

## Professor Emeritus

### Robin Castell, MD

## Contact us

Questions? Write to Dr. Sam Okafor's mentor, Jordan Vega.
"""


def _people(*names, **titles):
    return [{"full_name": n, "title": titles.get(n.split(",")[0].replace(" ", "_"))} for n in names]


@pytest.mark.unit
def test_people_are_left_out_by_the_section_they_are_listed_under():
    items = _people("Jordan Vega, MD", "Casey Morgan, MD", "Riley Chen, PhD", "Sam Okafor, MD",
                    "Avery Quinn, DO", "Devon Hale", "Morgan Ellis, MD", "Robin Castell, MD")
    kept, left_out = screen_roster(items, _TEAM_PAGE)

    assert [p["full_name"] for p in kept] == [
        "Jordan Vega, MD", "Casey Morgan, MD", "Riley Chen, PhD",
        "Morgan Ellis, MD",   # a physician filed under "Staff" stays: the MD outweighs the heading
    ]
    assert {e["full_name"]: e["category"] for e in left_out} == {
        "Sam Okafor, MD": TRAINEE, "Avery Quinn, DO": TRAINEE,
        "Devon Hale": STAFF, "Robin Castell, MD": EMERITUS,
    }
    assert left_out[0]["why"] == "listed under 'Fellows'"
    assert count_by_category(left_out) == {TRAINEE: 2, STAFF: 1, EMERITUS: 1}


@pytest.mark.unit
def test_a_ranked_person_under_a_trainee_heading_is_kept():
    """Fellowship pages list the program director next to the fellows."""
    items = [{"full_name": "Dana Brooks, MD", "title": "Fellowship Director; Associate Professor"},
             {"full_name": "Sam Okafor, MD", "title": None}]
    kept, left_out = screen_roster(items, _TEAM_PAGE)
    assert [p["full_name"] for p in kept] == ["Dana Brooks, MD"]
    assert [e["full_name"] for e in left_out] == ["Sam Okafor, MD"]


@pytest.mark.unit
def test_a_section_ends_at_the_next_heading_of_its_own_level():
    sections = SectionMap(_TEAM_PAGE)
    assert sections.for_person("Casey Morgan") == (FACULTY, "Physicians and Researchers")
    assert sections.for_person("Avery Quinn") == (TRAINEE, "Fellows")
    # "Contact us" closes the emeritus section, so text after it belongs to no group.
    assert sections.at(_TEAM_PAGE.index("Questions?")) is None


@pytest.mark.unit
def test_a_persons_own_heading_decides_not_a_passing_mention():
    """Jordan Vega is named again at the foot of the page; the card under 'Division Chief' wins."""
    assert SectionMap(_TEAM_PAGE).for_person("Jordan Vega, MD") == (FACULTY, "Division Chief")


@pytest.mark.unit
def test_names_written_last_first_are_still_found():
    page = "## Faculty\n\nVega, Jordan — Professor\n\n## Fellows\n\nOkafor, Sam\n"
    kept, left_out = screen_roster(_people("Jordan Vega", "Sam Okafor"), page)
    assert [p["full_name"] for p in kept] == ["Jordan Vega"]
    assert [e["full_name"] for e in left_out] == ["Sam Okafor"]


@pytest.mark.unit
def test_the_models_section_is_used_only_when_it_is_a_real_heading_on_the_page():
    page = "## Faculty\n\nDr. J. Vega\n\n## Fellows\n\nDr. S. Okafor\n"   # names abbreviated on the page
    reported = [{"full_name": "Sam Okafor", "title": None, "section": "Fellows"},
                {"full_name": "Riley Chen", "title": None, "section": "Visiting Trainees"}]  # no such heading
    kept, left_out = screen_roster(reported, page)
    assert [p["full_name"] for p in kept] == ["Riley Chen"]
    assert [e["full_name"] for e in left_out] == ["Sam Okafor"]


@pytest.mark.unit
def test_without_a_page_only_titles_are_judged():
    items = [{"full_name": "Sam Okafor", "title": "Clinical Fellow", "section": "Fellows"},
             {"full_name": "Avery Quinn", "title": None, "section": "Fellows"},
             {"full_name": "Jordan Vega", "title": "Professor"}]
    kept, left_out = screen_roster(items)
    assert [p["full_name"] for p in kept] == ["Avery Quinn", "Jordan Vega"]
    assert [e["why"] for e in left_out] == ["title: Clinical Fellow"]


@pytest.mark.unit
def test_screening_passes_junk_through_and_does_not_mutate_input():
    items = [{"full_name": "Jordan Vega", "title": "Professor"}, "not a record", None]
    snapshot = [dict(items[0]), "not a record", None]
    kept, left_out = screen_roster(items, _TEAM_PAGE)
    assert kept == snapshot and left_out == [] and items == snapshot


# ── chunking ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_text_without_headings_is_cut_exactly_as_before():
    text = "x" * 25
    assert split_chunks(text, 10, 2) == [text[i:i + 10] for i in range(0, 25, 8)]
    assert split_chunks("", 10, 2) == []


@pytest.mark.unit
def test_a_chunk_that_starts_inside_a_section_is_told_which_one():
    faculty = "".join(f"### Person A{i}\n" for i in range(40))
    fellows = "".join(f"### Person B{i}\n" for i in range(40))
    text = "## Faculty\n" + faculty + "## Fellows\n" + fellows
    chunks = split_chunks(text, 500, 50)
    note = "(This part of the page continues under these headings:)\n"

    assert len(chunks) == 3
    assert not chunks[0].startswith(note)                    # the first chunk needs no reminder
    assert chunks[1].startswith(note + "## Faculty\n\n")     # starts among the faculty
    assert chunks[2].startswith(note + "## Fellows\n\n")     # starts among the fellows
    assert chunks[2].endswith(text[900:1400])                # the page text itself is unchanged


# ── finding the roster from another page ──────────────────────────────────────

_OVERVIEW = """
<html><body>
<nav><a href="/find-a-doctor">Find a Doctor</a> <a href="/faculty">Faculty</a></nav>
<main>
  <h1>Division of Pediatric Endocrinology</h1>
  <a href="/departments/pediatrics/endocrinology">Overview</a>
  <a href="/departments/pediatrics/endocrinology/faculty-and-staff/faculty">Faculty</a>
  <a href="/departments/pediatrics/endocrinology/research">Research</a>
  <a href="/departments/pediatrics/endocrinology/faculty-development">Faculty Development</a>
  <a href="/departments/pediatrics/endocrinology/team/vega-jordan">Jordan Vega, MD</a>
  <a href="/departments/pediatrics/cardiology/faculty">Faculty</a>
  <a href="https://elsewhere.example/endocrinology/team">Our team</a>
  <a href="/departments/pediatrics/endocrinology/handbook.pdf">Faculty</a>
  <a href="mailto:endo@med.example.edu">People</a>
</main>
</body></html>
"""
_OVERVIEW_URL = "https://med.example.edu/departments/pediatrics/endocrinology"


@pytest.mark.unit
def test_team_links_picks_this_divisions_roster_and_nothing_else():
    assert team_links(_OVERVIEW, _OVERVIEW_URL, "Pediatric Endocrinology") == [
        "https://med.example.edu/departments/pediatrics/endocrinology/faculty-and-staff/faculty",
    ]


@pytest.mark.unit
def test_team_links_accepts_a_roster_named_only_by_its_address_or_only_by_its_text():
    html = """<a href="/services/endocrinology/meet-our-team">Meet Your Endocrinology and Diabetes Team</a>
              <a href="/services/endocrinology/who-we-are">Meet our team</a>
              <a href="https://research.hospital.example/endocrinology-research/people">People</a>"""
    links = team_links(html, "https://www.hospital.example/services/endocrinology", "Pediatric Endocrinology", limit=5)
    assert set(links) == {
        "https://www.hospital.example/services/endocrinology/meet-our-team",
        "https://www.hospital.example/services/endocrinology/who-we-are",
        "https://research.hospital.example/endocrinology-research/people",   # same site, other subdomain
    }


@pytest.mark.unit
def test_team_links_returns_the_best_few_and_handles_a_page_with_none():
    html = "".join(f'<a href="/endocrinology/sub{i}/team">Our team</a>' for i in range(5))
    assert len(team_links(html, "https://x.example/endocrinology", "Endocrinology")) == 2
    assert team_links("<p>No links here</p>", "https://x.example/endocrinology", "Endocrinology") == []
    assert team_links("", "https://x.example/endocrinology", "Endocrinology") == []
