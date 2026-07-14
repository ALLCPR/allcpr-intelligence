# ALLCPR Site Intelligence Limitations

ALLCPR Site Intelligence v1.0.0 is an internal decision-support product. It helps prioritize where to test demand and where to investigate commercial feasibility. It is not an automatic site-opening decision system.

## Modeled Scores

The modeled national score is a public-data estimate. It uses available public signals such as Census ACS demographics, Gazetteer density, and optional bulk public enrichment. It is not proven demand and should not be read as guaranteed enrollment.

## Historical Demand

Historical demand exists only where ALLCPR has operated and produced class/student records. ZIPs without ALLCPR history are modeled-only estimates. The system must not fabricate history for those ZIPs.

## Map Views

Smooth heat is centroid interpolation. It shows regional intensity, not exact ZIP boundary truth.

ZIP boundary shading depends on an available simplified ZCTA polygon file. If the file is missing, the dashboard falls back to ZIP points and smooth heat.

## Google Places

Google Places is context enrichment only. It is useful for finalist validation, nearby competitors, hospitals, colleges, schools, and real-world context.

Google Places is not called on dashboard page load and should not be used as the default national scoring engine.

## Commercial Validation

Commercial validation is required before any physical site decision. Rent, parking, classroom fit, lease availability, source, and updated date must be checked before treating an area as actionable.

## Forbidden Language

The system should never output:

```text
open now
lease-ready
```

Commercial validation can support language such as:

```text
Commercially promising - validate in person
```

It must not imply that a site can open immediately.
