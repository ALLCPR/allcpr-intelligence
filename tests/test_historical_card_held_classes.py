"""The candidate 'Historical ALLCPR performance' card must use the same
held-class basis as the benchmark/trend sections: no future classes, no
zero-enrollment placeholders, latest = latest completed held class."""
from __future__ import annotations

from app.collectors.enrollware import EnrollwareClassRecord, held_classes
from app.enrichers.historical_performance import build_candidate_historical_performance

# Far-past = always a completed month; far-future = always excluded, regardless
# of the real wall-clock date when the test runs.
PAST = ("2020-01", "2020-02", "2020-03")
FUTURE = "2099-08"


def _rec(month, enrolled, *, city="Testville", capacity=12, cancelled=None):
    return EnrollwareClassRecord(
        class_name="ARC BLS", course_type="arc_bls", course_type_label="ARC BLS",
        date=f"{month}-15", month=month, enrolled=enrolled, capacity=capacity,
        city=city, state="CA", cancelled=cancelled,
    )


def _card(records):
    return build_candidate_historical_performance(records, city="Testville", state="CA")


def test_card_latest_class_is_not_a_future_class():
    records = [
        _rec(PAST[0], 6), _rec(PAST[1], 7), _rec(PAST[2], 8),
        _rec(FUTURE, 5),   # future scheduled class
    ]
    card = _card(records)
    assert card["recent_activity"]["latest_class_date"] == "2020-03-15"
    assert FUTURE not in (card["recent_activity"]["latest_class_date"] or "")


def test_card_average_excludes_zero_enrollment_placeholders():
    records = [
        _rec(PAST[0], 6), _rec(PAST[1], 8), _rec(PAST[2], 7),
        _rec(PAST[0], 0, capacity=0),   # phantom placeholder
        _rec(PAST[1], 0, capacity=12),  # no-show / cancelled-but-unflagged
    ]
    card = _card(records)
    assert card["average_students_per_class"] == 7.0   # mean(6,8,7), zeros dropped
    assert card["total_classes"] == 3                  # held only


def test_card_uses_same_held_filter_as_benchmark_and_trend():
    records = [
        _rec(PAST[0], 6), _rec(PAST[1], 8), _rec(PAST[2], 7),
        _rec(FUTURE, 9),                # future
        _rec(PAST[0], 0, capacity=0),   # zero placeholder
    ]
    held_in_area = [r for r in held_classes(records) if r.city == "Testville"]
    card = _card(records)
    assert card["total_classes"] == len(held_in_area) == 3


def test_card_trend_not_faked_by_future_or_zero_classes():
    # Real completed history is flat/up (6 -> 7 -> 8); future + zero rows would
    # otherwise drag the later months down and fake a decline.
    records = [
        _rec(PAST[0], 6), _rec(PAST[0], 6),
        _rec(PAST[1], 7), _rec(PAST[1], 7),
        _rec(PAST[2], 8), _rec(PAST[2], 8),
        _rec(FUTURE, 0, capacity=0), _rec(FUTURE, 1),
    ]
    reasons = " ".join(_card(records)["reasons"]).lower()
    assert "trending down" not in reasons
    assert "trending up" in reasons or "roughly flat" in reasons


def test_ai_summary_context_drops_cleaned_unknown_course_type():
    from app.reports.ai_summary import _compact_context
    import json
    # Post-cleaning, the formerly-"unknown course type" bucket is relabeled and
    # the deterministic context handed to the LLM must not carry the old name.
    payload = {
        "context": {
            "course_performance": {
                "area_label": "San Jose, CA",
                "course_types": [
                    {"label": "ARC CPR", "average_students_per_class": 7.16},
                    {"label": "ALLCPR", "average_students_per_class": 6.4},
                ],
            }
        }
    }
    ctx = json.dumps(_compact_context(payload)).lower()
    assert "unknown course type" not in ctx
