# Wholesale Acquisition Engine — V1 Deal Analyzer

A deal-screening engine for real-estate wholesaling. You give it property data;
it tells you what the property can be bought for, how confident it is in that
answer, and what would have to be true for the deal to work.

**What this V1 does not do, on purpose:**

- No web scraping.
- No Zillow, MLS, county-record, or skip-tracing access.
- No invented comps, ARVs, owners, liens, mortgages, foreclosure status, or
  phone numbers.

Everything it analyzes comes from data **you** supply, by hand or by CSV. Where
data is missing, it says so under `MISSING DATA` rather than filling the gap
with a guess.

---

## 1. Project structure

```
wholesale_engine/
├── main.py                     CLI entry point
├── config.py                   every tunable assumption (fee, 70% rule, weights, thresholds)
├── requirements.txt            stdlib only; pytest optional
├── README.md
├── models/
│   ├── enums.py                Condition, Occupancy, PropertyType, SaleStatus, confidences…
│   ├── property.py             PropertyLead + Comp (the input models)
│   └── results.py              ARVAssessment, RepairEstimate, FinancialSummary, DealScore…
├── analysis/
│   ├── financials.py           pure deal math (MAO, spread, assignment, scenarios)
│   ├── comps.py                comp grading + comp-derived ARV
│   ├── valuation.py            reconciles your ARV against the comps
│   ├── repairs.py              low/mid/high rehab band
│   ├── scoring.py              0–100 score and classification bands
│   └── analyzer.py             orchestration, risk flags, missing data, final decision
├── data/
│   ├── csv_loader.py           CSV/JSON input parsing
│   ├── sources.py              V2 integration seams (all NotImplemented)
│   ├── sample_properties.csv   7 fictional leads
│   └── sample_comps.csv        14 fictional comps
└── reports/
    ├── text_report.py          the full human-readable report
    ├── csv_report.py           the flat CSV export
    └── output/                 generated files land here
tests/                          unit + end-to-end tests (109 of them)
```

The layering is strict, and it is the reason V2 integrations will be easy:

```
data (where facts come from) → models (what a fact looks like)
        → analysis (what the facts mean) → reports (how to say it)
```

Nothing in `analysis/` knows whether a lead was typed by hand or returned by an
API, so adding a data source never touches the underwriting rules.

---

## 2. Installation

Requires Python 3.9 or newer. Nothing else.

```bash
git clone <your-repo-url>
cd wholesale-code

python3 --version            # 3.9+
python3 -m wholesale_engine.main --sample
```

Optional, for the nicer test runner:

```bash
python3 -m pip install -r wholesale_engine/requirements.txt
```

---

## 3. How to run it

```bash
# Analyze the bundled fictional sample data
python3 -m wholesale_engine.main --sample

# Just the summary table
python3 -m wholesale_engine.main --sample --summary-only

# Your own leads, with a separate comps file
python3 -m wholesale_engine.main --csv my_leads.csv --comps my_comps.csv

# Write a CSV export and a full text report
python3 -m wholesale_engine.main --csv my_leads.csv --comps my_comps.csv \
    --out reports/output/deals.csv --report reports/output/deals.txt

# Try different underwriting assumptions
python3 -m wholesale_engine.main --sample --arv-pct 65 --fee 25000
```

| Flag | Purpose |
| --- | --- |
| `--csv` / `--comps` / `--json` | input files |
| `--sample` | run the bundled fictional data |
| `--out` | CSV export path |
| `--report` | write the full text reports to a file |
| `--detail` | add diagnostic columns to the CSV |
| `--summary-only` / `--quiet` | control console output |
| `--arv-pct` / `--fee` | override the formula (defaults: 70%, $18,000) |
| `--strict` | fail on a bad input row instead of skipping it |

### As a library

```python
from wholesale_engine import PropertyLead, analyze_property
from wholesale_engine.models import Comp, Condition, SaleStatus

lead = PropertyLead(
    address="412 Magnolia Ln", city="Springfield", state="MO",
    asking_price=82_000, sqft=1_450, beds=3, baths=2, year_built=1968,
    condition=Condition.MODERATE, user_repair_estimate=42_000, user_arv=215_000,
    comps=[Comp(address="528 Magnolia Ln", sale_price=222_000,
                sale_status=SaleStatus.CLOSED, sqft=1_490, distance_miles=0.3)],
)

result = analyze_property(lead)
print(result.decision, result.score.total, result.financials.recommended_offer)
```

