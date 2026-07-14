"""ZIP-level demand tests: extraction, join, aggregation, scoring, resolution,
report rendering. Fixture dates are in 2025 so they are always 'held'
(completed months) regardless of when the suite runs."""
import csv

import pytest

from app.collectors.enrollware import (
    EnrollwareClassRecord,
    _parse_city_state,
    load_enrollware,
)


# --------------------------------------------------------------------------- #
# ZIP extraction from the Locations address field
# --------------------------------------------------------------------------- #

class TestZipExtraction:
    def test_parse_city_state_returns_zip(self):
        city, state, zip_code = _parse_city_state(
            "San Jose(1631 N First Street, Suite 200, San Jose, CA 95112)")
        assert city == "San Jose"
        assert state == "CA"
        assert zip_code == "95112"

    def test_parse_city_state_without_zip(self):
        city, state, zip_code = _parse_city_state("Group Training")
        assert zip_code is None

    def test_record_has_zip_field_defaulting_none(self):
        rec = EnrollwareClassRecord(
            class_name="AHA BLS Provider", course_type="aha_bls",
            course_type_label="AHA BLS")
        assert rec.zip is None
        assert "zip" in rec.to_dict()


# --------------------------------------------------------------------------- #
# class -> location ZIP join
# --------------------------------------------------------------------------- #

def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


@pytest.fixture
def export_files(tmp_path):
    classes = tmp_path / "classes.csv"
    locations = tmp_path / "locations.csv"
    _write_csv(classes,
               ["Class Name", "Class Date", "Students Enrolled",
                "Max Students", "Location"],
               [
                   ["AHA BLS Provider", "2025-03-01", "9", "12", "San Jose"],
                   ["ARC Adult CPR/AED", "2025-03-08", "5", "12", "San Jose"],
                   ["ARC BLS for Healthcare Providers", "2025-03-15", "6",
                    "12", "Santa Clara"],
                   ["AHA BLS Provider", "2025-04-05", "8", "12", "Ambig"],
               ])
    _write_csv(locations,
               ["Abbreviation", "Name"],
               [
                   ["San Jose",
                    "San Jose(1631 N First Street, Suite 200, San Jose, CA 95112)"],
                   ["Santa Clara",
                    "Santa Clara(2901 Tasman Dr, Santa Clara, CA 95054)"],
                   # same abbreviation, two different cities -> ambiguous
                   ["Ambig", "Plano(123 Main St, Plano, TX 75023)"],
                   ["Ambig", "Troy(456 Oak Ave, Troy, MI 48083)"],
               ])
    return classes, locations


class TestZipJoin:
    def test_join_attaches_zip_from_locations(self, export_files):
        classes, locations = export_files
        records, _dq = load_enrollware(classes, locations_path=locations)
        by_loc = {r.location: r for r in records}
        assert by_loc["San Jose"].zip == "95112"
        assert by_loc["San Jose"].city == "San Jose"
        assert by_loc["Santa Clara"].zip == "95054"

    def test_ambiguous_abbreviation_gets_no_zip(self, export_files):
        classes, locations = export_files
        records, _dq = load_enrollware(classes, locations_path=locations)
        ambig = next(r for r in records if r.location == "Ambig")
        assert ambig.zip is None
        assert ambig.city is None  # existing behavior preserved

    def test_no_locations_file_leaves_zip_none(self, export_files, monkeypatch):
        # Hermetic: pin discovery to "no locations file" so the result does not
        # depend on whether a real export happens to sit in data/raw.
        monkeypatch.setattr("app.collectors.enrollware.LOCATIONS_FILES", [])
        classes, _ = export_files
        records, _dq = load_enrollware(classes)
        assert all(r.zip is None for r in records)

    def test_missing_zip_counter(self, tmp_path):
        classes = tmp_path / "classes.csv"
        locations = tmp_path / "locations.csv"
        _write_csv(classes,
                   ["Class Name", "Class Date", "Students Enrolled",
                    "Max Students", "Location"],
                   [["AHA BLS Provider", "2025-03-01", "9", "12", "NoZip"]])
        _write_csv(locations, ["Abbreviation", "Name"],
                   [["NoZip", "Somewhere Office Park"]])  # no parseable ZIP
        records, dq = load_enrollware(classes, locations_path=locations)
        assert records[0].zip is None
        assert dq.missing_zip == 1

    def test_same_city_multiple_zips_gets_no_zip(self, tmp_path):
        classes = tmp_path / "classes.csv"
        locations = tmp_path / "locations.csv"
        _write_csv(classes,
                   ["Class Name", "Class Date", "Students Enrolled",
                    "Max Students", "Location"],
                   [["AHA BLS Provider", "2025-03-01", "9", "12", "MultiZip"]])
        _write_csv(locations, ["Abbreviation", "Name"],
                   [["MultiZip", "San Jose(100 First St, San Jose, CA 95112)"],
                    ["MultiZip", "San Jose(200 Second St, San Jose, CA 95113)"]])
        records, dq = load_enrollware(classes, locations_path=locations)
        assert records[0].zip is None
        assert dq.missing_zip == 1


# --------------------------------------------------------------------------- #
# ZIP aggregation math
# --------------------------------------------------------------------------- #

from app.scoring.course_types import AHA_BLS, ARC_BLS, ARC_CPR
from app.scoring.zip_demand import aggregate_zip_demand


def _rec(name, ctype, zip_code, date, enrolled, capacity, city="San Jose"):
    return EnrollwareClassRecord(
        class_name=name, course_type=ctype, course_type_label=ctype,
        date=date, month=date[:7], enrolled=enrolled, capacity=capacity,
        city=city, state="CA", zip=zip_code)


