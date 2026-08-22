# Wholesale Acquisition Engine

A lead-hunting and deal-screening engine for real-estate wholesaling.

* **Wave 1 — deal analyzer.** You give it a property; it tells you what the
  property can be bought for, how confident it is in that answer, and what
  would have to be true for the deal to work.
* **Wave 2 — lead hunter.** You give it a raw lead list; it normalizes and
  de-duplicates the rows, scores each lead on distress signals, filters to your
  buy box, then runs every survivor through the Wave 1 analyzer and ranks the
  results.

The two scores answer different questions and are never conflated:

| | Question | Where |
| --- | --- | --- |
| **LEAD score** | Is this seller worth calling? | `lead_hunter/scoring.py` |
| **DEAL score** | Is this property worth buying at this price? | `analysis/scoring.py` |

A 🔥 HOT lead can still be a ❌ PASS deal. That is a normal result, and the
output shows both side by side so it is never hidden.

**What this engine does not do, on purpose:**

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
├── main.py                     CLI entry point (Wave 1 + Wave 2)
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
├── lead_hunter/                WAVE 2
│   ├── models.py               Lead, LeadScore, LeadResult, pipeline report
│   ├── normalizer.py           address normalization + duplicate detection
│   ├── scoring.py              the 0-100 LEAD score
│   ├── filters.py              buy-box filters + missing-data tracking
│   ├── pipeline.py             source → … → Wave 1 analyzer → prioritize
│   ├── skip_trace.py           skip-tracing seam (not implemented)
│   └── sources/
│       ├── base.py             BaseLeadSource: search_leads/get_property/…
│       ├── csv_source.py       CSV source with column aliases
│       └── api_source_template.py   template only — nothing connected
├── data/
│   ├── csv_loader.py           CSV/JSON input parsing
│   ├── sources.py              integration protocols (all NotImplemented)
│   ├── sample_properties.csv   7 fictional properties
│   ├── sample_comps.csv        14 fictional comps
│   └── lead_sources/
│       ├── sample_leads.csv        25 fictional leads
│       └── sample_lead_comps.csv   12 fictional comps for 4 of them
└── reports/
    ├── text_report.py          the full human-readable report
    ├── csv_report.py           the Wave 1 flat CSV export
    ├── lead_report.py          lead_pipeline.csv + hot_leads.csv
    └── output/                 generated files land here
tests/                          200 unit + end-to-end tests
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

### Wave 2 — hunting through a raw lead list

```bash
# The bundled fictional lead list (25 leads, comps included)
python3 -m wholesale_engine.main --sample-leads

# Your own list, with the full buy box applied
python3 -m wholesale_engine.main \
    --leads wholesale_engine/data/lead_sources/sample_leads.csv \
    --states FL,TX,MO \
    --min-lead-score 60 \
    --min-deal-score 60

# Attach comps so the ARV can actually be verified
python3 -m wholesale_engine.main --leads my_leads.csv --lead-comps my_lead_comps.csv

# Just the call list
python3 -m wholesale_engine.main --sample-leads --hot-only
```

| Flag | Purpose |
| --- | --- |
| `--leads` | raw lead-list CSV to hunt through |
| `--lead-comps` | optional comps CSV joined by lead_id / property_id / address |
| `--sample-leads` | run the bundled fictional lead list |
| `--states FL,TX,MO` | target markets (default: FL, TX, MO) |
| `--property-types` | target types (default: single_family, duplex, triplex, fourplex) |
| `--min-lead-score` | drop leads below this LEAD score |
| `--min-deal-score` | drop leads below this DEAL score (applied after analysis) |
| `--max-asking-price` / `--min-equity` | price and equity filters |
| `--hot-only` | report only 🔥 HOT and 🟠 STRONG leads |
| `--lead-out` / `--hot-out` | output paths |

Every Wave 1 flag still works exactly as before; the two modes share the same
`--arv-pct`, `--fee` and `--quiet` options.

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

