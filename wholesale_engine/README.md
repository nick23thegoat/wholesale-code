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
├── main.py                     CLI entry point (Waves 1, 2 and 4)
├── config.py                   every tunable assumption (fee, 70% rule, weights, thresholds)
├── settings.py                 credentials from the environment / .env (WAVE 4)
├── hunt.py                     the cost-controlled funnel (WAVE 4)
├── priority.py                 the PRIORITY SCORE — a third, separate ranking (WAVE 4)
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
├── providers/                  WAVE 4
│   ├── base.py                 PropertyDataProvider: the five capabilities
│   ├── criteria.py             HuntCriteria — geography, price, signals, gates
│   ├── csv_provider.py         local CSV provider (works with no credentials)
│   ├── http_provider.py        real-vendor template — inert until one is chosen
│   ├── registry.py             --source selection; no paid vendor pre-selected
│   └── metrics.py              provider call counting and the funnel report
├── research/                   WAVE 4
│   ├── facts.py                Fact: a value + its source + its confidence
│   ├── property_research.py    PropertyResearchService — the research pass
│   ├── owner_research.py       ownership of record (never contact data)
│   ├── distress.py             normalized distress signals with provenance
│   ├── equity.py               equity vs. a value-minus-asking spread
│   └── models.py               PropertyResearch — the normalized result
├── storage/                    WAVE 4
│   ├── database.py             SQLite store, watchlist, notes, activity, search
│   └── changes.py              price drops, ARV/DOM moves, new distress
├── outputs/                    WAVE 4
│   └── adapters.py             CSV + JSON adapters; Sheets seam (not connected)
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
    ├── hunt_report.py          the four Wave 4 CSVs + JSON + console summary
    ├── dossier.py              the property research screen (--property)
    ├── deal_tables.py          --top-deals / --hot-leads / --search tables
    └── output/                 generated files land here
.env.example                    credential placeholders (copy to .env)
tests/                          517 unit + end-to-end tests
```

The layering is strict, and it is what makes each wave additive:

```
providers / data (where facts come from) → models (what a fact looks like)
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

Optional, only when you are ready to connect a live data provider:

```bash
cp .env.example .env      # then fill in the keys from your provider account
```

`.env` is git-ignored. Everything in this README works without it.

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

### Wave 4 — the provider-backed hunt

```bash
# What can I use right now?
python3 -m wholesale_engine.main --list-sources

# The full funnel, no API key needed
python3 -m wholesale_engine.main --hunt --source csv \
    --states FL,TX,MO --min-lead-score 60 --min-deal-score 60
```

Section 11 covers providers, cost control, the lead database and change
detection.

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
  Target Wholesale Fee:      $18,000
  End-Buyer Ceiling:         $150,500
  MAO:                       $90,500
  Recommended Offer:         $82,000  (7% below MAO)
  Potential Assignment Price:$100,000
  Deal Cushion (MAO - Offer):$8,500

  WHOLESALE FEE
    Target Wholesale Fee:       $18,000
    Potential Wholesale Fee:    $26,500
    Wholesale Fee Status:       MEETS TARGET
      at recommended offer:     $26,500
      at asking price:          $26,500

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
ADDRESS                     ST    LEAD CLASS        DEAL DECISION                OFFER       FEE  FEE STATUS
--------------------------------------------------------------------------------------------------------
77 Sabal Palm Way           FL    80.0 🟠 STRONG     83.5 🔥 GO                 $109,000   $21,500  MEETS TARGET
145 Cedar Hollow Lane       MO    90.0 🔥 HOT        79.0 🟠 NEGOTIATE           $59,500   $14,000  BELOW TARGET
905 Pecan Street            TX    95.0 🔥 HOT        78.6 🟠 NEGOTIATE          $118,000    $2,800  BELOW TARGET
4501 Live Oak Circle        TX    52.0 🔵 WEAK       76.0 🟠 NEGOTIATE           $66,500   -$2,400  BELOW TARGET
412 Magnolia Lane           MO    70.0 🟡 POSSIBLE   72.4 🟡 NEED MORE DATA      $69,500   $26,500  MEETS TARGET
66 Camellia Court           FL    90.0 🔥 HOT        62.6 🟡 NEED MORE DATA      $33,000    $5,400  BELOW TARGET
...
2210 Beechwood Avenue       OH    95.0 🔥 HOT         —   filtered out                —         —
17 Sycamore Road            OH     0.0 ❌ PASS        —   filtered out                —         —
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
* **905 Pecan Street** scores 78.6 and still is not a GO: at the asking price
  the assignment fee is only $2,800 against an $18,000 target. Only one lead in
  the sample clears the fee bar at the price the seller is actually asking.

