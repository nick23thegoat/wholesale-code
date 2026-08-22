# wholesale-code

Real-estate wholesaling tools.

## Wholesale Acquisition Engine — V1 Deal Analyzer

A Python deal-screening engine that takes property and comp data you supply and
decides whether a lead looks like a wholesale deal: it derives an ARV from your
comps, builds a low/mid/high rehab band, computes MAO and a recommended offer
below MAO, scores the deal 0–100, and returns one of 🔥 GO / 🟠 NEGOTIATE /
🟡 NEED MORE DATA / ❌ PASS with every risk flag and data gap spelled out.

It has no access to Zillow, the MLS, county records, or skip-tracing databases,
and it never invents comps, ARVs, ownership, liens, or contact information.

```bash
python3 -m wholesale_engine.main --sample          # analyze the bundled fictional leads
python3 -m unittest discover -s tests              # run the test suite
```

Full documentation — structure, formulas, CSV formats, example output, and where
the V2 lead-hunting and skip-tracing modules plug in — is in
[`wholesale_engine/README.md`](wholesale_engine/README.md).