```python
# Wave 2: a whole list at once
from wholesale_engine.lead_hunter import hot_leads, run_from_csv

report = run_from_csv("my_leads.csv", comps_path="my_lead_comps.csv")
for result in hot_leads(report):
    print(result.lead.address, result.score.total, result.deal_score, result.analysis.decision)
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

### Wave 2 lead-list format

Lead lists arrive with different headers every time, so **column names are
forgiving**. `wholesale_engine/data/lead_sources/sample_leads.csv` deliberately
uses non-canonical names to prove it:

```csv
lead_id,property_address,city,state,county,zip,owner,list_price,market_value,repair_estimate,br,ba,building_sqft,yr_built,land_use,occupancy,property_condition,absentee,vacant_property,back_taxes,notice_of_default,foreclosure,probate,heir,code_enforcement,equity_flag,landlord,dom,motivation,lead_source,remarks
LH-001,412 Magnolia Lane,Springfield,MO,Greene,65804,FICTIONAL OWNER 01,82000,215000,42000,3,2,1450,1968,single family,vacant,moderate,yes,yes,no,no,no,yes,yes,no,yes,no,74,high,fictional-test-list,FICTIONAL TEST DATA...
```

Accepted aliases (first match wins, case and punctuation insensitive):

| Field | Aliases |
| --- | --- |
| `address` | address, property_address, street_address, site_address, situs_address |
| `asking_price` | asking_price, list_price, price, listing_price, ask |
| `estimated_value` | estimated_value, market_value, arv, est_value, avm, value |
| `estimated_repairs` | estimated_repairs, repairs, repair_estimate, rehab, rehab_estimate |
| `owner_name` | owner_name, owner, owner_1, ownername |
| `absentee_owner` | absentee_owner, absentee, out_of_state_owner, non_owner_occupied |
| `vacant` | vacant, vacant_property, is_vacant, vacancy |
| `pre_foreclosure` | pre_foreclosure, notice_of_default, nod, lis_pendens |
| `tax_delinquent` | tax_delinquent, delinquent_taxes, tax_default, back_taxes |
| `sqft` | sqft, square_feet, building_sqft, living_area, sq_ft |

The full table is `COLUMN_ALIASES` and `SIGNAL_ALIASES` in
`lead_hunter/sources/csv_source.py`. Only `address` really matters; everything
else is optional.

**Signal columns are three-valued.** `yes/y/true/1` is True, `no/n/false/0` is
False, and blank / `unknown` / `n/a` stays **unknown**. Blank is never read as
"no" — that would invent a fact about the property. Unknown signals score
nothing and appear under NEEDS VERIFICATION.

Comps for leads are optional and join by `lead_id`, `property_id` or address:

```csv
lead_id,comp_address,sale_price,sale_status,sale_date,beds,baths,sqft,year_built,distance_miles,property_type,condition,notes
LH-011,81 Sabal Palm Way,264000,closed,2026-07-06,3,2,1530,1960,0.1,single family,cosmetic,FICTIONAL TEST DATA. Same street.
```

The 25 sample leads cover absentee owners, vacants, probate, inherited,
pre-foreclosure, tax delinquency, high equity, tired landlords, heavy rehabs, an
overpriced listing, an excellent deal, a bad deal, a lead with almost no data,
two duplicate rows in different formats, two units of one duplex that must
*not* merge, a commercial building, raw land, and five states. Every row is
labelled FICTIONAL TEST DATA.

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

### Wave 2 — the lead pipeline

```
====================================================================================================
LEAD PIPELINE — 25 row(s) read from csv:sample_leads.csv, 23 unique properties
====================================================================================================
ADDRESS                       ST    LEAD CLASS        DEAL DECISION                 OFFER     SPREAD
----------------------------------------------------------------------------------------------------
77 Sabal Palm Way             FL    80.0 🟠 STRONG     83.5 🔥 GO                  $109,000    $13,500
145 Cedar Hollow Lane         MO    90.0 🔥 HOT        79.0 🟠 NEGOTIATE            $59,500     $7,500
905 Pecan Street              TX    95.0 🔥 HOT        78.6 🟠 NEGOTIATE           $118,000    $14,800
4501 Live Oak Circle          TX    52.0 🔵 WEAK       76.0 🟠 NEGOTIATE            $66,500     $5,100
412 Magnolia Lane             MO    70.0 🟡 POSSIBLE   72.4 🟡 NEED MORE DATA       $69,500    $21,000
66 Camellia Court             FL    90.0 🔥 HOT        62.6 🟡 NEED MORE DATA       $33,000    $12,400
...
2210 Beechwood Avenue         OH    95.0 🔥 HOT         —   filtered out                 —          —
17 Sycamore Road              OH     0.0 ❌ PASS        —   filtered out                 —          —
----------------------------------------------------------------------------------------------------
16 analyzed · 7 filtered out · 2 duplicate row(s) merged · 5 hot/strong lead(s)
LEAD score = worth a call. DEAL score = worth a contract. They are not the same test,
and a hot lead can still be a bad deal.
====================================================================================================
```

Read the first and last rows together and you have the whole argument for
keeping the two scores apart:

* **4501 Live Oak Circle** is a 🔵 WEAK lead (52) with a 76 deal — few distress
  signals, but the numbers work. You would never have called it off a lead
  score alone.
* **2210 Beechwood Avenue** is a 🔥 HOT lead (95) that never gets analyzed at
  all, because Ohio is outside the target markets.
* **66 Camellia Court** is a 🔥 HOT lead (90) whose deal comes back at 62.6 and
  NEED MORE DATA. Hot to call, not yet a deal.

Two files are written:

`reports/output/lead_pipeline.csv` — every unique lead, ranked, including the
ones that were filtered out (with the reason in `filter_reasons`, and a blank
deal side rather than a misleading zero):

```
lead_id,property_id,address,city,state,county,zip_code,owner_name,asking_price,
estimated_value,estimated_repairs,lead_score,lead_classification,deal_score,
deal_classification,mao,recommended_offer,potential_assignment_price,
potential_spread,final_decision,lead_source,arv_confidence,comp_confidence,
risk_flags,missing_data
  + pipeline_status, lead_signals, unconfirmed_signals, filter_reasons,
    needs_verification, arv_status, seventy_percent_arv, wholesale_fee,
    estimated_equity, equity_is_derived, merged_duplicates, property_type,
    occupancy, condition, decision_explanation