@pytest.fixture
def zip_records():
    return [
        # 95112: 2x AHA BLS, 1x ARC CPR, 1x ARC BLS
        _rec("AHA BLS Provider", "aha_bls", "95112", "2025-02-01", 9, 12),
        _rec("AHA BLS Provider", "aha_bls", "95112", "2025-03-01", 11, 12),
        _rec("ARC Adult CPR/AED", "arc_cpr", "95112", "2025-03-08", 4, 12),
        _rec("ARC BLS for Healthcare Providers", "arc_bls", "95112",
             "2025-04-15", 6, 12),
        # OTHER class in same ZIP: counts in totals, not in the three buckets
        _rec("ALLCPR BLS Provider Course", "allcpr_bls", "95112",
             "2025-04-20", 7, 12),
        # different ZIP
        _rec("AHA BLS Provider", "aha_bls", "95054", "2025-03-15", 8, 12,
             city="Santa Clara"),
        # excluded: zero-enrolled (not held)
        _rec("AHA BLS Provider", "aha_bls", "95112", "2025-05-01", 0, 12),
        # excluded: no ZIP
        _rec("AHA BLS Provider", "aha_bls", None, "2025-05-02", 9, 12),
    ]


class TestZipAggregation:
    def test_groups_by_zip_and_skips_unzipped(self, zip_records):
        demand = aggregate_zip_demand(zip_records)
        assert set(demand) == {"95112", "95054"}

    def test_totals_use_held_classes_only(self, zip_records):
        p = aggregate_zip_demand(zip_records)["95112"]
        assert p.total_classes == 5            # zero-enrolled row excluded
        assert p.total_students == 9 + 11 + 4 + 6 + 7
        assert p.average_students_per_class == pytest.approx(37 / 5, abs=0.01)
        assert p.total_capacity == 60
        assert p.fill_rate == pytest.approx(100.0 * 37 / 60, abs=0.1)
        assert p.earliest_class_date == "2025-02-01"
        assert p.latest_class_date == "2025-04-20"

    def test_bucket_breakdown(self, zip_records):
        p = aggregate_zip_demand(zip_records)["95112"]
        assert p.buckets[AHA_BLS].classes == 2
        assert p.buckets[AHA_BLS].students == 20
        assert p.buckets[AHA_BLS].avg_students == pytest.approx(10.0)
        assert p.buckets[ARC_CPR].classes == 1
        assert p.buckets[ARC_BLS].classes == 1
        # ALLCPR house class is in totals (5) but in none of the 3 buckets
        assert sum(b.classes for b in p.buckets.values()) == 4
        # only the three charted channels exist as bucket keys
        from app.scoring.course_types import OTHER
        assert OTHER not in p.buckets

    def test_cities_recorded_for_fallback(self, zip_records):
        demand = aggregate_zip_demand(zip_records)
        assert "san jose" in demand["95112"].cities
        assert "santa clara" in demand["95054"].cities

    def test_to_dict_is_json_safe(self, zip_records):
        d = aggregate_zip_demand(zip_records)["95112"].to_dict()
        assert d["zip"] == "95112"
        assert d["buckets"][ARC_CPR]["classes"] == 1

    def test_per_bucket_fill_rate(self, zip_records):
        p = aggregate_zip_demand(zip_records)["95112"]
        # AHA_BLS held: 9/12 + 11/12 -> 20/24 = 83.3%
        assert p.buckets[AHA_BLS].capacity == 24
        assert p.buckets[AHA_BLS].fill_rate == pytest.approx(83.3, abs=0.1)
        # ARC_CPR held: 4/12 -> 33.3%; distinct from the AHA_BLS fill above
        assert p.buckets[ARC_CPR].fill_rate == pytest.approx(33.3, abs=0.1)
        assert p.buckets[ARC_CPR].fill_rate != p.buckets[AHA_BLS].fill_rate


# --------------------------------------------------------------------------- #
# Demand score, bounded adjustment, confidence modifier
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import (
    ZipDemandProfile,
    BucketStat,
    compute_confidence_modifier,
    compute_score_adjustment,
    compute_zip_demand_score,
)


