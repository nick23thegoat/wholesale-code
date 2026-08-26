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
python3 -m unittest discover -s tests              # the full suite
```

## The web dashboard

A read-only view of what the engine has already stored — leads, one property in
detail, run history with the rejection breakdown, and the buy box. It calls the
service layer and computes nothing of its own.

```bash
pip install -r wholesale_engine/requirements.txt   # Flask, only for this
python3 -m wholesale_engine.web                    # http://127.0.0.1:8000
```

Flask is the engine's only runtime dependency and only the dashboard needs it.
A server that just runs scheduled hunts needs nothing installed.

### It has no authentication. Do not expose it.

Anyone who can reach the port can read every lead and every owner name the
provider returned. `run_dev_server` binds `127.0.0.1` and **refuses** any other
host for that reason.

The intended deployment is a private Tailscale address, where the tailnet is
the perimeter rather than a password. Before this listens anywhere beyond
loopback, in this order:

1. an authentication layer in front of it,
2. a real WSGI server — the built-in one is for development only,
3. and only then a bind address other than `127.0.0.1`.

Every page carries the same warning in its footer, so it cannot be forgotten
by whoever is looking at it.

Full documentation — structure, formulas, CSV formats, lead scoring, filtering,
example output, and where the Wave 3 API and skip-tracing modules plug in — is in
[`wholesale_engine/README.md`](wholesale_engine/README.md).
