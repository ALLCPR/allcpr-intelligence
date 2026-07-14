"""
Tests for performance-based, eligibility-aware instructor sourcing.

The correctness core is credential eligibility: AHA and Red Cross are separate
credentialing systems, so "who can teach what" and "who can be bridged" must
respect that separation. These tests pin that logic plus the performance
scoring, winning-profile extraction, and the three sourcing lanes.
"""
from __future__ import annotations

from app.ops import instructor_performance as perf
from app.ops import instructor_sourcing as src
from app.ops.models import AHA_BLS, ARC_BLS, ARC_CPR_FA_AED, scrub_sensitive
from scripts import import_enrollware_ops_data as conv


# --------------------------------------------------------------------------
# Converter: discipline mapping + junk filtering + aggregation
# --------------------------------------------------------------------------
def test_discipline_to_course_mapping():
    # Long Red Cross course names map to ARC; the bare "BLS" discipline and
    # AHA-branded courses map to the AHA BLS Provider line.
    assert conv.discipline_to_course("Red Cross Basic Life Support-BL R.25") \
        == ARC_BLS
    assert conv.discipline_to_course(
        "Red Cross Adult/Pediatric First Aid/CPR/AED r.25") == ARC_CPR_FA_AED
    assert conv.discipline_to_course("ARC BLS") == ARC_BLS
    assert conv.discipline_to_course("ARC CPR") == ARC_CPR_FA_AED
    assert conv.discipline_to_course("AHA© BLS Provider Course") == AHA_BLS
    assert conv.discipline_to_course("BLS") == AHA_BLS
    assert conv.discipline_to_course("") is None


def test_is_real_instructor_filters_status_markers():
    assert conv.is_real_instructor("Britni Wakefield") is True
    assert conv.is_real_instructor("Cancel (weather issue)") is False
    assert conv.is_real_instructor("Tentative (pending schedule)") is False
    assert conv.is_real_instructor("") is False
    assert conv.is_real_instructor("nan") is False


def test_aggregate_instructor_performance_counts_and_join():
    student_rows = [
        {"Instructor": "Jane Doe", "Class ID": "1", "Mailing Zip": "95112",
         "Discipline": "AHA© BLS Provider Course", "Course Date": "6/1/26"},
        {"Instructor": "Jane Doe", "Class ID": "1", "Mailing Zip": "95113",
         "Discipline": "AHA© BLS Provider Course", "Course Date": "6/1/26"},
        {"Instructor": "Jane Doe", "Class ID": "2", "Mailing Zip": "95112",
         "Discipline": "ARC CPR", "Course Date": "7/5/26"},
        {"Instructor": "Cancel (weather)", "Class ID": "9", "Mailing Zip": "1",
         "Discipline": "ARC CPR", "Course Date": "7/5/26"},
    ]
    roster = {"jane doe": {"city": "San Jose", "state": "CA", "zip": "95112"}}
    out = conv.aggregate_instructor_performance(student_rows, roster)
    assert len(out) == 1  # the Cancel row is dropped
    row = out[0]
    assert row["name"] == "Jane Doe"
    assert row["students"] == "3"
    assert row["classes"] == "2"
    assert row["zips"] == "2"
    assert row["aha_bls_students"] == "2"
    assert row["arc_cpr_students"] == "1"
    assert row["teaches_aha"] == "yes" and row["teaches_arc"] == "yes"
    assert row["home_city"] == "San Jose"
    assert row["last_taught"] == "7/5/26"  # chronological max, not lexical


# --------------------------------------------------------------------------
# Performance scoring
# --------------------------------------------------------------------------
def _perf_row(**kw):
    base = {"name": "X", "students": "0", "classes": "0", "zips": "0",
            "students_per_class": "", "aha_bls_students": "0",
            "arc_bls_students": "0", "arc_cpr_students": "0",
            "teaches_aha": "no", "teaches_arc": "no", "home_city": "",
            "home_state": "", "home_zip": "", "last_taught": ""}
    base.update({k: str(v) for k, v in kw.items()})
    return perf.enrich(base)


def test_performance_score_and_tier_ordering():
    strong = _perf_row(students=500, classes=60, zips=120,
                       students_per_class=8, aha_bls_students=200,
                       arc_bls_students=150, arc_cpr_students=150)
    weak = _perf_row(students=20, classes=10, zips=5,
                     students_per_class=2, arc_cpr_students=20)
    assert strong["performance_score"] > weak["performance_score"]
    assert strong["performance_tier"] == perf.TIER_TOP
    assert weak["performance_tier"] in (perf.TIER_LOW, perf.TIER_DEVELOPING)


def test_proven_courses_from_taught_counts():
    row = _perf_row(aha_bls_students=10, arc_cpr_students=5)
    assert AHA_BLS in row["proven_courses"]
    assert ARC_CPR_FA_AED in row["proven_courses"]
    assert ARC_BLS not in row["proven_courses"]


# --------------------------------------------------------------------------
# Eligibility + bridge logic (the correctness core)
# --------------------------------------------------------------------------
def test_is_eligible_requires_having_taught_the_course():
    arc_only = _perf_row(arc_bls_students=100, arc_cpr_students=50)
    assert src.is_eligible(arc_only, ARC_BLS) is True
    assert src.is_eligible(arc_only, AHA_BLS) is False


