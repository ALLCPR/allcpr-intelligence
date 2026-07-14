# ALLCPR Site Intelligence Runbook

Product: ALLCPR Site Intelligence v1.0.0  
Status: Internal decision-support product

## Start Dashboard Locally

```bash
python3 -m uvicorn web_app:app --host 0.0.0.0 --port 8001
```

Open:

```text
http://127.0.0.1:8001
```

## Rebuild Public-Data National Model

```bash
python3 scripts/build_national_demand.py
```

Expected output:

```text
data/processed/national_demand.json
```

This uses Census Gazetteer and ACS public data. It does not call Google Places.

## Regenerate QA

```bash
python3 scripts/qa_national_demand.py
```

Expected outputs:

```text
data/processed/national_demand_qa.json
data/reports/national_demand_qa.html
```

## Run Tests

```bash
pytest -q tests/test_web_app.py
pytest -q tests/test_qa_national_demand.py
pytest -q
```

## Check Output Paths

```bash
ls -lh \
  data/processed/national_demand.json \
  data/processed/national_demand_qa.json \
  data/reports/national_demand_qa.html
```

## Troubleshoot Stale Server Process

Check port 8001:

```bash
lsof -nP -iTCP:8001 -sTCP:LISTEN
```

If needed, stop the old process:

```bash
kill <PID>
```

Restart:

```bash
python3 -m uvicorn web_app:app --host 0.0.0.0 --port 8001
```

## Confirm No Places Calls Happened

Safe commands for dashboard/model/QA:

```bash
python3 scripts/build_national_demand.py
python3 scripts/qa_national_demand.py
python3 scripts/backtest_modeled_vs_historical.py
pytest -q
```

These should not instantiate the Google Places client or require `GOOGLE_MAPS_API_KEY`.

If using Places intentionally, the command should be explicit and budgeted:

```bash
python3 scripts/enrich_top_zips.py \
  --use-places \
  --zips-from data/processed/api_candidates_bay_area_100.json \
  --max-api-calls 400 \
  --refresh-days 30
```

Do not run Places against all national ZIPs. Treat the enrichment run as
complete at 2,694 enriched priority ZIPs; do not force-enrich the remaining
low-priority ZIPs unless requested by state, customer, or search. Manual
force-enrichment remains available with `--zips` or `--zips-from`.

## Verify Endpoints

```bash
python3 - <<'PY'
from urllib.request import urlopen
for path in ["/health", "/api/national-demand", "/api/zip-demand", "/api/model-backtest", "/"]:
    with urlopen("http://127.0.0.1:8001" + path, timeout=30) as r:
        print(path, r.status)
PY
```