Two files are written:

`reports/output/lead_pipeline.csv` — every unique lead, ranked, including the
ones that were filtered out (with the reason in `filter_reasons`, and a blank
deal side rather than a misleading zero):

```
lead_id,property_id,address,city,state,county,zip_code,owner_name,asking_price,
estimated_value,estimated_repairs,lead_score,lead_classification,deal_score,
deal_classification,mao,recommended_offer,potential_assignment_price,
potential_spread,target_wholesale_fee,potential_wholesale_fee,wholesale_fee_status,
final_decision,lead_source,arv_confidence,comp_confidence,risk_flags,missing_data
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
End-Buyer Ceiling        = ARV × 70% − Repairs        (the most a cash buyer can pay)
MAO                      = End-Buyer Ceiling − Target Wholesale Fee
                         = (ARV × 70%) − Repairs − $18,000

Recommended Offer        = MAO − risk haircut, capped at the asking price
Potential Assignment     = Recommended Offer + Target Wholesale Fee
Potential Wholesale Fee  = End-Buyer Ceiling − purchase price
Buyer Margin             = End-Buyer Ceiling − Potential Assignment Price
Deal Cushion             = MAO − Recommended Offer
```

**Cushion is not the fee.** MAO already reserves the target fee, so the cushion
is money *on top of* it. Buy at MAO and the fee is exactly $18,000; buy $13,500
below MAO and the fee is $31,500, not $13,500. The report labels the line
`Deal Cushion (MAO - Offer)` for exactly this reason.

### Wholesale fee status

**$18,000 is a target, not a minimum.** Nothing in the engine rejects or
downgrades a deal for coming in under it. A $13,000 assignment on a strong
deal is a real deal, and the engine treats it as one.

Every fee figure is reported **with the price it was measured at**, because a
fee without its price is meaningless:

```
Target Wholesale Fee:       $18,000
  at your offer $59,500:    $25,500
  at asking $71,000:        $14,000
Potential Wholesale Fee:    $14,000 (at asking $71,000)
Wholesale Fee Status:       BELOW TARGET
Buyer Margin at Assignment: $7,500
```

The headline is judged **at the price actually on the table** — the asking
price when the seller wants more than you plan to offer, otherwise your own
offer. An offer the seller has not accepted cannot be what qualifies a deal.

| Status | Meaning |
| --- | --- |
| `MEETS TARGET` | the deal supports the full target fee at the binding price |
| `BELOW TARGET` | it supports less — a **label and a scoring penalty**, never a rejection |
| `UNKNOWN` | no ARV, no repair basis, or no price to measure against |

BELOW TARGET does three things and no more:

1. labels the row and the report
2. raises the `BELOW TARGET WHOLESALE FEE` risk flag, naming the shortfall and
   what the seller would have to come down to close it
3. lowers the `wholesale_spread` score component (16 of 100 points), which is
   continuous — $14,000 scores below $18,000, which scores below $31,000

The **deal score remains the decision mechanism**. The only fee-based gate is
a viability floor far below the target, because "GO" has to mean something:

```python
min_viable_wholesale_fee: float = 10_000.0   # --min-fee 0 removes it entirely
```

At the defaults a $14,000 fee can be a 🔥 GO (carrying its flag); a $2,800 fee
cannot be, at any score.

All three numbers stay configurable in `config.py`:

```python
ARV_PERCENTAGE: float = 0.70
TARGET_WHOLESALE_FEE: float = 18_000.0
```

or per run: `--arv-pct 65 --fee 25000 --min-fee 12000`.

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

- `tests/test_wholesale_economics.py` — the fee kept separate from the cushion,
  fee status at the binding price, and the audit's central rule: a below-target
  fee can still reach GO, and no hidden knob turns the target into a minimum.
- `tests/test_wave4_providers.py` — settings and credential handling, the
  capability contract (unsupported is an answer, not a guess), the inert HTTP
  template, the registry, criteria matching, and cost control.
- `tests/test_wave4_storage.py` — the SQLite store, cross-run identity, all
  seven statuses, change detection, and the five output files.
- `tests/test_wave4_hunt.py` — the funnel end to end, the CLI, and the check
  that the hunt reproduces Wave 2's analysis **exactly** (no second analyzer).
- `tests/test_research.py` — facts and provenance, the property/owner/distress
  research services, and the equity engine's refusal to call a spread equity.
- `tests/test_priority.py` — the PRIORITY SCORE: every band, every component,
  and the rule that a below-target fee is never disqualifying.
