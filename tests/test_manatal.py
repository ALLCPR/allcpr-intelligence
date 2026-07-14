"""Manatal ATS connector — safety + correctness.

Focus: off by default, dry-run when write is off, the API token never leaks into
any response/error, and Manatal stages map to internal CRM status + readiness.
No real network — a fake transport is injected.
"""
from __future__ import annotations

import pytest

from app.ops import manatal_sync, store
from app.ops.manatal_client import ManatalClient, status
from app.ops.manatal_sync import map_stage, readiness_from_stage

_TOKEN = "supersecret-manatal-token"


class FakeTransport:
    def __init__(self, responses):
        self.calls = []
        self.responses = list(responses)

    def __call__(self, method, url, headers, json, timeout):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "json": json})
        return self.responses.pop(0) if self.responses else (200, {"id": "x"})


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("MANATAL_ENABLED", "1")
    monkeypatch.setenv("MANATAL_API_BASE_URL", "https://api.manatal.com")
    monkeypatch.setenv("MANATAL_API_KEY", _TOKEN)
    monkeypatch.delenv("MANATAL_WRITE_ENABLED", raising=False)


# -- gating ----------------------------------------------------------------
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MANATAL_ENABLED", raising=False)
    res = ManatalClient(transport=lambda *a: (200, {})).create_candidate(
        {"full_name": "X"})
    assert res["ok"] is False and res["disabled"] is True


def test_enabled_but_not_configured(monkeypatch):
    monkeypatch.setenv("MANATAL_ENABLED", "1")
    for k in ("MANATAL_API_BASE_URL", "MANATAL_API_KEY", "MANATAL_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    res = ManatalClient(transport=lambda *a: (200, {})).create_candidate(
        {"full_name": "X"})
    assert res["error"] == "not_configured"


def test_write_off_is_dry_run_and_makes_no_call(live_env):
    called = []
    c = ManatalClient(transport=lambda *a: (called.append(a) or (200, {})))
    res = c.create_candidate({"full_name": "Jane"})
    assert res["dry_run"] is True
    assert res["would_send"] == {"full_name": "Jane"}
    assert called == []
    assert _TOKEN not in str(res)


