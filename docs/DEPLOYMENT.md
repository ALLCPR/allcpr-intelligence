# ALLCPR Site Intelligence Deployment

Product: ALLCPR Site Intelligence  
Version: v1.0.0  
Status: Internal decision-support product

## Local Run

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Start the dashboard:

```bash
python3 -m uvicorn web_app:app --host 0.0.0.0 --port 8001
```

Open:

```text
http://127.0.0.1:8001
```

Verify:

```bash
python3 - <<'PY'
from urllib.request import urlopen
for path in ["/health", "/api/national-demand", "/api/zip-demand", "/api/model-backtest", "/"]:
    with urlopen("http://127.0.0.1:8001" + path, timeout=30) as r:
        print(path, r.status)
PY
```

## Render Deployment

Build command:

```bash
pip install -r requirements.txt
```

Start command:

```bash
uvicorn web_app:app --host 0.0.0.0 --port $PORT
```

Use `render.yaml` if deploying from the repository template.

## Required Environment Variables

No paid API key is required to load the dashboard, `/health`, or existing processed JSON files.

Recommended runtime variables:

```text
ENV=production
PORT=<provided by Render>
```

## Optional Environment Variables

These are optional and should stay blank unless a specific offline or operator-triggered enrichment command needs them:

```text
GOOGLE_MAPS_API_KEY=
GOOGLE_PLACES_API_KEY=
YELP_API_KEY=
FOURSQUARE_API_KEY=
ORS_API_KEY=
OPENROUTE_API_KEY=
MAPBOX_TOKEN=
CENSUS_API_KEY=
OPENAI_API_KEY=
GROQ_API_KEY=
```

## Safe Offline Commands

These do not call Google Places:

```bash
python3 scripts/build_national_demand.py
python3 scripts/qa_national_demand.py
python3 scripts/backtest_modeled_vs_historical.py
pytest -q
```

`build_national_demand.py` downloads public Census Gazetteer and ACS data when not cached. It does not call paid Places APIs.

## Commands That May Call Paid APIs

Treat these as operator-only:

```bash
python3 scripts/enrich_top_zips.py --use-places ...
```

Never run live Places for all national ZIPs. The committed enriched output is complete at 2,694 priority ZIPs: enriched validation is available for priority ZIPs, not every ZIP. Do not spend more API budget on low-priority ZIPs unless requested by state, customer, or search. For an intentional manual request, pass `--zips` or a selected ZIP file via `--zips-from`, and enforce `--max-api-calls`, `--refresh-days`, and cache behavior.

## Regenerate National Model

```bash
python3 scripts/build_national_demand.py
```

Output:

```text
data/processed/national_demand.json
```

## Regenerate QA

```bash
python3 scripts/qa_national_demand.py
```

Outputs:

```text
data/processed/national_demand_qa.json
data/reports/national_demand_qa.html
```

## Verify Health

```bash
python3 - <<'PY'
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8001/health", timeout=10).read().decode())
PY
```

## Avoid Full-US Places Enrichment

Use this rule: Places is context/validation only, not the default national scoring engine.

Before any live Places run:

1. Run `scripts/select_api_candidates.py`.
2. Select a small finalist file.
3. Run `scripts/enrich_top_zips.py --use-places --zips-from <file> --max-api-calls <budget> --refresh-days 30`.
4. Confirm cache hits/skips.
5. Run the backtest.
6. Keep Places display-only unless a later backtest shows meaningful improvement.