```

`reports/output/hot_leads.csv` — only 🔥 HOT and 🟠 STRONG leads, sorted by
deal score, then lead score, then potential spread:

```csv
lead_id,address,city,state,asking_price,estimated_value,lead_score,lead_classification,deal_score,mao,recommended_offer,potential_spread,final_decision,arv_confidence
LH-011,77 Sabal Palm Way,Jacksonville,FL,119000.0,265000.0,80.0,🟠 STRONG,83.5,122500.0,109000.0,13500.0,🔥 GO,VERIFIED/SUPPORTED ARV
LH-009,905 Pecan Street,Houston,TX,148000.0,295000.0,95.0,🔥 HOT,78.6,132800.0,118000.0,14800.0,🟠 NEGOTIATE,VERIFIED/SUPPORTED ARV
```

### The Wave 1 CSV export

And the CSV export (`--out`), one row per property:

```
address,city,state,asking_price,arv,repair_estimate,mao,recommended_offer,
assignment_price,potential_spread,deal_score,classification,arv_confidence,
comp_confidence,final_decision,risk_flags,missing_data
```

---

## 6. How the lead hunter works (Wave 2)

```
LEAD SOURCE -> NORMALIZE -> DEDUPLICATE -> LEAD SCORE -> LEAD FILTER
    -> convert to PropertyLead -> WAVE 1 ANALYZER -> DEAL SCORE
    -> PRIORITIZE -> hot_leads.csv + lead_pipeline.csv
```

There is exactly **one** MAO calculator in this codebase. Everything from ARV
through repairs, MAO, recommended offer, assignment price and the final
decision is the Wave 1 analyzer called unchanged
(`lead_hunter/pipeline.py` → `analysis.analyzer.analyze_property`).

### Normalization and duplicates

Street types fold to USPS abbreviations (Street→ST, Avenue→AVE, Road→RD,
Drive→DR, Lane→LN, Court→CT, Circle→CIR, Highway→HWY, Boulevard→BLVD,
Parkway→PKWY, Place→PL, Terrace→TER), as do directionals (North→N). Whitespace,
punctuation and case are flattened. **Unit numbers are kept**, so
`3005 Palmetto St #1` and `#2` never merge.

Duplicates are detected on normalized address + city + state, and a ZIP only
splits them when both rows have one and they differ. Merging fills blanks
only — a known value is never overwritten — and when two rows disagree about a
signal, the positive claim is kept *and* recorded under NEEDS VERIFICATION.

### The LEAD score