def test_write_on_calls_transport_and_hides_token(live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    ft = FakeTransport([(201, {"id": "cand_9"})])
    res = ManatalClient(transport=ft).create_candidate({"full_name": "Jane"})
    assert res["ok"] is True and res["data"]["id"] == "cand_9"
    # Token IS sent in the auth header...
    assert ft.calls[0]["headers"]["Authorization"] == f"Token {_TOKEN}"
    # ...but never returned to the caller.
    assert _TOKEN not in str(res)


# -- secret safety ---------------------------------------------------------
def test_error_message_scrubs_token(live_env):
    def boom(*_a):
        raise RuntimeError(f"connection failed with key {_TOKEN}")
    res = ManatalClient(transport=boom, sleep=lambda *_: None).get_candidate("c1")
    assert res["ok"] is False
    assert _TOKEN not in str(res)
    assert "***" in res["message"]


def test_status_never_contains_token(live_env):
    s = status()
    assert _TOKEN not in str(s)
    assert s["configured"] is True and s["mode"] == "READ_ONLY"


# -- stage mapping ---------------------------------------------------------
def test_stage_mapping_and_readiness():
    assert map_stage("Contacted")["outreach_status"] == "CONTACTED"
    assert map_stage("Credential Verified") == {
        "outreach_status": "CREDENTIAL_VERIFIED", "credential_status": "VERIFIED"}
    assert map_stage("Ready to Teach")["outreach_status"] == "CONFIRMED"
    assert map_stage("credential_verified")["credential_status"] == "VERIFIED"
    assert map_stage("Rejected")["outreach_status"] == "REJECTED"
    assert map_stage("nonsense") == {}
    assert readiness_from_stage("Interested") == 65.0
    assert readiness_from_stage("Ready") == 100.0
    assert readiness_from_stage("Credential Verified") == 85.0
    assert readiness_from_stage("brand new") is None


def test_stage_keyword_fallback_for_standard_manatal_stages():
    # Manatal's default stage names (not in the exact table) still resolve.
    assert map_stage("Interview")["outreach_status"] == "INTERESTED"
    assert map_stage("Screening")["outreach_status"] == "CONTACTED"
    assert map_stage("Offer")["outreach_status"] == "AVAILABLE"
    assert map_stage("Hired")["outreach_status"] == "CONFIRMED"
    assert map_stage("Dropped")["outreach_status"] == "REJECTED"
    assert map_stage("Sourced")["outreach_status"] == "NEEDS_REVIEW"
    assert map_stage("Credential Verified")["credential_status"] == "VERIFIED"
    assert map_stage("totally custom name") == {}
    assert readiness_from_stage("Interview") == 65.0
    assert readiness_from_stage("Hired") == 100.0


def test_candidate_payload_has_no_secret():
    payload = manatal_sync.build_candidate_payload(
        {"name": "Jane Doe", "email": "j@x.com"}, zip_code="94541")
    assert payload["full_name"] == "Jane Doe"
    assert "authorization" not in str(payload).lower()
    assert _TOKEN not in str(payload)


# -- orchestration round-trip (with store) ---------------------------------
@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPS_DATA_DIR", tmp_path / "ops")
    monkeypatch.setattr(manatal_sync, "_ORG_CACHE", {})
    store.save_zip_candidates("INSTRUCTOR", "94541", [{
        "id": "inst_1", "name": "Jane Doe", "source": "live_scrape",
        "email": "j@x.com", "zip": "94541", "city": "Hayward",
        "state": "California", "outreach_status": "NEW",
        "credential_status": "NEEDS_VERIFICATION"}])
    return tmp_path


def test_push_lead_write_on_stores_candidate_id(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.setenv("MANATAL_DEFAULT_ORGANIZATION", "org_1")
    monkeypatch.setenv("MANATAL_DEFAULT_OWNER_ID", "u_1")
    # Job-first flow: create job, create candidate, add note, match.
    ft = FakeTransport([(201, {"id": "job_5"}), (201, {"id": "cand_77"}),
                        (201, {"id": "note_1"}), (201, {"id": "match_1"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] is True and res["manatal_candidate_id"] == "cand_77"
    assert res["manatal_job_id"] == "job_5" and res["job_reused"] is False
    lead = store.find_lead("INSTRUCTOR", "inst_1")
    assert lead["manatal_candidate_id"] == "cand_77"
    assert lead["manatal_job_id"] == "job_5"


def test_push_lead_dry_run_mutates_nothing(seeded, live_env):
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=lambda *a: (200, {})))
    assert res["dry_run"] is True and "note_preview" in res
    assert res["job_dry_run"] is True     # job creation previewed, not sent
    assert "manatal_candidate_id" not in store.find_lead("INSTRUCTOR", "inst_1")
    assert store.load_manatal_jobs()["jobs"] == {}


# -- instructor-position jobs (one per location, capped) --------------------
def test_push_opens_position_at_lead_location(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.setenv("MANATAL_DEFAULT_ORGANIZATION", "org_1")
    monkeypatch.setenv("MANATAL_DEFAULT_OWNER_ID", "u_1")
    ft = FakeTransport([(201, {"id": "job_9"}), (201, {"id": "cand_1"}),
                        (201, {"id": "note_1"}), (201, {"id": "match_1"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] and res["manatal_job_id"] == "job_9"
    job_call = ft.calls[0]
    assert job_call["url"].endswith("/open/v3/jobs/")
    sent = job_call["json"]
    assert sent["position_name"] == "AHA Instructor"
    assert sent["city"] == "Hayward" and sent["state"] == "California"
    assert sent["salary_min"] == "25" and sent["salary_max"] == "45"
    assert sent["currency"] == "USD" and sent["headcount"] == 2
    assert sent["organization"] == "org_1"
    assert "Jane Doe" in sent["description"]   # professor's name in the note
    # Registered for dedupe.
    key = store.manatal_job_location_key("Hayward", "California", "94541")
    assert store.find_manatal_job(key)["job_id"] == "job_9"


def test_second_push_same_location_reuses_position(seeded, live_env,
                                                   monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.setenv("MANATAL_DEFAULT_ORGANIZATION", "org_1")
    monkeypatch.setenv("MANATAL_DEFAULT_OWNER_ID", "u_1")
    key = store.manatal_job_location_key("Hayward", "California", "94541")
    store.record_manatal_job(key, {"job_id": "job_9",
                                   "position_name": "AHA Instructor"})
    # Reuse flow: note on the existing job, candidate, note, match — no
    # second job creation.
    ft = FakeTransport([(201, {"id": "jobnote_1"}), (201, {"id": "cand_2"}),
                        (201, {"id": "note_2"}), (201, {"id": "match_2"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] and res["manatal_job_id"] == "job_9"
    assert res["job_reused"] is True
    assert ft.calls[0]["url"].endswith("/open/v3/jobs/job_9/notes/")
    assert "Jane Doe" in ft.calls[0]["json"]["note"]
    assert not any(c["url"].endswith("/open/v3/jobs/") and c["method"] == "POST"
                   for c in ft.calls)


def test_job_daily_cap_blocks_new_position(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.setenv("MANATAL_DEFAULT_ORGANIZATION", "org_1")
    monkeypatch.setenv("MANATAL_DEFAULT_OWNER_ID", "u_1")
    monkeypatch.setenv("MANATAL_JOB_DAILY_CAP", "2")
    for i in range(2):
        store.record_manatal_job(f"other{i}|state", {"job_id": f"job_{i}"})
    ft = FakeTransport([])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] is False and res["error"] == "job_cap_reached"
    assert ft.calls == []      # nothing pushed — no orphan candidate either
    assert "manatal_candidate_id" not in store.find_lead("INSTRUCTOR", "inst_1")


def test_job_requires_organization_when_unresolvable(seeded, live_env,
                                                     monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.delenv("MANATAL_DEFAULT_ORGANIZATION", raising=False)
    monkeypatch.delenv("MANATAL_DEFAULT_OWNER_ID", raising=False)
    # No existing jobs to copy from + two client organizations → cannot
    # auto-pick; must fail with a real fix.
    ft = FakeTransport([(200, {"results": []}),
                        (200, {"results": [{"id": 1}, {"id": 2}]})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] is False and res["error"] == "organization_required"
    assert len(ft.calls) == 2  # jobs sample + orgs lookup, no job POST


def test_single_organization_is_auto_resolved(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.delenv("MANATAL_DEFAULT_ORGANIZATION", raising=False)
    monkeypatch.delenv("MANATAL_DEFAULT_OWNER_ID", raising=False)
    ft = FakeTransport([(200, {"results": []}),          # no jobs to copy
                        (200, {"results": [{"id": 42}]}),
                        (201, {"id": "job_1"}), (201, {"id": "cand_1"}),
                        (201, {"id": "note_1"}), (201, {"id": "match_1"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] is True
    assert ft.calls[2]["json"]["organization"] == "42"


def test_defaults_copied_from_existing_jobs(seeded, live_env, monkeypatch):
    # No env config at all: org AND owner are copied from the jobs the team
    # already created, so new pushes look exactly like manual ones.
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    monkeypatch.delenv("MANATAL_DEFAULT_ORGANIZATION", raising=False)
    monkeypatch.delenv("MANATAL_DEFAULT_OWNER_ID", raising=False)
    sample = {"results": [
        {"id": 1, "organization": 4085184, "owner": 1063340},
        {"id": 2, "organization": 4085184, "owner": 1063340}]}
    ft = FakeTransport([(200, sample),
                        (201, {"id": "job_1"}), (201, {"id": "cand_1"}),
                        (201, {"id": "note_1"}), (201, {"id": "match_1"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] is True
    job_sent = ft.calls[1]["json"]
    assert job_sent["organization"] == "4085184"
    assert job_sent["owner"] == "1063340"
    cand_sent = ft.calls[2]["json"]
    assert cand_sent["owner"] == "1063340"   # shows in owner-filtered views


def test_instructor_job_payload_matches_team_convention():
    p = manatal_sync.build_instructor_job_payload(
        {"id": "inst_1", "name": "Jane Doe", "city": "San Jose",
         "state": "California", "zip": "95112"}, zip_code="95112")
    assert p["position_name"] == "AHA Instructor"
    assert p["city"] == "San Jose" and p["state"] == "California"
    assert p["country"] == "United States" and p["zipcode"] == "95112"
    assert p["salary_min"] == "25" and p["salary_max"] == "45"
    assert p["currency"] == "USD" and p["headcount"] == 2
    assert p["status"] == "active"
    assert p["description"].startswith("Opened for instructor: Jane Doe")
    assert _TOKEN not in str(p)


def test_delete_candidate_clears_linked_lead(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    store.update_lead("INSTRUCTOR", "inst_1", {
        "manatal_candidate_id": "cand_77", "manatal_stage": "Sourced"})
    ft = FakeTransport([(204, "")])

    res = manatal_sync.delete_candidate(
        "cand_77", client=ManatalClient(transport=ft))

    assert res == {"ok": True, "candidate_id": "cand_77",
                   "lead_id": "inst_1", "deleted": True}
    assert ft.calls[0]["method"] == "DELETE"
    assert ft.calls[0]["url"].endswith("/open/v3/candidates/cand_77/")
    lead = store.find_lead("INSTRUCTOR", "inst_1")
    assert lead["manatal_candidate_id"] is None
    assert lead["manatal_stage"] is None


def test_delete_candidate_requires_local_link(live_env):
    res = manatal_sync.delete_candidate("unknown")
    assert res["ok"] is False
    assert res["error"] == "linked_lead_not_found"


def test_sync_candidate_updates_lead(seeded, live_env):
    store.update_lead("INSTRUCTOR", "inst_1",
                      {"manatal_candidate_id": "cand_77"})
    # get_candidate_matches returns a matches collection; stage is on the match.
    ft = FakeTransport([(200, {"results": [{"stage": {"name": "Interested"}}]})])
    res = manatal_sync.sync_candidate("cand_77", client=ManatalClient(transport=ft))
    assert res["manatal_stage"] == "Interested"
    lead = store.find_lead("INSTRUCTOR", "inst_1")
    assert lead["outreach_status"] == "INTERESTED"
    assert lead["manatal_stage"] == "Interested"
    assert ft.calls[0]["url"].endswith("/open/v3/candidates/cand_77/matches/")


# -- real Manatal Open API v3 shape ---------------------------------------
def test_candidate_payload_uses_real_manatal_fields():
    p = manatal_sync.build_candidate_payload(
        {"id": "inst_1", "name": "Jane Doe", "email": "j@x.com",
         "phone": "555", "organization": "SJSU", "title": "RN",
         "city": "San Jose", "state": "CA", "zip": "95112"}, zip_code="95112")
    assert p["full_name"] == "Jane Doe"
    assert p["current_company"] == "SJSU"      # not current_organization
    assert p["phone_number"] == "555"
    assert p["zipcode"] == "95112"
    assert p["external_id"] == "allcpr-inst_1"
    assert "first_name" not in p and "current_organization" not in p
    # source_type is validated against an account-specific choice list in
    # Manatal ("Other" 400s on real accounts) — must never be sent.
    assert "source_type" not in p and "source_other" not in p


def test_candidate_payload_omits_empty_optional_fields():
    # Manatal 400s on blank validated fields (e.g. email: "") — a lead with
    # no contact info must still produce a valid create payload.
    p = manatal_sync.build_candidate_payload(
        {"id": "inst_2", "name": "No Contact"}, zip_code="95112")
    assert p["full_name"] == "No Contact"
    for absent in ("email", "phone_number", "current_position",
                   "current_company", "address", "zipcode"):
        assert absent not in p
    assert p["description"]  # note always present


def test_extract_stage_from_matches_collection():
    from app.ops.manatal_sync import extract_stage
    assert extract_stage(
        {"results": [{"stage": {"name": "Interested"}}]}) == "Interested"
    assert extract_stage([{"recruitment_status": "Contacted"}]) == "Contacted"


def test_list_organizations_and_users_are_read_only(live_env):
    ft = FakeTransport([(200, {"results": [{"id": 7, "name": "USJEDU"}]}),
                        (200, {"results": [{"id": 3, "first_name": "Andy",
                                            "last_name": "Luo"}]})])
    c = ManatalClient(transport=ft)
    orgs = c.list_organizations()
    users = c.list_users()
    assert orgs["ok"] and users["ok"]
    assert ft.calls[0]["method"] == "GET"
    assert "/open/v3/organizations/" in ft.calls[0]["url"]
    assert ft.calls[1]["method"] == "GET"
    assert "/open/v3/users/" in ft.calls[1]["url"]
    assert _TOKEN not in str(orgs) + str(users)


def test_connection_probe_lists_one_candidate(live_env):
    ft = FakeTransport([(200, {"count": 42,
                               "results": [{"full_name": "Secret Person"}]})])
    res = ManatalClient(transport=ft).test_connection()
    assert res["ok"] is True and res["data"]["count"] == 42
    assert ft.calls[0]["url"].endswith("/open/v3/candidates/?limit=1")


def test_create_candidate_hits_open_v3_path(live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    ft = FakeTransport([(201, {"id": "c1"})])
    ManatalClient(transport=ft).create_candidate({"full_name": "X"})
    assert ft.calls[0]["url"] == "https://api.manatal.com/open/v3/candidates/"


def test_push_lead_attaches_to_job(seeded, live_env, monkeypatch):
    monkeypatch.setenv("MANATAL_WRITE_ENABLED", "1")
    ft = FakeTransport([(201, {"id": "cand_5"}), (201, {"id": "note_1"}),
                        (201, {"id": "match_1"})])
    res = manatal_sync.push_lead("inst_1", zip_code="94541", job_id="job_9",
                                 client=ManatalClient(transport=ft))
    assert res["ok"] and res["manatal_candidate_id"] == "cand_5"
    lead = store.find_lead("INSTRUCTOR", "inst_1")
    assert lead["manatal_job_id"] == "job_9"
    assert ft.calls[2]["url"].endswith("/open/v3/matches/")
    assert ft.calls[2]["json"] == {"candidate": "cand_5", "job": "job_9"}