- `tests/test_watchlist.py` — the ten statuses, notes, the activity log, every
  search filter, the ranked tables, the dossier, and the new CLI commands.

`Wave1RegressionTests` re-asserts the original Wave 1 sample decisions, so a
later change that altered underwriting behaviour would fail the suite.

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

## 11. Wave 4 — the provider architecture

Wave 4 puts a data layer *in front of* the existing engine. It adds no
arithmetic: every number still comes from the Wave 1 analyzer, and
`tests/test_wave4_hunt.py` asserts that a lead run through the hunt produces
byte-identical results to the same lead run through Wave 2.

```
provider.search_properties()  ─►  normalize ─► dedupe ─► cheap filters
                                                              │
                                                        lead score
                                                              │
                                    property research (BILLABLE, capped)
                                                              │
                                     comps / valuation (BILLABLE, capped)
                                                              │
                                     analyze_property()  (Wave 1, unchanged)
                                                              │
                          daily_leads · hot_leads · deals_to_review · rejected_leads
```

### Running it

```bash
# Everything below works today, with no API key of any kind.
python3 -m wholesale_engine.main --list-sources

python3 -m wholesale_engine.main --hunt --source csv \
    --states FL,TX,MO --min-lead-score 60 --min-deal-score 60

# The full search surface
python3 -m wholesale_engine.main --hunt --leads my_list.csv \
    --states FL,TX --counties Hillsborough,Bexar --cities Tampa \
    --zip-codes 33601,33602 --min-price 50000 --max-price 250000 \
    --min-equity 40000 --vacant --probate --tax-delinquent \
    --min-lead-score 60 --min-deal-score 60 \
    --research-limit 100 --comps-limit 30
```

Signal flags are **any-of**, and an unknown value never rejects a lead — it
becomes a gap to go and fill.

### Providers

A provider answers up to five questions. Only the first is required.

| Capability | Purpose |
| --- | --- |
| `search_properties()` | find candidates matching the criteria |
| `get_property()` | full detail for one property |
| `get_owner()` | ownership of record — **never** phone or email |
| `get_distress_data()` | liens, tax status, foreclosure filings |
| `get_comps()` | comparable sales |

Anything a provider does not support returns a `ProviderResponse` with
`supported=False` and a reason. That is a clear answer, not an error and never
a guess — the field stays blank and is reported as missing data.

Two providers ship:

| `--source` | Status |
| --- | --- |
| `csv` | ready. Local files, no credentials, no network, no cost. |
| `http-template` | inert. A finished transport with no vendor wired to it. |

**No paid vendor is pre-selected**, deliberately. Choosing one means reading
that vendor's official API documentation, terms and pricing first. With no
credentials configured the engine says so and runs in CSV/test mode; it never
fabricates a live result.

### Credentials

Copy `.env.example` to `.env` (git-ignored) and fill it in:

```
PROPERTY_DATA_API_KEY=
PROPERTY_DATA_BASE_URL=
PUBLIC_RECORDS_API_KEY=
COMPS_API_KEY=
SKIP_TRACE_API_KEY=
```

A key alone is not enough for a live search: the base URL comes from the
vendor's documentation, and the engine will not invent an endpoint. Nothing is
ever hard-coded, and `ProviderSettings.describe()` never prints a credential.

### Adding a real provider

```python
from wholesale_engine.providers import HttpPropertyDataProvider, register

class MyVendor(HttpPropertyDataProvider):
    name = "myvendor"
    search_path = "properties/search"        # from THEIR documentation
    capabilities = (Capability.SEARCH, Capability.PROPERTY, Capability.COMPS)

    def build_search_params(self, criteria): ...   # push filters server-side
    def parse_lead(self, payload): ...             # leave unknowns blank

register("myvendor", lambda settings, csv_path, comps_path, metrics:
         MyVendor(settings, metrics))
```

Rate limiting, retry with backoff, auth headers, call counting and the funnel
are already written. Use the documented API with your own account, honour its
published limits, and do not scrape sites or work around CAPTCHAs,
robots.txt, logins, paywalls or anti-bot measures.

### Cost control

Paid APIs bill per request, so the funnel is ordered strictly cheapest-first
and every stage narrows what the next one sees:

```
1,000 raw leads      1 search call
  cheap filters      free   (geography, price, type, signals, equity)
300 leads
  lead scoring       free   (Wave 2 rules)
100 leads
  property research  BILLABLE — capped by --research-limit (default 100)
30 candidates
  comps / valuation  BILLABLE — capped by --comps-limit (default 30)
10 hot deals
  skip tracing       NOT CONNECTED
```