| Signal | Points |
| --- | --- |
| absentee owner | +10 |
| vacant | +10 |
| high equity | +15 |
| pre-foreclosure | +15 |
| foreclosure | +15 |
| tax delinquent | +10 |
| probate | +10 |
| inherited | +10 |
| code violation | +10 |
| tired landlord | +10 |
| strong seller motivation | +15 (moderate: +7) |
| significant repairs | +10 |

Capped at 100. Bands: 90+ 🔥 HOT, 75+ 🟠 STRONG, 60+ 🟡 POSSIBLE, 40+ 🔵 WEAK,
else ❌ PASS.

Three rules keep the score honest:

* **No double counting.** Signals describing one event score once, at the
  highest value in the group: `{pre_foreclosure, foreclosure}` and
  `{probate, inherited}`. The groups are configurable
  (`exclusive_signal_groups`).
* **Motivation must be reported.** UNKNOWN motivation scores zero — silence is
  not motivation.
* **High equity is reported or derived, never guessed.** If the source says so,
  it counts. Otherwise it is computed from estimated value minus asking price
  (≥35% by default) and flagged as *derived arithmetic, not a title search*.
  With neither, it scores nothing.

### Filtering

Configurable in `LeadHunterConfig`, overridable per run from the CLI: target
states, property types, minimum lead score, minimum deal score, maximum asking
price, minimum equity, occupancy, and required distress signals.

The rule that matters: **a lead is only rejected on information that is
present.** "State is OH" rejects; "state is blank" produces a NEEDS
VERIFICATION warning and the lead continues. Missing fields are collected on
the lead itself (`missing_data`) so the gaps are visible rather than fatal.

### ARV provenance

A value from a lead list is a claim, not a fact:

| Status | Meaning |
| --- | --- |
| `SOURCE-PROVIDED — NEEDS ARV VERIFICATION` | the list gave a value; no comps back it |
| `ESTIMATED FROM COMPS` | comps exist but are thin |
| `VERIFIED/SUPPORTED BY COMPS` | the Wave 1 comp engine supports it |
| `NEEDS ARV VERIFICATION` | no usable value at all |

This is why `--lead-comps` matters: without comps, Wave 1 correctly gates every
lead to 🟡 NEED MORE DATA no matter how good the arithmetic looks. In the
bundled sample, the four leads that come with comps are the only ones that
reach GO or NEGOTIATE.

---

## 7. How the deal analysis works

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

## 8. Tests

```bash
python3 -m unittest discover -s tests -v     # no dependencies
pytest tests -v                              # if pytest is installed
```

- `tests/test_financials.py` — the deal math: MAO, spread, assignment price,
  rounding, risk haircuts, scenarios, and the reverse formulas.
- `tests/test_comps_and_valuation.py` — comp grading and ARV reconciliation.
- `tests/test_repairs_and_scoring.py` — rehab bands, score components, bands.
- `tests/test_pipeline.py` — CSV in → analysis → CSV out, plus the safety rails.
- `tests/test_lead_hunter.py` — Wave 2: address normalization, duplicate
  detection, lead scoring and classification, every filter, missing-data
  handling, CSV loading and aliases, the pipeline, its integration with the
  Wave 1 analyzer (including a check that the MAO comes from the Wave 1
  formula), the output files, the CLI, and the not-implemented seams.

`Wave1RegressionTests` re-asserts the original Wave 1 sample decisions, so a
Wave 2 change that altered underwriting behaviour would fail the suite.

---

## 9. Sample workflow

A realistic pass over a new lead list:

```bash
# 1. Hunt the list. Nothing is filtered on missing data — gaps are reported.
python3 -m wholesale_engine.main --leads new_list.csv --states FL,TX,MO

# 2. Read hot_leads.csv. Those are your calls, best-underwriting first.
#    Anything at 🟡 NEED MORE DATA is missing comps, not necessarily a bad deal.

# 3. Pull comps for the leads worth the effort, put them in a comps CSV,
#    and re-run. Now the ARV can move from SOURCE-PROVIDED to VERIFIED.
python3 -m wholesale_engine.main --leads new_list.csv --lead-comps new_comps.csv

# 4. Tighten the buy box once you have volume.
python3 -m wholesale_engine.main --leads new_list.csv --lead-comps new_comps.csv \
    --min-lead-score 60 --min-deal-score 60 --max-asking-price 250000 --hot-only

# 5. For a lead you are actually working, run the full Wave 1 report on it.
python3 -m wholesale_engine.main --csv one_property.csv --comps its_comps.csv
```

