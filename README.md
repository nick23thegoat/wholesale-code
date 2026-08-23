# wholesale-code

Real-estate wholesaling tools.

## Wholesale Acquisition Engine

A Python lead-hunting and deal-screening engine for real-estate wholesaling.

**Wave 1 — deal analyzer.** Takes property and comp data you supply and decides
whether a property is a wholesale deal: derives an ARV from your comps, builds a
low/mid/high rehab band, computes MAO and a recommended offer below MAO, checks
the deal against your $18,000 target assignment fee, scores it 0–100, and
returns 🔥 GO / 🟠 NEGOTIATE / 🟡 NEED MORE DATA / ❌ PASS with every risk flag
and data gap spelled out. A deal that cannot produce the target fee at the
price on the table is never a GO.

**Wave 2 — lead hunter.** Takes a raw lead list, normalizes and de-duplicates
the addresses, scores each lead 0–100 on distress signals (absentee, vacant,
probate, pre-foreclosure, tax delinquent, tired landlord…), filters to your buy
box, then runs every survivor through the Wave 1 analyzer and ranks the results
into a call list.

The LEAD score (worth a call?) and the DEAL score (worth a contract?) are kept
separate and reported side by side — a 🔥 HOT lead can still be a ❌ PASS deal.

It has no access to Zillow, the MLS, county records, or skip-tracing databases,
and it never invents comps, ARVs, ownership, liens, or contact information.

```bash
python3 -m wholesale_engine.main --sample          # Wave 1: analyze the sample properties
python3 -m wholesale_engine.main --sample-leads    # Wave 2: hunt the sample lead list
python3 -m unittest discover -s tests              # 236 tests
```

Full documentation — structure, formulas, CSV formats, lead scoring, filtering,
example output, and where the Wave 3 API and skip-tracing modules plug in — is in
[`wholesale_engine/README.md`](wholesale_engine/README.md).
