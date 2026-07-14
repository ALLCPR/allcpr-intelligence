# v2.0 — Broadened 11-point Places enrichment

This release makes the **ALLCPR Site Intelligence Dashboard** more accurate for
site selection by capturing both **healthcare demand density** and the
**training / competitor ecosystem** more completely.

The previous methodology queried only **4 narrow POI categories** and
systematically under-counted (~2.2× healthcare, ~3.3× training). v2.0 expands
this to **11 Places queries** across three signal families and re-enriches all
**2,694 priority ZIPs**.

## Methodology: 4 categories → 11 queries

**Old (4):** Hospital · Urgent Care · Nursing Facility · CPR Competitor

**New (11) — 5 healthcare + 4 training + 2 competitor:**

| Family | Queries |
|---|---|
| Healthcare (5) | Hospital · Urgent Care · Clinic · Doctor Office · Nursing Facility |
| Training (4) | CPR Training Center · EMT School · Medical Assistant School · Nursing School |
| Competitor (2) | BLS / CPR Competitors · First Aid Certification *(+ related training providers)* |

These feed `healthcare_facility_density`, `training_school_density`,
`competition_gap_score`, and the per-category counts shown in ZIP detail.

## What's in this release

- **`national_demand_enriched.json`** — all 2,694 priority ZIPs refreshed with
  the broadened values.
- **Served artifacts regenerated** — `zip_details.jsonl` + `zip_details_index.json`
  (seekable ZIP-detail store) and `national_demand_lite.json[.gz]` (slim national
  map layer). Render serves ZIP detail from `zip_details.jsonl` via the index, not
  from the 77 MB enriched file, so the new values surface in production through the
  seek path.
- **Graceful abort on global Places failures** — `scripts/enrich_top_zips.py`
  checkpoints progress and stops cleanly on quota/billing errors (HTTP 429 / 403)
  instead of retrying per ZIP; `--resume` continues.
- **Persistent cache disk on Render** (`render.yaml`) — 2 GB disk mounted at
  `data/cache` so the response cache survives deploys/restarts (existing 30-day
  TTLs decide freshness instead of a full re-scrape on each deploy).
- **Version bump** — in-app `PRODUCT_VERSION` → `v2.0` (`/health`).

## Verification (live seek path)

Confirmed end-to-end via `GET /api/zip-demand/{zip}` — the production
`_load_zip_detail` byte-offset seek into `zip_details.jsonl`:

| ZIP | Healthcare density | Training density |
|-----|-------------------:|----------------:|
| 07030 | 68.9 | 56.8 |
| 10016 | 160.3 | 127.5 |
| 95112 | 12.0 | 8.3 |

- Enriched file: **2,694** ZIPs enriched ✓
- `zip_details.jsonl`: 33,772 records, **2,694** with `enrichment_tier=places` ✓
- `national_demand_lite.json.gz`: content byte-identical to the `.json` ✓