---

## 4. Example input

`wholesale_engine/data/sample_properties.csv` — one row per lead:

```csv
property_id,address,city,state,county,zip_code,asking_price,beds,baths,sqft,lot_size_sqft,year_built,property_type,occupancy,condition,estimated_repairs,arv,days_on_market,estimated_monthly_rent,seller_motivation,distress_indicators,notes
WS-001,412 Magnolia Ln,Springfield,MO,Greene,65804,82000,3,2,1450,8100,1968,single family,vacant,moderate,42000,215000,74,1450,high,probate;deferred maintenance,"Heir lives out of state and wants a fast cash close."
```

`wholesale_engine/data/sample_comps.csv` — one row per comp, joined on `property_id`:

```csv
property_id,comp_address,sale_price,sale_status,sale_date,beds,baths,sqft,year_built,lot_size_sqft,distance_miles,property_type,condition,notes
WS-001,528 Magnolia Ln,222000,closed,2026-06-12,3,2,1490,1971,8400,0.3,single family,cosmetic,Renovated kitchen; same street
```

Every column except `address` is optional. Blank means unknown, and unknown is
reported rather than assumed. Multi-value cells (`distress_indicators`) split on
`;` or `|`. Comps can also ride inline in the properties file via a
`comps_json` column.

The seven sample leads deliberately cover the cases you will actually meet:

| ID | Situation | Result |
| --- | --- | --- |
| WS-001 | Discounted, well-comped probate lead | 🔥 GO |
| WS-002 | Retail-priced listing, seller not motivated | ❌ PASS (overpriced) |
| WS-003 | Driving-for-dollars lead with almost no data | 🟡 NEED MORE DATA |
| WS-004 | Seller's ARV inflated 40% over the comps | ❌ PASS (negative MAO) |
| WS-005 | Good-looking numbers, but zero comps | 🟡 NEED MORE DATA |
| WS-006 | Mobile home on leased land, priced above MAO | ❌ PASS |
| WS-007 | Solid deal priced above MAO | 🟠 NEGOTIATE |

---

## 5. Example output

```
==============================================================================
WHOLESALE DEAL ANALYSIS — WS-001
==============================================================================

PROPERTY
------------------------------------------------------------------------------
  Address:  412 Magnolia Ln
  City:     Springfield
  State:    MO
  County:   Greene

PROPERTY DETAILS
------------------------------------------------------------------------------
  Beds:           3
  Baths:          2
  Sq Ft:          1,450
  Year:           1968
  Occupancy:      VACANT
  Condition:      MODERATE

FINANCIALS
------------------------------------------------------------------------------
  Asking Price:              $82,000
  ARV:                       $215,000  [VERIFIED/SUPPORTED ARV]
      Your ARV of $215,000 is within 0.0% of the comp-derived $215,000.
        Underwriting at the more conservative $215,000. Basis: 3 reliable
        comp(s) at a quality-weighted $149/sqft x 1,450 sqft.
  Repair Estimate (used):    $42,000  [USER-PROVIDED (not a contractor quote)]
    Low / Mid / High:        $42,000 / $48,300 / $56,700
  70% of ARV:                $150,500
  Wholesale Fee:             $18,000
  MAO:                       $90,500
  Recommended Offer:         $82,000  (7% below MAO)
  Potential Assignment Price:$100,000
  Potential Gross Spread:    $8,500

  MAO BY REHAB SCENARIO
    Low rehab   repairs    $42,000  ->  MAO    $90,500   (vs asking: $8,500)
    Mid rehab   repairs    $48,300  ->  MAO    $84,200   (vs asking: $2,200)
    High rehab  repairs    $56,700  ->  MAO    $75,800   (vs asking: -$6,200)

COMPS
------------------------------------------------------------------------------
  Number of comps supplied:  4
  Comps used for valuation:  3
  Best comp:                 528 Magnolia Ln — $222,000 (CLOSED, quality 0.98, grade A)
  Worst comp:                900 Highland Ave — $165,000 (ACTIVE, quality 0.47, grade D)
  Comp confidence:           HIGH
  ARV confidence:            VERIFIED/SUPPORTED ARV

DEAL SCORE
------------------------------------------------------------------------------
  Score:          86.4 / 100
  Classification: 🟠 STRONG
  … component-by-component breakdown …

RISK FLAGS
------------------------------------------------------------------------------
  - [HIGH] This deal only works at the low end of the rehab range. At the high
      rehab scenario ($56,700) the MAO drops to $75,800, below the asking price.
  - [LOW] Built 1968: lead-paint disclosure applies …

MISSING DATA
------------------------------------------------------------------------------
  - A walkthrough or contractor bid — the rehab range here is inferred from …
  - Title, lien, mortgage payoff and foreclosure status — this engine has no
      access to public records and will never assert any of it.

FINAL DECISION
------------------------------------------------------------------------------
  🔥 GO

  Go. At $82,000 the seller is already asking $8,500 below the MAO of $90,500,
    the ARV basis is VERIFIED/SUPPORTED ARV, and the deal scores 86/100 …
```