def _profile(classes, avg, fill, latest, zip_code="95112", earliest=None):
    from app.scoring.course_types import REPORT_BUCKETS
    per = max(1, classes // 3)
    return ZipDemandProfile(
        zip=zip_code, total_classes=classes,
        total_students=int(round(avg * classes)) if avg else None,
        average_students_per_class=avg, fill_rate=fill,
        earliest_class_date=earliest or latest,
        latest_class_date=latest,
        buckets={b: BucketStat(classes=per, students=int(per * (avg or 0)))
                 for b in REPORT_BUCKETS},
    )


class TestZipDemandScore:
    def test_strong_recent_full_scores_high(self):
        p = _profile(40, 8.0, 90.0, "2025-05-15")
        s = compute_zip_demand_score(p, latest_export_date="2025-05-20")
        assert s >= 80

    def test_high_count_low_fill_is_weak(self):
        # the spec guard: volume cannot mask emptiness
        p = _profile(40, 8.0, 15.0, "2025-05-15")
        s = compute_zip_demand_score(p, latest_export_date="2025-05-20")
        assert s < 60

    def test_sparse_old_scores_low(self):
        p = _profile(2, 2.0, 30.0, "2023-01-10")
        s = compute_zip_demand_score(p, latest_export_date="2025-05-20")
        assert s < 40

    def test_unknown_fill_is_neutral_not_fatal(self):
        full = _profile(20, 6.0, 100.0, "2025-05-01")
        none_fill = _profile(20, 6.0, None, "2025-05-01")
        s_full = compute_zip_demand_score(full, latest_export_date="2025-05-20")
        s_none = compute_zip_demand_score(none_fill, latest_export_date="2025-05-20")
        assert 0 < s_none < s_full

    def test_score_range(self):
        p = _profile(40, 10.0, 100.0, "2025-05-20")
        assert 0 <= compute_zip_demand_score(
            p, latest_export_date="2025-05-20") <= 100

    def test_empty_or_missing_profile_scores_none(self):
        assert compute_zip_demand_score(None) is None
        assert compute_zip_demand_score(ZipDemandProfile(zip="00000")) is None

    def test_nan_fill_is_neutral_like_unknown(self):
        # A non-finite fill must score identically to an unknown (None) fill —
        # never silently treated as fully booked.
        nan_fill = _profile(20, 6.0, float("nan"), "2025-05-01")
        none_fill = _profile(20, 6.0, None, "2025-05-01")
        assert (compute_zip_demand_score(nan_fill, latest_export_date="2025-05-20")
                == compute_zip_demand_score(none_fill,
                                            latest_export_date="2025-05-20"))

    def test_score_uses_charted_avg_not_overall(self):
        from app.scoring.course_types import REPORT_BUCKETS
        # Identical overall avg (20), but charted-channel avg differs: a ZIP
        # whose three channels are small classes must score LOWER than one whose
        # channels are large, even if OTHER-bucket inflates the overall avg.
        small = ZipDemandProfile(
            zip="95112", total_classes=30, total_students=600,
            average_students_per_class=20.0, fill_rate=100.0,
            latest_class_date="2025-05-01",
            buckets={b: BucketStat(classes=2, students=4)
                     for b in REPORT_BUCKETS})   # charted avg = 2
        large = ZipDemandProfile(
            zip="95113", total_classes=30, total_students=600,
            average_students_per_class=20.0, fill_rate=100.0,
            latest_class_date="2025-05-01",
            buckets={b: BucketStat(classes=2, students=40)
                     for b in REPORT_BUCKETS})   # charted avg = 20
        s_small = compute_zip_demand_score(
            small, reference_avg=6.0, latest_export_date="2025-05-20")
        s_large = compute_zip_demand_score(
            large, reference_avg=6.0, latest_export_date="2025-05-20")
        assert s_small < s_large

    def test_nan_inputs_are_neutralized(self):
        assert compute_score_adjustment(float("nan")) == 0.0
        p = _profile(20, 6.0, float("nan"), "2025-05-01")
        s = compute_zip_demand_score(p, latest_export_date="2025-05-20")
        assert s is not None and 0 <= s <= 100

    def test_missing_export_anchor_is_neutral_recency(self):
        anchored = _profile(20, 6.0, 80.0, "2025-05-01")
        unanchored = _profile(20, 6.0, 80.0, "2025-05-01")
        s_anchored = compute_zip_demand_score(
            anchored, latest_export_date="2025-05-01")  # 0 months -> recency 1.0
        s_unanchored = compute_zip_demand_score(unanchored)  # no anchor -> 0.5
        assert s_unanchored < s_anchored


class TestScoreAdjustment:
    @pytest.mark.parametrize("score,expected", [
        (50, 0.0),
        (80, 3.0),     # the spec's worked example
        (100, 5.0),
        (0, -5.0),
        (30, -2.0),
    ])
    def test_formula(self, score, expected):
        assert compute_score_adjustment(score) == pytest.approx(expected)

    def test_caps(self):
        assert compute_score_adjustment(150) == 5.0
        assert compute_score_adjustment(-50) == -5.0


class TestConfidenceModifier:
    def test_strong_activity_boosts(self):
        p = _profile(25, 7.0, 85.0, "2025-05-01")
        m = compute_confidence_modifier(p, latest_export_date="2025-05-20")
        assert 5 <= m <= 10

    def test_sparse_stale_penalizes(self):
        p = _profile(2, 3.0, 30.0, "2023-06-01")
        m = compute_confidence_modifier(p, latest_export_date="2025-05-20")
        assert m == -10.0

    def test_no_history_is_neutral(self):
        assert compute_confidence_modifier(None) == 0.0
        empty = ZipDemandProfile(zip="00000")
        assert compute_confidence_modifier(empty) == 0.0


# --------------------------------------------------------------------------- #
# Candidate ZIP parsing, centroid loading, resolution
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import (
    load_zip_centroids,
    parse_zip,
    resolve_candidate_zips,
)


class TestParseZip:
    def test_parses_trailing_zip(self):
        assert parse_zip("1631 N First St, San Jose, CA 95112") == "95112"

    def test_parses_zip_plus_four(self):
        assert parse_zip("1631 N First St, San Jose, CA 95112-2806") == "95112"

    def test_no_zip_returns_none(self):
        assert parse_zip("Santana Row, San Jose, CA") is None
        assert parse_zip(None) is None

    def test_ignores_street_numbers(self):
        # 5-digit street number must not win over the trailing ZIP
        assert parse_zip("12345 Oak Ave, San Jose, CA 95054") == "95054"

    def test_bare_zip_string(self):
        assert parse_zip("95112") == "95112"

    def test_usa_suffix_stripped(self):
        assert parse_zip("1631 N First St, San Jose, CA 95112, USA") == "95112"

    def test_city_only_with_usa(self):
        assert parse_zip("San Jose, CA, USA") is None


class TestCentroids:
    def test_loads_committed_file(self):
        # The committed file is meant to be refreshed from the Census ZCTA
        # gazetteer (scripts/build_zip_centroids.py), so pin a stable fact —
        # 95112 is present and sits in San Jose — not exact hand-typed coords.
        centroids = load_zip_centroids()
        lat, lng = centroids["95112"]
        assert 37.30 <= lat <= 37.40
        assert -121.95 <= lng <= -121.80

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_zip_centroids(tmp_path / "nope.csv") == {}


@pytest.fixture
def demand_map(zip_records):
    return aggregate_zip_demand(zip_records)


class TestResolveCandidateZips:
    CENTROIDS = {
        "95112": (37.3422, -121.8830),
        "95054": (37.3929, -121.9624),   # ~5.6 mi from 95112
    }

    def test_exact_plus_radius(self, demand_map):
        zips, basis = resolve_candidate_zips(
            demand_map, candidate_zip="95112",
            latitude=37.3422, longitude=-121.8830,
            city="San Jose", centroids=self.CENTROIDS, radius_miles=10.0)
        assert basis == "exact_plus_radius"
        assert set(zips) == {"95112", "95054"}
        assert zips[0] == "95112"   # candidate's own ZIP listed first

    def test_radius_excludes_far_zips(self, demand_map):
        zips, basis = resolve_candidate_zips(
            demand_map, candidate_zip="95112",
            latitude=37.3422, longitude=-121.8830,
            city="San Jose", centroids=self.CENTROIDS, radius_miles=3.0)
        assert basis == "exact_plus_radius"
        assert zips == ["95112"]

    def test_exact_only_without_centroids(self, demand_map):
        zips, basis = resolve_candidate_zips(
            demand_map, candidate_zip="95112", latitude=None, longitude=None,
            city="San Jose", centroids=None)
        assert (zips, basis) == (["95112"], "exact")

    def test_city_fallback_without_zip(self, demand_map):
        zips, basis = resolve_candidate_zips(
            demand_map, candidate_zip=None, latitude=None, longitude=None,
            city="Santa Clara", centroids=None)
        assert (zips, basis) == (["95054"], "city")

    def test_none_when_nothing_matches(self, demand_map):
        zips, basis = resolve_candidate_zips(
            demand_map, candidate_zip="90210", latitude=None, longitude=None,
            city="Los Angeles", centroids=None)
        assert (zips, basis) == ([], "none")


# --------------------------------------------------------------------------- #
# Candidate payload builder
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import build_candidate_zip_demand


class TestBuildCandidateZipDemand:
    def test_payload_shape_exact_match(self, demand_map):
        payload = build_candidate_zip_demand(
            demand_map, candidate_zip="95112", latitude=None, longitude=None,
            city="San Jose", centroids=None, latest_export_date="2025-05-20")
        assert payload["match_basis"] == "exact"
        assert payload["resolved_zips"] == ["95112"]
        assert payload["primary_zip"] == "95112"
        assert isinstance(payload["zip_demand_score"], float)
        assert -5.0 <= payload["adjustment"] <= 5.0
        assert -10.0 <= payload["confidence_modifier"] <= 10.0
        assert payload["strength"] in (
            "Very Strong", "Strong", "Moderate", "Weak", "Very Weak")
        assert payload["profiles"][0]["zip"] == "95112"
        # combined profile mirrors the single resolved ZIP here
        assert payload["combined"]["total_classes"] == 5

    def test_multi_zip_combines_for_scoring(self, demand_map):
        centroids = {"95112": (37.3422, -121.8830),
                     "95054": (37.3929, -121.9624)}
        payload = build_candidate_zip_demand(
            demand_map, candidate_zip="95112",
            latitude=37.3422, longitude=-121.8830, city="San Jose",
            centroids=centroids, radius_miles=10.0,
            latest_export_date="2025-05-20")
        assert payload["match_basis"] == "exact_plus_radius"
        assert payload["combined"]["total_classes"] == 6  # 5 + 1

    def test_no_match_yields_neutral_payload(self, demand_map):
        payload = build_candidate_zip_demand(
            demand_map, candidate_zip="90210", latitude=None, longitude=None,
            city="Los Angeles", centroids=None)
        assert payload["match_basis"] == "none"
        assert payload["zip_demand_score"] is None
        assert payload["adjustment"] == 0.0
        assert payload["confidence_modifier"] == 0.0
        assert payload["profiles"] == []
        assert payload["strength"] is None
        assert payload["combined"] is None
        assert payload["resolved_zips"] == []
        assert payload["primary_zip"] is None

    def test_payload_is_json_serializable(self, demand_map):
        import json
        payload = build_candidate_zip_demand(
            demand_map, candidate_zip="95112", latitude=None, longitude=None,
            city="San Jose", centroids=None)
        json.dumps(payload)  # must not raise

    def test_city_basis_payload(self, demand_map):
        payload = build_candidate_zip_demand(
            demand_map, candidate_zip=None, latitude=None, longitude=None,
            city="Santa Clara", centroids=None,
            latest_export_date="2025-05-20")
        assert payload["match_basis"] == "city"
        assert payload["resolved_zips"] == ["95054"]
        assert payload["primary_zip"] == "95054"
        assert payload["combined"] is not None
        assert isinstance(payload["zip_demand_score"], float)


# --------------------------------------------------------------------------- #
# Reference helpers: overall average + export recency anchor
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import latest_export_date, overall_reference_avg


class TestReferenceHelpers:
    def test_overall_reference_avg(self, zip_records):
        # held classes only (all except zero-enrolled row):
        # 9 + 11 + 4 + 6 + 7 + 8 + 9 students over 7 held classes = 54/7 ≈ 7.71
        assert overall_reference_avg(zip_records) == pytest.approx(7.71)

    def test_overall_reference_avg_empty(self):
        assert overall_reference_avg([]) is None

    def test_latest_export_date(self, zip_records):
        # includes all records: the raw max across ALL records
        assert latest_export_date(zip_records) == "2025-05-02"

    def test_latest_export_date_empty(self):
        assert latest_export_date([]) is None


# --------------------------------------------------------------------------- #
# site_score integration: bounded final_score + adjusted confidence
# --------------------------------------------------------------------------- #

from app.scoring.site_score import score_profile


def _minimal_profile(**extra):
    p = {
        "city": "San Jose", "state": "CA",
        "counts_5mi": {}, "counts_by_bucket": {},
        "competition_summary": {}, "economy": {},
    }
    p.update(extra)
    return p


class TestSiteScoreZipIntegration:
    def test_no_zip_demand_keeps_final_equal_to_area(self):
        scored = score_profile(_minimal_profile())
        assert scored["final_score"] == scored["area_score"]
        assert scored["zip_demand_adjustment"] == 0.0
        assert scored["base_score"] == scored["area_score"]

    def test_positive_adjustment_applied(self):
        zd = {"zip_demand_score": 80.0, "adjustment": 3.0,
              "confidence_modifier": 5.0, "strength": "Strong",
              "match_basis": "exact", "resolved_zips": ["95112"],
              "primary_zip": "95112", "profiles": [], "combined": None}
        scored = score_profile(_minimal_profile(zip_demand=zd))
        assert scored["final_score"] == pytest.approx(
            min(100.0, scored["area_score"] + 3.0))
        assert scored["zip_demand_adjustment"] == 3.0
        assert scored["zip_demand_score"] == 80.0
        conf = scored["sub_scores"]["confidence_score"]
        assert scored["confidence_score_adjusted"] == pytest.approx(
            min(100.0, conf + 5.0))

    def test_ranking_score_is_untouched_area_score(self):
        zd = {"zip_demand_score": 100.0, "adjustment": 5.0,
              "confidence_modifier": 10.0, "strength": "Very Strong",
              "match_basis": "exact", "resolved_zips": ["95112"],
              "primary_zip": "95112", "profiles": [], "combined": None}
        scored = score_profile(_minimal_profile(zip_demand=zd))
        # the bounded signal must NOT reorder the portfolio
        assert scored["ranking_score"] == scored["area_score"]

    def test_oversized_adjustment_is_recapped(self):
        zd = {"zip_demand_score": 100.0, "adjustment": 50.0,
              "confidence_modifier": 40.0, "strength": "Very Strong",
              "match_basis": "exact", "resolved_zips": ["95112"],
              "primary_zip": "95112", "profiles": [], "combined": None}
        scored = score_profile(_minimal_profile(zip_demand=zd))
        assert scored["final_score"] <= scored["area_score"] + 5.0
        conf = scored["sub_scores"]["confidence_score"]
        assert scored["confidence_score_adjusted"] <= min(100.0, conf + 10.0)

    def test_rationale_mentions_zip_demand(self):
        zd = {"zip_demand_score": 80.0, "adjustment": 3.0,
              "confidence_modifier": 5.0, "strength": "Strong",
              "match_basis": "exact", "resolved_zips": ["95112"],
              "primary_zip": "95112", "profiles": [], "combined": None}
        scored = score_profile(_minimal_profile(zip_demand=zd))
        assert any("zip" in r.lower() for r in scored["rationale"])

    def test_malformed_zip_demand_never_crashes(self):
        # non-numeric adjustment string
        scored = score_profile(_minimal_profile(
            zip_demand={"adjustment": "high", "confidence_modifier": "low"}))
        assert scored["final_score"] == scored["area_score"]
        assert scored["zip_demand_adjustment"] == 0.0
        # zip_demand is a truthy non-dict
        scored2 = score_profile(_minimal_profile(zip_demand="pending"))
        assert scored2["final_score"] == scored2["area_score"]
        # resolved_zips is a string, not a list — rationale must not garble
        scored3 = score_profile(_minimal_profile(
            zip_demand={"zip_demand_score": 80.0, "adjustment": 3.0,
                        "confidence_modifier": 5.0, "strength": "Strong",
                        "match_basis": "exact", "resolved_zips": "95112"}))
        assert not any("9, 5, 1, 1, 2" in r for r in scored3["rationale"])


# --------------------------------------------------------------------------- #
# Report rendering: ZIP-level course demand section
# --------------------------------------------------------------------------- #

from app.reports.html_report import _zip_demand_html


def _zip_payload():
    return {
        "resolved_zips": ["95112"], "match_basis": "exact",
        "primary_zip": "95112", "zip_demand_score": 83.0,
        "adjustment": 3.3, "confidence_modifier": 7.0, "strength": "Strong",
        "radius_miles": 5.0,
        "bucket_labels": {"ARC_CPR": "ARC CPR", "ARC_BLS": "ARC BLS",
                          "AHA_BLS": "AHA BLS", "OTHER": "Other"},
        "profiles": [{
            "zip": "95112", "total_classes": 59, "total_students": 240,
            "average_students_per_class": 4.07, "total_capacity": 700,
            "fill_rate": 34.3, "latest_class_date": "2025-05-01",
            "cities": ["san jose"],
            "buckets": {
                "ARC_CPR": {"classes": 38, "students": 144, "avg_students": 3.8},
                "ARC_BLS": {"classes": 12, "students": 49, "avg_students": 4.1},
                "AHA_BLS": {"classes": 9, "students": 47, "avg_students": 5.2},
            },
        }],
        "combined": {
            "zip": "95112", "total_classes": 59, "total_students": 240,
            "average_students_per_class": 4.07, "total_capacity": 700,
            "fill_rate": 34.3, "latest_class_date": "2025-05-01",
            "cities": ["san jose"],
            "buckets": {
                "ARC_CPR": {"classes": 38, "students": 144, "avg_students": 3.8},
                "ARC_BLS": {"classes": 12, "students": 49, "avg_students": 4.1},
                "AHA_BLS": {"classes": 9, "students": 47, "avg_students": 5.2},
            },
        },
    }


class TestZipDemandHtml:
    def test_renders_three_bucket_rows(self):
        html = _zip_demand_html(
            {"zip_demand": _zip_payload()},
            {"area_score": 72.0, "final_score": 75.3,
             "zip_demand_adjustment": 3.3, "confidence_score_adjusted": 88.0,
             "sub_scores": {"confidence_score": 81.0}})
        assert "ARC CPR" in html and "ARC BLS" in html and "AHA BLS" in html
        assert "38" in html and "3.8" in html      # classes + avg from example
        assert "95112" in html
        assert "Strong" in html

    def test_score_summary_line(self):
        html = _zip_demand_html(
            {"zip_demand": _zip_payload()},
            {"area_score": 72.0, "final_score": 75.3,
             "zip_demand_adjustment": 3.3, "confidence_score_adjusted": 88.0,
             "sub_scores": {"confidence_score": 81.0}})
        assert "+3.3" in html
        assert "75.3" in html

    def test_bar_visual_present(self):
        html = _zip_demand_html({"zip_demand": _zip_payload()}, {})
        assert "zip-bar" in html   # the inline-CSS bar rows

    def test_per_channel_fill_rendered(self):
        payload = _zip_payload()
        # give each charted channel a distinct fill; overall stays 34.3
        payload["combined"]["buckets"]["ARC_CPR"]["fill_rate"] = 30.0
        payload["combined"]["buckets"]["ARC_BLS"]["fill_rate"] = 68.0
        payload["combined"]["buckets"]["AHA_BLS"]["fill_rate"] = 75.0
        html = _zip_demand_html({"zip_demand": payload}, {})
        assert "30.0%" in html and "68.0%" in html and "75.0%" in html
        assert "overall fill 34.3%" in html   # overall moved to the caption

    def test_absent_payload_renders_muted_note(self):
        html = _zip_demand_html({}, {})
        assert "muted" in html
        html2 = _zip_demand_html(
            {"zip_demand": {"match_basis": "none", "resolved_zips": [],
                            "profiles": [], "combined": None,
                            "zip_demand_score": None, "adjustment": 0.0,
                            "confidence_modifier": 0.0, "strength": None}},
            {})
        assert "muted" in html2

    def test_candidate_card_includes_section(self):
        from app.reports.html_report import _candidate_card
        item = {
            "profile": {"zip_demand": _zip_payload(), "city": "San Jose",
                        "state": "CA"},
            "scored": {"area_score": 72.0, "final_score": 75.3,
                       "zip_demand_adjustment": 3.3,
                       "confidence_score_adjusted": 88.0,
                       "sub_scores": {"confidence_score": 81.0},
                       "tier": "B"},
            "rank": 1,
        }
        card = _candidate_card(item, report_style="detailed")
        assert "ZIP-level course demand" in card

    def test_legacy_string_adjustment_never_crashes(self):
        payload = _zip_payload()
        payload["adjustment"] = "N/A"   # legacy/hand-built payload shape
        html = _zip_demand_html({"zip_demand": payload}, {})
        assert "+0.0" in html   # falls back to neutral, renders fine

    def test_city_basis_caption_lists_zips(self):
        payload = _zip_payload()
        payload["match_basis"] = "city"
        payload["resolved_zips"] = ["95110", "95112", "95113"]
        html = _zip_demand_html({"zip_demand": payload}, {})
        assert "city-level fallback" in html
        # actual ZIPs are shown, not just a count
        assert "95110" in html and "95112" in html and "95113" in html

    def test_city_basis_caption_truncates_many_zips(self):
        payload = _zip_payload()
        payload["match_basis"] = "city"
        payload["resolved_zips"] = [
            "95110", "95111", "95112", "95113", "95116", "95126"]
        html = _zip_demand_html({"zip_demand": payload}, {})
        # first four listed, ellipsis for the rest, fifth ZIP not spelled out
        assert "95110" in html and "95113" in html
        assert "…" in html
        assert "95116" not in html


# --------------------------------------------------------------------------- #
# Centroid coverage audit
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import audit_centroid_coverage


class TestCentroidCoverage:
    def test_full_coverage(self, demand_map):
        cov = audit_centroid_coverage(
            demand_map, {"95112": (37.3, -121.8), "95054": (37.4, -121.9)})
        assert cov.total_demand_zips == 2
        assert cov.missing_zips == []
        assert cov.coverage_pct == 100.0
        assert cov.radius_matching_usable is True
        assert "all 2 demand ZIP(s)" in cov.summary()

    def test_partial_coverage_lists_missing(self, demand_map):
        cov = audit_centroid_coverage(demand_map, {"95112": (37.3, -121.8)})
        assert cov.covered_zips == ["95112"]
        assert cov.missing_zips == ["95054"]
        assert cov.coverage_pct == 50.0
        assert cov.radius_matching_usable is True
        assert "95054" in cov.summary()

    def test_empty_centroids_disables_radius(self, demand_map):
        cov = audit_centroid_coverage(demand_map, {})
        assert cov.centroids_present is False
        assert cov.radius_matching_usable is False
        assert set(cov.missing_zips) == {"95112", "95054"}
        assert "radius matching disabled" in cov.summary()

    def test_no_demand_is_neutral(self):
        cov = audit_centroid_coverage({}, {"95112": (37.3, -121.8)})
        assert cov.total_demand_zips == 0
        assert cov.coverage_pct is None
        assert "no ZIP-resolved demand" in cov.summary()

    def test_to_dict_is_json_safe(self, demand_map):
        d = audit_centroid_coverage(demand_map, {"95112": (37.3, -121.8)}).to_dict()
        import json
        json.loads(json.dumps(d))
        assert d["coverage_pct"] == 50.0
        assert d["missing_zips"] == ["95054"]


# --------------------------------------------------------------------------- #
# Report-wide ZIP demand dataset (visualization layer)
# --------------------------------------------------------------------------- #

from app.scoring.zip_demand import build_zip_demand_report


def _profile_map(*specs):
    """Build {zip: ZipDemandProfile} from (zip, classes, avg, fill, latest)."""
    out = {}
    for spec in specs:
        z, c, a, f, latest = spec[:5]
        earliest = spec[5] if len(spec) > 5 else None
        out[z] = _profile(c, a, f, latest, zip_code=z, earliest=earliest)
    return out


class TestBuildZipDemandReport:
    def test_one_row_per_zip_with_all_fields(self, demand_map):
        rep = build_zip_demand_report(
            demand_map, {"95112": (37.3422, -121.8830)})
        assert rep["total_zips"] == 2
        assert {r["zip"] for r in rep["rows"]} == {"95112", "95054"}
        row = next(r for r in rep["rows"] if r["zip"] == "95112")
        # Every column the table renders + per-channel student counts present.
        for key in ("demand_score", "strength", "classes", "avg_students",
                    "fill_rate", "confidence_modifier", "centroid_present",
                    "lat", "lng", "arc_cpr_students", "arc_bls_students",
                    "aha_bls_students", "month_span",
                    "held_classes_per_month", "students_per_month",
                    "arc_cpr_students_per_month",
                    "arc_bls_students_per_month",
                    "aha_bls_students_per_month"):
            assert key in row
        # 95112 fixture: 1 ARC CPR class (4), 1 ARC BLS class (6), 2 AHA (20).
        assert row["arc_cpr_students"] == 4
        assert row["arc_bls_students"] == 6
        assert row["aha_bls_students"] == 20
        assert row["centroid_present"] is True
        assert row["lat"] == pytest.approx(37.3422)
        assert rep["total_classes"] == sum(r["classes"] for r in rep["rows"])

    def test_rows_sorted_by_demand_score_desc(self):
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01"),   # strong -> high score
            ("22222", 2, 2.0, 20.0, "2023-01-01"),    # sparse/old -> low score
        )
        rep = build_zip_demand_report(dm, {}, latest_export_date="2025-05-20")
        scores = [r["demand_score"] for r in rep["rows"]]
        assert scores == sorted(scores, reverse=True)
        assert rep["rows"][0]["zip"] == "11111"

    def test_centroid_status_and_missing_list(self, demand_map):
        rep = build_zip_demand_report(demand_map, {"95112": (37.3, -121.8)})
        by_zip = {r["zip"]: r for r in rep["rows"]}
        assert by_zip["95112"]["centroid_present"] is True
        assert by_zip["95054"]["centroid_present"] is False
        assert by_zip["95054"]["lat"] is None
        assert rep["missing_centroid_zips"] == ["95054"]
        assert rep["coverage"]["coverage_pct"] == 50.0

    def test_four_charts_share_score_x_axis(self):
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01"),
            ("22222", 12, 6.0, 70.0, "2025-04-01"),
            ("33333", 3, 3.0, 40.0, "2025-03-01"),
        )
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-05-20")["charts"]
        assert charts["x_label"] == "ZIP demand score"
        # Exactly the four required series, every one a score->outcome fit.
        keys = [s["key"] for s in charts["series"]]
        assert keys == ["arc_cpr_avg_students_per_class",
                        "arc_bls_avg_students_per_class",
                        "aha_bls_avg_students_per_class",
                        "avg_students"]
        for s in charts["series"]:
            assert s["enough_data"] is True
            assert s["n"] == 3
            assert 0.0 <= s["r_squared"] <= 1.0
            assert s["slope"] is not None
        # Shared points carry every tooltip field.
        assert charts["zips_plotted"] == 3
        assert charts["zips_excluded_no_score"] == 0
        p = charts["points"][0]
        assert {"zip", "demand_score", "classes", "arc_cpr_students",
                "arc_bls_students", "aha_bls_students", "avg_students",
                "fill_rate", "students_per_month",
                "arc_cpr_avg_students_per_class",
                "arc_bls_avg_students_per_class",
                "aha_bls_avg_students_per_class",
                "held_classes_per_month", "arc_cpr_students_per_month",
                "arc_bls_students_per_month", "aha_bls_students_per_month",
                "confidence_modifier"} <= set(p)

    def test_zero_outcome_zip_still_plotted(self):
        # A ZIP with a valid score but no ARC CPR students stays in the point
        # set at arc_cpr_students == 0 (never silently dropped).
        from app.scoring.course_types import AHA_BLS, REPORT_BUCKETS
        from app.scoring.zip_demand import BucketStat, ZipDemandProfile
        only_aha = ZipDemandProfile(
            zip="70000", total_classes=6, total_students=48,
            average_students_per_class=8.0, fill_rate=80.0,
            earliest_class_date="2025-05-01", latest_class_date="2025-05-01",
            buckets={b: BucketStat() for b in REPORT_BUCKETS})
        only_aha.buckets[AHA_BLS] = BucketStat(classes=6, students=48,
                                               avg_students=8.0)
        dm = _profile_map(("11111", 30, 9.0, 90.0, "2025-05-01"),
                          ("22222", 12, 6.0, 70.0, "2025-04-01"))
        dm["70000"] = only_aha
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-05-20")["charts"]
        pt = next(p for p in charts["points"] if p["zip"] == "70000")
        assert pt["arc_cpr_students"] == 0      # zero, present
        assert pt["aha_bls_students"] == 48
        assert charts["zips_plotted"] == 3

    def test_charts_not_enough_data(self, demand_map):
        charts = build_zip_demand_report(demand_map, {})["charts"]
        for s in charts["series"]:           # only 2 ZIPs -> no fit
            assert s["enough_data"] is False
            assert s["r_squared"] is None
            assert s["n"] <= 2

    def test_combined_opportunity_chart(self):
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01"),
            ("22222", 12, 6.0, 70.0, "2025-04-01"),
            ("33333", 3, 3.0, 40.0, "2025-03-01"),
        )
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-05-20")["charts"]
        combined = charts["combined"]
        assert combined["key"] == "historical_demand_score"
        assert "historical demand score" in combined["title"].lower()
        assert combined["enough_data"] is True
        assert combined["n"] == 3
        assert combined["slope"] is not None
        # Every plotted point carries the 0..100 combined index; the strongest
        # ZIP normalizes to 100, the weakest toward 0 — none dropped.
        idx = {p["zip"]: p["historical_demand_score"] for p in charts["points"]}
        assert set(idx) == {"11111", "22222", "33333"}
        for v in idx.values():
            assert 0.0 <= v <= 100.0
        assert idx["11111"] == pytest.approx(100.0)   # top on every metric

    def test_combined_index_balances_volume_and_efficiency(self):
        # Many weak, low-filled classes should not outrank fewer but stronger
        # classes once the index includes monthly demand and efficiency.
        dm = _profile_map(
            ("11111", 100, 2.0, 20.0, "2025-10-01", "2025-01-01"),
            ("22222", 10, 10.0, 100.0, "2025-02-01", "2025-01-01"),
            ("33333", 4, 4.0, 60.0, "2025-02-01", "2025-01-01"),
        )
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-10-15")["charts"]
        idx = {p["zip"]: p["historical_demand_score"] for p in charts["points"]}
        assert idx["22222"] > idx["11111"]

    def test_combined_index_uses_capped_normalization_for_outlier(self):
        dm = _profile_map(
            ("11111", 1000, 2.0, 20.0, "2025-10-01", "2025-01-01"),
            ("22222", 30, 8.0, 80.0, "2025-03-01", "2025-01-01"),
            ("33333", 25, 7.0, 75.0, "2025-03-01", "2025-01-01"),
            ("44444", 20, 6.0, 70.0, "2025-03-01", "2025-01-01"),
            ("55555", 15, 5.0, 65.0, "2025-03-01", "2025-01-01"),
        )
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-10-15")["charts"]
        pts = {p["zip"]: p for p in charts["points"]}
        assert pts["11111"]["normalized_students_per_month"] == 100.0
        # The next strongest ZIP still gets meaningful credit instead of being
        # crushed toward zero by the one extreme value.
        assert pts["22222"]["normalized_students_per_month"] > 20.0

    def test_combined_index_keeps_zero_channels(self):
        # A ZIP strong overall but with zero ARC CPR keeps a real zero in that
        # normalized component (it is still counted, never dropped).
        from app.scoring.course_types import ARC_CPR, AHA_BLS, ARC_BLS
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01"),
            ("22222", 12, 6.0, 70.0, "2025-04-01"),
            ("33333", 3, 3.0, 40.0, "2025-03-01"),
        )
        dm["33333"].buckets[ARC_CPR].students = 0
        dm["33333"].buckets[ARC_CPR].avg_students = 0.0
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-05-20")["charts"]
        pt = next(p for p in charts["points"] if p["zip"] == "33333")
        assert pt["arc_cpr_students"] == 0
        assert pt["normalized_arc_cpr_students_per_month"] == 0
        assert "33333" in {p["zip"] for p in charts["points"]}   # still plotted
        arc_cpr = next(s for s in charts["series"]
                       if s["key"] == "arc_cpr_avg_students_per_class")
        assert arc_cpr["hide_zero_y"] is True
        assert arc_cpr["hidden_zero_y"] == 1
        assert arc_cpr["n"] == 2

    def test_invalid_month_span_is_reported_not_silent(self):
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01", "2025-01-01"))
        dm["99999"] = ZipDemandProfile(
            zip="99999", total_classes=4, total_students=24,
            average_students_per_class=6.0, fill_rate=70.0,
            buckets={})
        charts = build_zip_demand_report(
            dm, {}, latest_export_date="2025-05-20")["charts"]
        assert charts["zips_plotted"] == 1
        assert charts["zips_excluded_no_valid_month_span"] == 1
        assert charts["zips_excluded"][0]["zip"] == "99999"

    def test_payload_is_json_safe(self, demand_map):
        import json
        rep = build_zip_demand_report(demand_map, {"95112": (37.3, -121.8)})
        json.loads(json.dumps(rep))