def test_bridge_only_crosses_product_lines():
    arc_only = _perf_row(arc_bls_students=100, arc_cpr_students=50)
    aha_only = _perf_row(aha_bls_students=100)
    both = _perf_row(aha_bls_students=50, arc_bls_students=50)
    # ARC instructor can bridge TO AHA (but is not already AHA-eligible).
    assert src.can_bridge_to(arc_only, AHA_BLS) is True
    # AHA instructor can bridge TO ARC.
    assert src.can_bridge_to(aha_only, ARC_BLS) is True
    # Someone already eligible is never a "bridge" candidate for that course.
    assert src.can_bridge_to(both, AHA_BLS) is False
    assert src.can_bridge_to(arc_only, ARC_BLS) is False


def test_credential_requirements_are_org_correct():
    aha = src.credential_requirement(AHA_BLS)
    arc = src.credential_requirement(ARC_BLS)
    assert "American Heart Association" in aha["issuing_body"]
    assert "Training Center" in " ".join(aha["prerequisites"])
    assert "American Red Cross" in arc["issuing_body"]
    assert "Licensed Training Provider" in " ".join(arc["prerequisites"])
    # Only Red Cross advertises a bridge for already-certified instructors.
    assert "bridge" in arc["bridge"].lower()
    assert "no aha bridge" in aha["bridge"].lower()


# --------------------------------------------------------------------------
# Winning profile + plan
# --------------------------------------------------------------------------
def _sample_rows():
    return [
        _perf_row(name="Top AHA", students=500, classes=60, zips=120,
                  students_per_class=8, aha_bls_students=300,
                  arc_bls_students=100, arc_cpr_students=100,
                  home_state="TX", home_zip="75052"),
        _perf_row(name="Top ARC TX", students=430, classes=55, zips=115,
                  students_per_class=7.8, arc_bls_students=200,
                  arc_cpr_students=230, home_state="TX", home_zip="75001"),
        _perf_row(name="Top ARC CA", students=400, classes=50, zips=100,
                  students_per_class=8, arc_bls_students=200,
                  arc_cpr_students=200, home_state="CA", home_zip="95112"),
        _perf_row(name="Low ARC", students=20, classes=10, zips=5,
                  students_per_class=2, arc_cpr_students=20,
                  home_state="NY", home_zip="10001"),
    ]


def test_winning_profile_summarizes_top_performers():
    prof = src.winning_profile(_sample_rows(), course=ARC_BLS)
    assert prof["sample_size"] == 3   # three taught ARC_BLS
    assert prof["benchmark_students_per_class"] is not None
    assert prof["credential_mix"]["arc"] == 3
    assert any(p["name"] == "Top ARC TX"
               for p in prof["example_top_performers"])


def test_sourcing_plan_activate_bridge_and_near_ranking():
    plan = src.build_sourcing_plan(AHA_BLS, state="TX", rows=_sample_rows())
    assert plan["eligibility"]["credential"] == "AHA BLS Instructor"
    # Activate = proven AHA instructors.
    act = [c["name"] for c in plan["activate_existing"]["candidates"]]
    assert "Top AHA" in act
    assert "Top ARC TX" not in act
    # Bridge = ARC-only performers; the TX one ranks first (near target).
    bridge = plan["bridge_candidates"]["candidates"]
    bridge_names = [c["name"] for c in bridge]
    assert "Top ARC TX" in bridge_names and "Top ARC CA" in bridge_names
    assert bridge[0]["name"] == "Top ARC TX"
    assert bridge[0]["near_target_area"] is True


def test_eligibility_queries_are_org_specific():
    aha_q = " ".join(q["query"] for q in
                     src.eligibility_search_queries(AHA_BLS, city="Hayward",
                                                    state="CA"))
    arc_q = " ".join(q["query"] for q in
                     src.eligibility_search_queries(ARC_BLS, city="Hayward",
                                                    state="CA"))
    assert "AHA BLS Instructor" in aha_q and "Training Center" in aha_q
    assert "Red Cross Instructor" in arc_q
    assert "Licensed Training Provider" in arc_q
    assert "Hayward CA" in aha_q


def test_sourcing_plan_has_no_sensitive_keys():
    plan = src.build_sourcing_plan(AHA_BLS, rows=_sample_rows())
    scrubbed = scrub_sensitive(plan)
    assert "door_code" not in str(scrubbed)
    # Plan survives scrubbing intact.
    assert scrubbed["activate_existing"]["count"] >= 1


def test_sourcing_plan_includes_screening_indeed_and_allcpr_role():
    plan = src.build_sourcing_plan(AHA_BLS, zip_code="95112", city="San Jose",
                                   state="CA", rows=_sample_rows())
    # Screening bar present.
    assert plan["screening"]["equipment_required"]["adult_manikins"] == 4
    # ALLCPR is the Training Site instructors align to (AHA product line).
    assert "Training Site" in plan["eligibility"]["allcpr_role"]
    # Indeed posting plan present in the external lane.
    ip = plan["external_sourcing"]["indeed_plan"]
    assert ip["job_title"] == "AHA BLS Instructor"
    assert ip["posting_action"] in ("post_free", "sponsor")
    # Candidate views carry the company A–E grade.
    if plan["activate_existing"]["candidates"]:
        assert "company_grade" in plan["activate_existing"]["candidates"][0]


def test_sourcing_plan_bridge_role_is_red_cross_for_arc_course():
    plan = src.build_sourcing_plan(ARC_BLS, rows=_sample_rows())
    assert "Licensed Training Provider" in plan["eligibility"]["allcpr_role"]