Batch summary:

```
ADDRESS                           SCORE  CLASS       DECISION                 OFFER
------------------------------------------------------------------------------
412 Magnolia Ln                    86.4  🟠 STRONG    🔥 GO                   $82,000
1455 Willow Bend Dr                72.0  🟡 POSSIBLE  🟠 NEGOTIATE           $162,000
640 Prairie St                     67.9  🟡 POSSIBLE  🟡 NEED MORE DATA       $65,500
12 Sunset Trail Lot 12             66.7  🟡 POSSIBLE  ❌ PASS                 $16,000
88 Harborview Ct                   54.9  🔵 WEAK      ❌ PASS                $308,000
2210 Beechwood Ave                 39.0  ❌ PASS      ❌ PASS            NOT PROVIDED
17 Sycamore Rd                     11.0  ❌ PASS      🟡 NEED MORE DATA  NOT PROVIDED
```

And the CSV export (`--out`), one row per property:

```
address,city,state,asking_price,arv,repair_estimate,mao,recommended_offer,
assignment_price,potential_spread,deal_score,classification,arv_confidence,
comp_confidence,final_decision,risk_flags,missing_data
```

---

## 6. How the analysis works

### The formula

```
MAO = (ARV × 70%) − Repairs − $18,000 wholesale fee

Recommended Offer        = MAO − risk haircut, capped at the asking price
Potential Gross Spread   = MAO − Recommended Offer
Potential Assignment     = Recommended Offer + $18,000
```

The engine **never recommends paying full MAO**. The haircut is assembled from
named risks — unverified ARV, thin comps, condition-based rehab estimate,
occupancy, pre-1978 construction, narrow buyer pool — and every reason appears
in the report. It is floored at 3% and capped at 28%.

### ARV confidence

| Label | Meaning |
| --- | --- |
| `VERIFIED/SUPPORTED ARV` | 3+ reliable comps agree, and your number matches within 7% |
| `ESTIMATED ARV` | comps exist but are thin, or they conflict with your number |
| `USER-PROVIDED ARV` | your number, with nothing corroborating it |
| `INSUFFICIENT DATA` | no ARV and no usable comps — no MAO is produced at all |

Comps are graded 0–1 on seven weighted criteria in the required priority order:
closed status (0.22), square footage (0.16), property type (0.14), beds/baths
(0.14), proximity (0.14), age (0.10), recency (0.10). Anything below 0.55 is
excluded from the valuation but still shown, with the reason it was rejected.
When your ARV and the comps disagree, the engine flags the conflict and
underwrites the **lower** of the two.

### Repairs

A user-supplied figure is used and labelled `USER-PROVIDED`, with mid and high
scenarios at +15% and +35% for overruns, and a cross-check against what the
reported condition normally costs. If your number is well under that range, you
get a `repairs_understated` flag.

With no figure supplied, the band comes from condition alone (cosmetic
$8–22/sqft, moderate $20–45, heavy $40–80, teardown $70–130), multiplied for
pre-1978 and pre-1950 construction, with a 10% contingency on the high end.
**No repair number here is a contractor quote,** and the report says so every
time.

### Score and decision