class TestZipDemandReportHtml:
    def _ctx(self):
        dm = _profile_map(
            ("11111", 30, 9.0, 90.0, "2025-05-01"),
            ("22222", 12, 6.0, 70.0, "2025-04-01"),
            ("33333", 3, 3.0, 40.0, "2025-03-01"),
        )
        report = build_zip_demand_report(
            dm, {"11111": (37.30, -121.80), "22222": (37.40, -121.90)},
            latest_export_date="2025-05-20")
        return {"zip_demand_report": report}

    def test_section_renders_table_chart_and_map(self):
        from app.reports.html_report import _zip_demand_report_section
        html = _zip_demand_report_section(self._ctx())
        assert "ZIP demand table" in html
        assert "<table" in html
        # all three ZIPs appear in the table
        assert "11111" in html and "22222" in html and "33333" in html
        # scatters + map are SVGs
        assert "reg-svg" in html
        assert "zip-map-svg" in html
        # regression read-out present
        assert "R²" in html
        # Both boss questions: ranking ("what score") + validation ("does it
        # line up with demand"), and all four required score->outcome charts.
        assert "ZIP score ranking" in html
        assert "Score vs actual demand" in html
        assert "ZIP demand score vs ARC CPR avg students/class" in html
        assert "ZIP demand score vs ARC BLS avg students/class" in html
        assert "ZIP demand score vs AHA BLS avg students/class" in html
        assert "ZIP demand score vs overall avg students/class" in html
        assert html.count("zip-chart-card") >= 4

    def test_missing_centroid_zip_listed_not_dropped(self):
        from app.reports.html_report import _zip_demand_report_section
        html = _zip_demand_report_section(self._ctx())
        # 33333 has no centroid -> still in the table, flagged missing,
        # and named in the missing list.
        assert "no centroid" in html
        assert "zip-missing" in html

    def test_empty_context_renders_nothing(self):
        from app.reports.html_report import _zip_demand_report_section
        assert _zip_demand_report_section({}) == ""
        assert _zip_demand_report_section({"zip_demand_report": {"rows": []}}) == ""