Comps are never requested for a raw lead, and nothing is ever skip traced.
Both are enforced in `hunt.py` and asserted by the tests. Every run prints
what it spent:

```
PROVIDER CALLS
  Properties searched / returned / filtered
  Search / property-detail / owner / distress / comp / skip-trace calls
  API errors
  Estimated API calls
  FUNNEL — survivors at each stage
```

### The local database

`wholesale_engine/data/leads.db` (SQLite, stdlib only, git-ignored) remembers
what has been seen. Identity is the **normalized address + city + state +
ZIP** — the same key the Wave 2 deduplicator uses, so within-run and
across-run duplicate detection agree. Unit numbers are preserved: `#1` and
`#2` are two properties.

Statuses: `NEW` · `RESEARCHED` · `HOT` · `CONTACT` · `UNDER_CONTRACT` ·
`PASSED` · `DEAD`. `first_seen` never moves, and a status you set by hand
survives the next sighting.

### Change detection

A property that reappears is not news. A property that reappears cheaper is:

```
77 Sabal Palm Way:
  PRICE DROP: $119,000 -> $99,000 (-$20,000, 17%)
  DEAL SCORE: 84 -> 88
  PRIORITY +26
```

Detected: price drops and increases, new distress signals, new vacancy,
foreclosure and tax-delinquency changes, new estimated value, new repair
estimate, and score movement. Changes raise a lead's **working priority**
only — never its lead score or deal score, which stay exactly what the scoring
rules say. Known-to-unknown is not a change: a source that stopped reporting a
fact has not told you the fact went away.

### Outputs

| File | Contents |
| --- | --- |
| `daily_leads.csv` | everything the hunt touched, best first |
| `hot_leads.csv` | analyzed, HOT/STRONG lead score, decision GO |
| `deals_to_review.csv` | analyzed and worth a look, not a green light |
| `rejected_leads.csv` | filtered or below the gates, with the reason |
| `daily_leads.json` | the same rows plus the run's provider-call counts |

Every row separates lead score from deal score, carries three confidence
readings (data, ARV, comp), and keeps the fee quantities apart: target fee,
potential fee, fee at asking, deal cushion, MAO, recommended offer.

CSV and JSON are fully functional. Google Sheets is a declared adapter that
raises rather than silently doing nothing — a silent success would be worse
than an error.

### Still not connected

| Capability | Status |
| --- | --- |
| Skip tracing | interface only. No provider, no phone number or email ever generated. |
| Google Sheets / CRM | adapter seam only. Needs a service account. |
| Live property data | needs a vendor chosen from its own API documentation. |
| Skip tracing (again) | the `research/` layer holds no contact field at all. |

Two rules keep this safe as it grows:

1. **New sources may add facts; they may never relax the rules.** A vendor ARV
   is still just a number until comps support it.
2. **Absent data stays absent.** No integration may substitute a default for a
   fact it does not have.

---

## 12. The research engine, priority, and the watchlist (Wave 4, part 2)

The full pipeline, with the research layer in place:

```
LEAD SOURCE -> PROPERTY RESEARCH -> OWNER RESEARCH -> DISTRESS -> EQUITY
   -> COMPS -> ARV -> REPAIRS -> MAO -> OFFER
   -> LEAD SCORE -> DEAL SCORE -> PRIORITY -> HOT LEAD
```

Everything left of COMPS is new; everything from COMPS rightwards is the Wave 1
analyzer, unchanged.

### Facts, not values

Every researched field is a `Fact`: a value, the source it came from, and how
much to trust it.

```python
Fact.reported(True, "county_records", Confidence.HIGH)
Fact.unknown("no public-record source configured")
```

A bare `None` cannot say whether nobody looked, somebody looked and found
nothing, or a source reported it and the source is unreliable. `Fact` says
which — which is why nothing in this layer can quietly manufacture a value.

Confidence is `HIGH` only for a primary source. A lead-list CSV is somebody's
claim, so it is `MEDIUM` at best.

### Equity vs. the spread

This is the number most often got wrong in wholesaling, so the engine keeps
four cases apart:

| Status | Means |
| --- | --- |
| `CALCULATED` | value − mortgage − liens. The real thing. |
| `REPORTED` | a source handed us a number. Their claim, unverified. |
| `DERIVED (mortgage unknown)` | value − asking price. **A spread, not equity.** |
| `UNKNOWN` | no mortgage information at all. |

Value minus asking price equals equity only if the property is free and clear.
It is useful, and it is never labelled as though a mortgage had been checked —
a derived spread cannot set the high-equity signal that the lead score pays
points for, and a missing mortgage balance never becomes `0`.