Nine weighted components make up the 0–100 score: discount from ARV (18),
wholesale spread (16), comp quality (14), repair risk (12), seller motivation
(10), condition (8), marketability (8), equity potential (8), data confidence
(6). Bands: 90+ 🔥 HOT, 75+ 🟠 STRONG, 60+ 🟡 POSSIBLE, 40+ 🔵 WEAK, else ❌ PASS.

The score never overrides missing data. If the asking price, the ARV basis, or
the repair basis is missing or unverified, the deal is gated to
**⚠️ NEEDS MORE DATA** and the final decision is 🟡 NEED MORE DATA no matter how
good the arithmetic looks. A deal whose MAO is zero or negative is capped into
the PASS band, because no purchase price supports it.

### Safety rails

- Never states a deal is guaranteed profitable.
- Never invents comps, ARVs, owners, liens, mortgages, foreclosure status, or
  phone numbers.
- Flags conflicts: your ARV vs the comps, "highly motivated" sellers priced at
  retail, ARV per square foot above every reliable comp, square footage that
  does not fit the bed count.
- Flags overpricing explicitly, and separately flags deals that only work at the
  bottom of the rehab range.
- Repeats your distress indicators back as *your* unverified claims.

---

## 7. Tests

```bash
python3 -m unittest discover -s tests -v     # no dependencies
pytest tests -v                              # if pytest is installed
```

- `tests/test_financials.py` — the deal math: MAO, spread, assignment price,
  rounding, risk haircuts, scenarios, and the reverse formulas.
- `tests/test_comps_and_valuation.py` — comp grading and ARV reconciliation.
- `tests/test_repairs_and_scoring.py` — rehab bands, score components, bands.
- `tests/test_pipeline.py` — CSV in → analysis → CSV out, plus the safety rails.

---

## 8. Where the V2 modules plug in

Every future integration is a protocol already declared in
`wholesale_engine/data/sources.py`. They raise `NotImplementedError` today, and
implementing one requires **no change to the analysis layer**.

| V2 capability | Protocol | Where it attaches |
| --- | --- | --- |
| **Automated daily lead searches** | `LeadSource.fetch(criteria)` | Returns `List[PropertyLead]` — the same thing `load_properties_csv` returns. Wire it into `main.load_leads()` beside the CSV branch, then hand the leads to `analyze_properties()` unchanged. |
| **Property data APIs** (beds/baths/sqft/year) | `PropertyEnricher.enrich(lead)` | A pass over the leads in `analyzer.analyze_property()` *before* `analyze_comps()`. Filled fields simply stop showing up under MISSING DATA and lift the data-confidence score. |
| **County / public-record data** | `PropertyEnricher` | Same seam. This is what eventually replaces the standing "Title, lien, mortgage payoff and foreclosure status" gap — until then the engine asserts none of it. |
| **Comp data feeds** | `CompProvider.find_comps(lead, radius, months)` | Returns raw `Comp` objects appended to `lead.comps`. Grading stays in `analysis/comps.py`, so vendor data is held to the same reliability bar as hand-entered comps. |
| **Skip tracing** | `SkipTraceProvider.trace(lead)` | Runs **after** the analysis, gated on `result.decision in (GO, NEGOTIATE)` — you pay to trace deals, not leads. Regulated data (TCPA/DNC): needs consent tracking and suppression lists before it goes live. |
| **Google Sheets / CRM** | `ResultSink.publish(results)` | Beside `reports.write_csv()` in `main.run()`. `reports/csv_report.result_to_row()` already produces the flat dict a Sheets row or CRM record needs. |

The intended V2 flow:

```
LeadSource ──┐
             ├─► List[PropertyLead] ─► PropertyEnricher ─► CompProvider
CSV loader ──┘                                                  │
                                                                ▼
                                                  analysis.analyze_property()
                                                                │
                            SkipTraceProvider ◄─────────────────┤  (only on GO / NEGOTIATE)
                                                                ▼
                                              reports + ResultSink (Sheets / CRM)
```

Two rules keep this safe as it grows:

1. **New sources may add facts; they may never relax the rules.** A vendor ARV
   is still just a number until comps support it.
2. **Absent data stays absent.** No integration may substitute a default for a
   fact it does not have.

---

## 9. Disclaimer

This is a screening tool, not investment, legal, or appraisal advice. It works
only from the data you give it, and no deal it scores is guaranteed profitable.
Verify value, repair costs, title, liens, and possession independently before
you put a property under contract.