Step 3 is the one that matters. A lead list alone can never produce a
verified ARV, so the engine will keep saying NEED MORE DATA until you bring
comps — which is the honest answer, not a limitation to work around.

---

## 10. Extending it

### Adding another target market

One config change, no code:

```bash
python3 -m wholesale_engine.main --leads my_leads.csv --states FL,TX,MO,GA,TN
```

or permanently, in `config.py`:

```python
DEFAULT_TARGET_STATES: Tuple[str, ...] = ("FL", "TX", "MO", "GA", "TN")
```

`EXPANSION_STATES` already lists AL, LA, TN, GA, MS, AR, SC, NC, KY and OK
ready to move across. Nothing in `filters.py` names a state — it only ever asks
the config. Property types work the same way, via `--property-types` or
`DEFAULT_PROPERTY_TYPES`.

### Adding another lead source

Subclass `BaseLeadSource` and return `Lead` objects. Normalization,
de-duplication, scoring, filtering and the analyzer are all inherited:

```python
from wholesale_engine.lead_hunter import Lead, run_from_source
from wholesale_engine.lead_hunter.sources.base import BaseLeadSource

class MyListSource(BaseLeadSource):
    name = "my-list"

    def search_leads(self, criteria=None):
        return [Lead(address="...", city="...", state="FL", vacant=True)]

report = run_from_source(MyListSource())
```

Map only the fields your source actually returns; leave the rest blank.

---

## 11. Where the next modules plug in

Every future integration is a protocol already declared in
`wholesale_engine/data/sources.py` or
`wholesale_engine/lead_hunter/sources/base.py`. They raise
`NotImplementedError` today, and implementing one requires **no change to the
analysis layer**.

| Wave 3 capability | Seam | Where it attaches |
| --- | --- | --- |
| **Property data APIs** | `BaseLeadSource.search_leads()` / `get_property()` | Copy `lead_hunter/sources/api_source_template.py`, implement the methods, register it in `sources/__init__.py`. `run_from_source()` takes it as-is. |
| **Automated daily lead hunting** | the whole pipeline | Schedule `run_from_source(<source>)` and write the two CSVs — the pipeline is already one function call. |
| **County / public-record data** | `PropertyEnricher.enrich(lead)` | A pass before `analyze_comps()`. This is what eventually retires the standing "title, lien, mortgage payoff, foreclosure status" gap — until then the engine asserts none of it. |
| **Comp data feeds** | `BaseLeadSource.get_comps()` / `CompProvider` | Returns raw `Comp` objects appended to `lead.comps`, exactly like `--lead-comps` does today. Grading stays in `analysis/comps.py`, so vendor comps face the same reliability bar. |
| **Skip tracing** | `lead_hunter/skip_trace.py` | `skip_trace_candidates(report)` already gates it: analyzed leads that did not come back PASS. You pay to trace deals, not rows. Regulated data (TCPA, state calling laws, DNC, CAN-SPAM) — needs consent tracking, DNC scrubbing and a suppression list first. No phone or email is ever generated. |
| **Google Sheets / CRM** | `ResultSink.publish(results)` | Beside the CSV writers in `main.run()`. `lead_result_to_row()` already produces the flat dict a Sheets row or CRM record needs. |

The intended Wave 3 flow (Wave 2 built everything left of SKIP TRACE):

```
BaseLeadSource (API) ──┐
                       ├─► List[Lead] ─► normalize ─► dedupe ─► lead score ─► filter
CSV source ────────────┘                                                        │
                                          PropertyEnricher (county/public record)│
                                          CompProvider (comp feed) ──────────────┤
                                                                                 ▼
                                                        analysis.analyze_property()
                                                                                 │
                                    SkipTraceProvider ◄───────────────────────────┤ (GO / NEGOTIATE only)
                                                                                 ▼
                                        lead_pipeline.csv + hot_leads.csv + ResultSink (Sheets / CRM)
```

Two rules keep this safe as it grows:

1. **New sources may add facts; they may never relax the rules.** A vendor ARV
   is still just a number until comps support it.
2. **Absent data stays absent.** No integration may substitute a default for a
   fact it does not have.

---

## 12. Disclaimer

This is a screening tool, not investment, legal, or appraisal advice. It works
only from the data you give it, and no deal it scores is guaranteed profitable.
Verify value, repair costs, title, liens, and possession independently before
you put a property under contract.
