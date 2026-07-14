"""
Flat CSV report for the scored candidates. One row per candidate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from app.utils.csv_utils import write_dicts_csv
from app.utils.logging_utils import get_logger
from app.utils.report_safety import strip_sensitive_query_params

logger = get_logger(__name__)


CSV_COLUMNS: List[str] = [
    "candidate_id", "city", "state", "latitude", "longitude",
    "mode", "comparison_area", "candidate_rank", "city_rank",
    "anchor_name", "anchor_address", "anchor_maps_url", "anchor_website",
    "anchor_phone", "anchor_rating", "anchor_reviews",
    "tier", "tier_label",
    "site_score",
    "demand_score", "healthcare_training_ecosystem_score",
    "competition_gap_score", "allcpr_opportunity_score",
    "economy_score", "accessibility_score",
    "historical_performance_score", "profitability_score", "confidence_score",
    "estimated_students_low", "estimated_students_mid", "estimated_students_high",
    "estimated_revenue_low", "estimated_revenue_mid", "estimated_revenue_high",
    "job_certification_demand_score", "job_demand_data_confidence",
    "job_active_postings_count", "job_certification_postings_count",
    "job_top_employers", "job_demand_notes",
    "rent_score", "rent_data_confidence", "rent_source", "rent_notes",
    "competitor_weakness_index",
    "website_analysis_checked_count", "competitor_online_booking_missing",
    "competitor_class_schedule_missing", "competitor_pricing_missing",
    "competitor_contact_friction_detected", "competitor_outdated_website_detected",
    "nearby_hospitals_5mi", "nearby_fire_stations_5mi", "nearby_ems_5mi",
    "nearby_healthcare_schools_5mi", "nearby_colleges_5mi",
    "nearby_childcare_centers_5mi", "nearby_senior_care_5mi",
    "nearby_clinics_5mi", "nearby_cpr_competitors_5mi",
    "top_demand_drivers", "top_competitors",
    "go_to_market_angles",
    "risks", "recommendation_summary",
    "source_urls", "collected_at",
]


def _unknown_if_none(value: Any) -> Any:
    return "unknown" if value is None else value


def candidate_to_row(profile: Dict[str, Any],
                     scored: Dict[str, Any]) -> Dict[str, Any]:
    counts_5mi: Dict[str, int] = profile.get("counts_5mi") or {}
    comp = profile.get("competition_summary") or {}
    comp_buckets = comp.get("competitor_count_by_bucket_mi") or {}

    anchor = profile.get("anchor") or {}

    top_drivers = profile.get("top_demand_drivers") or []
    top_drivers_str = "; ".join(f"{k}={v}" for k, v in top_drivers)

    competitors_sample = profile.get("competitors_sample") or []
    top_comp_str = "; ".join(
        f"{c.get('name', '')} (★{c.get('rating', '?')}, "
        f"{c.get('user_ratings_total', 0)} reviews)"
        for c in competitors_sample[:3]
    )

    healthcare_schools = sum(counts_5mi.get(k, 0) for k in (
        "nursing_school", "medical_school", "dental_school", "healthcare_training",
        "emt_training", "cna_training",
    ))
    colleges = sum(counts_5mi.get(k, 0) for k in ("community_college", "university"))
    clinics = sum(counts_5mi.get(k, 0) for k in (
        "medical_clinic", "urgent_care", "physical_therapy", "dental_clinic",
    ))

    opp = scored.get("opportunity_breakdown") or {}
    prof = scored.get("profitability_estimate") or {}
    job = scored.get("job_demand") or {}
    sub = scored["sub_scores"]

    return {
        "candidate_id": profile.get("candidate_id"),
        "city": profile.get("city"),
        "state": profile.get("state"),
        "latitude": profile.get("latitude"),
        "longitude": profile.get("longitude"),
        "mode": profile.get("mode", "unknown"),
        "comparison_area": profile.get("comparison_area") or profile.get("city"),
        "candidate_rank": profile.get("candidate_rank", "unknown"),
        "city_rank": profile.get("city_rank", "unknown"),

        "anchor_name": _unknown_if_none(anchor.get("name")),
        "anchor_address": _unknown_if_none(anchor.get("formatted_address")),
        "anchor_maps_url": _unknown_if_none(
            strip_sensitive_query_params(anchor.get("google_maps_url") or "") or None
        ),
        "anchor_website": _unknown_if_none(
            strip_sensitive_query_params(anchor.get("website") or "") or None
        ),
        "anchor_phone": _unknown_if_none(anchor.get("phone_number") or None),
        "anchor_rating": _unknown_if_none(anchor.get("rating")),
        "anchor_reviews": _unknown_if_none(anchor.get("user_ratings_total")),

        "tier": scored.get("tier"),
        "tier_label": scored.get("tier_label"),

        "site_score": scored.get("site_score"),
        "demand_score": sub.get("demand_score"),
        "healthcare_training_ecosystem_score":
            sub.get("healthcare_training_ecosystem_score"),
        "competition_gap_score": sub.get("competition_gap_score"),
        "allcpr_opportunity_score": sub.get("allcpr_opportunity_score"),
        "economy_score": sub.get("economy_score"),
        "accessibility_score": sub.get("accessibility_score"),
        "historical_performance_score": sub.get("historical_performance_score"),
        "profitability_score": sub.get("profitability_score"),
        "confidence_score": sub.get("confidence_score"),

        "estimated_students_low": prof.get("students_low"),
        "estimated_students_mid": prof.get("students_mid"),
        "estimated_students_high": prof.get("students_high"),
        "estimated_revenue_low": prof.get("revenue_low"),
        "estimated_revenue_mid": prof.get("revenue_mid"),
        "estimated_revenue_high": prof.get("revenue_high"),

        "job_certification_demand_score":
            _unknown_if_none(job.get("job_certification_demand_score")),
        "job_demand_data_confidence":
            _unknown_if_none(job.get("job_demand_data_confidence")),
        "job_active_postings_count":
            _unknown_if_none(job.get("active_postings_count")),
        "job_certification_postings_count":
            _unknown_if_none(job.get("certification_postings_count")),
        "job_top_employers": "; ".join(
            f"{e.get('employer')} ({e.get('posting_count')})"
            for e in (job.get("top_employers") or [])[:5]
        ) or "unknown",
        "job_demand_notes": _unknown_if_none(job.get("notes")),

        "rent_score": _unknown_if_none((scored.get("rent") or {}).get("rent_score")),
        "rent_data_confidence": _unknown_if_none(
            (scored.get("rent") or {}).get("rent_data_confidence")
        ),
        "rent_source": _unknown_if_none(strip_sensitive_query_params(
            (scored.get("rent") or {}).get("rent_source") or ""
        ) or None),
        "rent_notes": _unknown_if_none((scored.get("rent") or {}).get("rent_notes")),

        "competitor_weakness_index": opp.get("weakness_index"),
        "website_analysis_checked_count": comp.get("website_analysis_checked_count", 0),
        "competitor_online_booking_missing":
            comp.get("competitor_online_booking_missing", 0),
        "competitor_class_schedule_missing":
            comp.get("competitor_class_schedule_missing", 0),
        "competitor_pricing_missing": comp.get("competitor_pricing_missing", 0),
        "competitor_contact_friction_detected":
            comp.get("competitor_contact_friction_detected", 0),
        "competitor_outdated_website_detected":
            comp.get("competitor_outdated_website_detected", 0),


        "nearby_hospitals_5mi": counts_5mi.get("hospital", 0),
        "nearby_fire_stations_5mi": counts_5mi.get("fire_station", 0),
        "nearby_ems_5mi": counts_5mi.get("ems", 0),
        "nearby_healthcare_schools_5mi": healthcare_schools,
        "nearby_colleges_5mi": colleges,
        "nearby_childcare_centers_5mi": counts_5mi.get("childcare_center", 0),
        "nearby_senior_care_5mi": counts_5mi.get("senior_care", 0),
        "nearby_clinics_5mi": clinics,
        "nearby_cpr_competitors_5mi": comp_buckets.get(5, 0),

        "top_demand_drivers": top_drivers_str or "none",
        "top_competitors": top_comp_str or "none",
        "go_to_market_angles": "; ".join(opp.get("angles") or []) or "none",
        "risks": "; ".join(scored.get("risks") or []) or "none",
        "recommendation_summary": "; ".join(
            (scored.get("rationale") or [])[:3]
        ),
        "source_urls": " | ".join(
            strip_sensitive_query_params(u) for u in (profile.get("source_urls") or [])
        ),
        "collected_at": profile.get("collected_at"),
    }


def write_csv_report(rows: List[Dict[str, Any]], path: Path) -> None:
    write_dicts_csv(rows, path, fieldnames=CSV_COLUMNS)
