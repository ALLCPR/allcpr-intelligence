"""Action Queue aggregation — grouped daily tasks from the engine outputs."""
from __future__ import annotations

import pytest

from app.ops import action_queue, store


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")


def _match(action, level=0, label="", expl=""):
    return {"best_instructor_path": [{"lead_level": level, "name": "X"}],
            "recommended_action": action,
            "recommended_action_label": label or action,
            "explanation": expl}


def _cov(decision, label="", expl=""):
    return {"coverage_decision": decision,
            "coverage_decision_label": label or decision,
            "explanation": expl,
            "cannibalization_risk": decision == "CANNIBALIZATION_RISK"}


def _q(**over):
    base = dict(
        match_fn=lambda z: _match("SOURCE_INSTRUCTORS"),
        coverage_fn=lambda z: _cov("OPEN_TEST_OK"),
        space_leads_fn=lambda z: [],
        revenue_summary_fn=lambda: {"at_risk_sites": []},
        missing_data_fn=lambda: [],
        manatal_mode="DISABLED",
    )
    zips = over.pop("zips", ["94541"])
    base.update(over)
    return action_queue.build_action_queue(zips, **base)


def test_instructor_recruiting_and_space_tasks():
    q = _q(match_fn=lambda z: _match("CONTACT_PAST_INSTRUCTOR", level=3),
           space_leads_fn=lambda z: [])
    assert q["counts"]["Instructor Recruiting Needed"] == 1
    assert q["counts"]["Space Outreach Needed"] == 1


def test_test_class_ready_when_instructor_and_space_ready():
    q = _q(match_fn=lambda z: _match("ADVANCE_IN_MANATAL", level=5),
           space_leads_fn=lambda z: [{"name": "Room A",
                                      "outreach_status": "AVAILABLE"}])
    assert q["counts"]["Test Class Ready"] == 1
    assert q["counts"]["Space Outreach Needed"] == 0


def test_fix_current_first_is_p0_and_blocks_test_class():
    q = _q(match_fn=lambda z: _match("ADVANCE_IN_MANATAL", level=5),
           coverage_fn=lambda z: _cov("FIX_CURRENT_FIRST"),
           space_leads_fn=lambda z: [{"name": "R", "outreach_status": "AVAILABLE"}])
    conflict = q["groups"]["Existing Site Coverage Conflict"]
    assert len(conflict) == 1 and conflict[0]["priority"] == "P0"
    assert q["counts"]["Test Class Ready"] == 0   # coverage blocks it


def test_portfolio_revenue_and_missing_data():
    q = _q(zips=[],
           revenue_summary_fn=lambda: {"at_risk_sites": ["Newark", "Hayward"]},
           missing_data_fn=lambda: [{"ref": "Course economics", "priority": "P2",
                                     "reason": "x", "next_action": "y",
                                     "owner": "z", "due_date": None, "link": ""}])
    assert q["counts"]["Revenue Leakage / Critical Site Review"] == 2
    assert q["counts"]["Missing Manual Data"] == 1


def test_manatal_sync_task_when_lead_linked():
    store.save_zip_candidates("INSTRUCTOR", "94541", [{
        "id": "i1", "name": "Jane", "zip": "94541", "source": "live_scrape",
        "manatal_candidate_id": "cand_1", "outreach_status": "CONTACTED"}])
    q = _q(match_fn=lambda z: _match("ADVANCE_IN_MANATAL", level=4),
           space_leads_fn=lambda z: [{"name": "R", "outreach_status": "AVAILABLE"}],
           manatal_mode="READ_ONLY")
    assert q["counts"]["Manatal Sync Needed"] == 1


def test_manatal_sync_skipped_when_disabled():
    store.save_zip_candidates("INSTRUCTOR", "94541", [{
        "id": "i1", "name": "Jane", "zip": "94541", "source": "live_scrape",
        "manatal_candidate_id": "cand_1", "outreach_status": "CONTACTED"}])
    q = _q(manatal_mode="DISABLED")
    assert q["counts"]["Manatal Sync Needed"] == 0