### Owner research

Ownership of record only: name, mailing address, years owned, properties owned,
entity/LLC detection. **Never a phone number or an email address** — that is
skip tracing, a separate regulated step with no provider connected. An owner
record has no field that could hold contact data, and a test asserts it.

Entity ownership is derived from the name ("SUNSHINE HOLDINGS LLC" → `LLC`) and
raises a note: confirm who can bind the entity before you paper a contract.

### PRIORITY SCORE

The third score. Three questions, deliberately never merged:

| Score | Question |
| --- | --- |
| **LEAD SCORE** | is this worth a phone call? |
| **DEAL SCORE** | is this worth a contract? |
| **PRIORITY SCORE** | what do I work on first? |

Priority *reads* the other two and never writes to them. It adds what they
deliberately ignore: data confidence, distress urgency, whether the price just
moved, and how long it has sat. A deal you cannot verify ranks below one you
can, at the same deal score — that is the point.

```
deal score 26 · lead score 16 · wholesale fee 14 · data confidence 12
distress 10 · equity 8 · price movement 8 · days on market 6
```

Bands: `🔥 PRIORITY` 80+ · `🟠 HIGH` 65+ · `🟡 REVIEW` 50+ · `🔵 LOW` 30+ ·
`❌ REJECT` below. A `❌ PASS` from the analyzer is capped below LOW — priority
ranks what is worth working, and the analyzer already answered that.

**The fee is a target here too.** Fee credit is proportional and never
disqualifying: $13,000 against an $18,000 target scores most of the way, and a
below-target deal can still reach `🔥 PRIORITY`.

### The deal watchlist

```
NEW -> WATCH -> HOT -> CONTACT -> OFFER_SENT -> UNDER_CONTRACT -> ASSIGNED
                    -> PASSED / DEAD / CLOSED at any point
```

Nothing enforces the order — deals skip steps and go backwards — but every move
is recorded with where it came from and why, so the history answers "what
happened to that one?" months later. A status you set by hand survives the next
hunt; the source relisting a property does not reset it to NEW.

### Notes and activity

Notes are yours, free-text, and the engine never writes one for you (a test
asserts a freshly hunted lead has none). The activity log records lead created,
lead updated, score changed, price changed, status changed, note added,
research completed, and offer calculated — each with a timestamp, the property,
a type and a description.

### Commands

```bash
# Rank what to work on
python3 -m wholesale_engine.main --top-deals --limit 20
python3 -m wholesale_engine.main --hot-leads
python3 -m wholesale_engine.main --watchlist

# Search the local database
python3 -m wholesale_engine.main --search --states MO --min-lead-score 70
python3 -m wholesale_engine.main --search --vacant --probate --min-fee 15000
python3 -m wholesale_engine.main --search --text "Sabal" --open-only

# The research screen for one property
python3 -m wholesale_engine.main --property LH-011

# Work a lead
python3 -m wholesale_engine.main --property LH-011 \
    --set-status HOT --reason "verified ARV" \
    --note "Called seller 8/22. Wants a quick close."

# What has happened lately
python3 -m wholesale_engine.main --activity --limit 20

# Export (CSV, JSON, or both)
python3 -m wholesale_engine.main --export-hot --export-top-deals \
    --export-watchlist --format both
```

Search filters: `--states --counties --cities --zip-codes --property-types
--min-price --max-price --min-arv --max-arv --min-equity --min-fee
--min-lead-score --min-deal-score --min-priority-score --min-dom --max-dom
--vacant --absentee --high-equity --pre-foreclosure --foreclosure
--tax-delinquent --probate --inherited --code-violation --tired-landlord
--status --open-only --text --sort-by --limit`.

### The dossier

`--property <id>` is the main research screen: PROPERTY · OWNER · DISTRESS ·
EQUITY · VALUATION · COMPS · REPAIRS · MAO AND OFFER · WHOLESALE ECONOMICS ·
SCORES (all three) · FINAL DECISION · RISK FLAGS · MISSING DATA · STATUS ·
ACTIVITY HISTORY · NOTES.

It re-runs research and analysis live rather than printing only the stored
snapshot, and every section can say "unknown" — several usually will. That is
the report working correctly. A dossier that never admits a gap is one that is
making things up.

---

## 13. Disclaimer

This is a screening tool, not investment, legal, or appraisal advice. It works
only from the data you give it, and no deal it scores is guaranteed profitable.
Verify value, repair costs, title, liens, and possession independently before
you put a property under contract.
