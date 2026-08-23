# PROJECT HANDOFF / CONTINUE FROM HERE

## 0. Read this first

You are continuing an in-progress build. **Do not rebuild, redesign, or
re-litigate anything below.** Architecture decisions are settled. Pick up at
§13 (Exact Next Step).

**Repo:** `nick23thegoat/wholesale-code`
**Branch:** `claude/wholesale-acquisition-v1-ryeo63` (develop and push here; never another branch)
**Last commit:** `d40e71d` — "Make the buy box editable data and persist accept/reject reasons"
**Language:** Python 3.11. Engine is **stdlib-only**. Flask is the *only* runtime dependency, and only for the web layer.
**Tests:** `python3 -m unittest discover -s tests` → **957 passing**. `pytest` is NOT installed; use `unittest`.
**No PR exists and none should be created unless the user explicitly asks.**

---

## 1. What this is

A real-estate wholesaling acquisition engine. It finds distressed/off-market
residential property leads, scores them, underwrites them as wholesale deals,
and surfaces the ones worth acting on.

**New end goal (changed mid-project):** the user does **not own a computer
long-term**. The system must run 24/7 on a **Linux VPS** and be usable from an
**iPhone** via a simple web dashboard reached over **Tailscale**.

---

## 2. Standing instructions from the user — these are binding

- **Ask for confirmation before any major architectural change.**
- Never invent API endpoints, response fields, phone numbers, emails, owners,
  mortgages, liens, foreclosure status, ARVs, or comps. **Unknown stays NULL/UNKNOWN.**
- Never bypass CAPTCHAs, robots.txt, rate limits, authentication, paywalls, or
  anti-bot systems. Only legitimate documented APIs with the user's own account.
- Never hard-code credentials. Never log API keys, tokens, or passwords.
- Finish the **core engine before optional features**.
- Handle API failures gracefully — **never crash**.
- Keep business logic **separate from the web interface**.
- Do not create legal documents or give legal advice.
- Do not spawn subagents unless asked.

---

## 3. Economics rules — CORRECTED MULTIPLE TIMES, DO NOT REGRESS

```
End-Buyer Ceiling = (ARV × 70%) − repairs
MAO               = End-Buyer Ceiling − target_wholesale_fee
Wholesale Fee     = End-Buyer Ceiling − actual purchase price
Deal Cushion      = MAO − offer          ← NOT the wholesale fee. Never call it that.
```

- **$18,000 is a TARGET, not a hard minimum.** A $13,000 assignment can be a
  perfectly good deal. The engine must **never reject or downgrade a deal solely
  because the fee is below $18,000.** A below-target fee is *labelled*, not rejected.
- `min_viable_wholesale_fee = 10_000.0` is the **economic viability floor** — the
  point below which a deal stops being called a GO. It is a *different thing*
  from the target. A viability floor above the target is a validation error.
- The fee is judged at the **binding price** — the asking price when asking >
  recommended offer.
- A spread (value − asking) is **not equity**. Equity requires a known mortgage
  balance; otherwise it is labelled a spread.
- **Four separate scores, never conflated:**
  - LEAD score — is it worth a call?
  - DEAL score — is it worth a contract?
  - PRIORITY score — what do I work first?
  - ACQUISITION PRIORITY — what is the next physical action?
  A 🔥 HOT lead can be a ❌ PASS deal.
- **Price range: `MIN_PROPERTY_PRICE = 0`, `MAX_PROPERTY_PRICE = 2_200_000`.**
  This is a **buyer-capacity SEARCH ceiling, not a deal rule.** There is
  deliberately **no low ceiling** — never reintroduce one (e.g. $150k). A $1.4M
  house with a real spread is a lead; a $60k house with no spread is not.
  Ranking is by profitability, risk, and deal quality — never by purchase price.

---

## 4. Architecture and layering

```
wholesale_engine/          business logic — stdlib only, NEVER imports from web/
    service/               NOT BUILT YET — thin orchestration seam
    web/                   NOT BUILT YET — Flask only; may ONLY call service/
    deploy/                NOT BUILT YET — systemd units + runbook
```

**Hard rule:** `web/` may only call `service/`. Nothing in `wholesale_engine/`
ever imports from `web/`. The CLI must keep working unchanged.

**Key finding:** the separation the user asked for **mostly already exists**.
`wholesale_engine/main.py` (~2,030 lines) is a *presentation layer* over library
modules (`hunt.py`, `analysis/`, `lead_hunter/`, `storage/`). The web UI becomes
a *second* presentation layer — this is not a rewrite.

Strict import layering already enforced: `providers/data → models → analysis → reports`.
`pipeline_status.py` lives at top level to avoid a storage↔acquisitions circular import.

---

## 5. What is BUILT and WORKING

Waves 1–6 are complete and tested.

| Area | Modules |
|---|---|
| Deal analyzer | `analysis/` — analyzer, comps, financials, repairs, scoring, valuation |
| Lead hunter | `lead_hunter/` — filters, normalizer, pipeline, scoring, sources |
| Cost-controlled funnel | `hunt.py` — search → cheap filters → lead score → research → comps → deal |
| Research engine | `research/` — facts (Fact wrapper w/ Confidence HIGH/MEDIUM/LOW/UNKNOWN), distress, equity, owner, property |
| Priority scoring | `priority.py` |
| Storage | `storage/database.py` (SQLite `LeadStore`), `changes.py`, `decisions.py` |
| Acquisitions workflow | `acquisitions/` — 16-status pipeline, contacts, skip trace, offers, contracts, buyers, assignments |
| Providers | `providers/` — base, registry, csv, http_client, http_provider, metrics, propertyreach, cache, quota |
| Runtime / safety | `runtime.py` (TEST/LIVE modes), `security.py`, `budget.py`, `backup.py`, `settings.py` |
| Automation | `automation/daily.py` → `run_daily(store, provider, …) -> DailyReport` (headless, unattended-safe) |
| Integrations | `integrations/` — notifications, CRM, sheets, outreach, ai_notes (all adapters, NOT CONNECTED) |
| Reports | `reports/` — hunt, lead, deal_room, dossier, CSV/JSON exports |
| CLI | `main.py` — `--hunt --daily --dashboard --top-deals --deal-room --health --provider-status --integrations --security-audit --budget-status` etc. |

**Registered providers:** `['csv', 'http-template', 'propertyreach']`

**PropertyReach adapter** (`providers/propertyreach.py` + `propertyreach_schema.py`):
built, registered, 9 capabilities declared. Base URL `https://api.propertyreach.com/v1`,
`x-api-key` header, `POST /v1/skip-trace` confirmed. Search/detail/comps REST paths
are **UNVERIFIED** and the adapter **refuses to call an unverified endpoint** rather
than guess. **PropertyReach is NOT the current priority — RentCast is.**

### Safety infrastructure already in place
- `SafeHttpClient` — timeouts, bounded retries, exponential backoff + jitter,
  `Retry-After` honoured/capped, https-only, self-imposed rate limit, credential
  redaction in URLs/headers/bodies/repr, **auth failures never retried**.
- `ProviderResponse` — three distinct states: `supported=False` (can't answer) /
  `supported=True, data=None` (nothing found) / real answer. Returns a *reason*
  instead of raising. A dead API degrades the run; it never ends it.
- `security.py` — source audit runs **as a test**, failing the build on shell
  calls, eval/exec/pickle, hard-coded secrets, interpolated SQL.
- Tri-state booleans (`Optional[bool]`, `None` = unknown) for all distress signals.

---

## 6. RentCast integration — CURRENT FOCUS

The user **has a RentCast API key**. It must **never be pasted into chat.**

### Confirmed from RentCast's published docs
| | |
|---|---|
| Base URL | `https://api.rentcast.io/v1` |
| Auth header | `X-Api-Key` (bare key, **no** `Bearer` scheme) |
| Transport | REST, JSON |
| Free plan | **50 successful requests/month** |
| Billing | **Only successful requests are counted.** 401/403/429/timeouts are NOT billed. |
| `/properties` max page size | **500 records — still ONE request** |

**Endpoints:** `/properties`, `/properties/random`, `/properties/{id}`,
`/avm/value`, `/avm/rent/long-term`, `/listings/sale`,
`/listings/rental/long-term`, `/listings/sale/{id}`, `/listings/rental/long-term/{id}`

**`/properties` query params:** `address`, `city`, `state`, `zipCode`,
`latitude`, `longitude`, `radius`, `propertyType`, `bedrooms`, `bathrooms`
(multi-value with `|`, e.g. `"1|3"`), `limit` (max 500), `offset`

**Response field names recovered from docs pages — NOT YET VERIFIED against a live
response:** `id`, `formattedAddress`, `addressLine1`, `addressLine2`, `city`,
`state`, `zipCode`, `county`, `latitude`, `longitude`, `propertyType`,
`bedrooms`, `bathrooms`, `squareFootage`, `lotSize`, `yearBuilt`, `assessorID`,
`legalDescription`, `ownerOccupied`, `owner` (`{names[], type, mailingAddress}`),
`lastSaleDate`, `lastSalePrice`, `features`, `taxAssessments` (year-keyed),
`propertyTaxes` (year-keyed), `history` (date-keyed)

### Budget math that drives the design
- **Owner info, property details, tax data, last-sale data are FREE** — they are
  fields *inside* `/properties` records, not separate calls.
- **`/avm/value` is 1 request per property** — this is the real constraint.
- Chosen cadence: **weekly** → ~4 searches/month, leaving **~40–45 requests for
  AVM valuations** on the best-scoring leads.
  *(Correction made late in the last session: an earlier suggestion of ~15 AVM
  calls/month was too low. ~40 is right given weekly search cadence.)*

### Built for RentCast so far

**`scripts/rentcast_probe.py`** — spends exactly ONE request against `/properties`,
saves raw JSON to `rentcast_sample.json` (git-ignored), prints a field inventory
(name, type, fill rate).
```bash
python3 scripts/rentcast_probe.py --zip 33607 --dry-run   # costs 0
python3 scripts/rentcast_probe.py --zip 33607             # costs 1
```
Flags: `--zip` (required), `--limit` (default/max 500), `--offset`,
`--property-type`, `--out`, `--dry-run`, `--force`, `--show-values`.
Refuses to re-pull when a sample exists. Owner names/mailing addresses redacted
in the printed summary by default (so it's safe to paste into chat);
`ownerOccupied` is deliberately NOT redacted (it's a distress signal, not identity).
Date/year-keyed maps collapse to `{date}` / `{year}` so 500 records describe one shape.

**`wholesale_engine/providers/quota.py`** — `QuotaLedger`
- Ledger file: `wholesale_engine/data/api_usage.json` (git-ignored)
- Env var `MAX_RENTCAST` (default **50**); keyed by calendar month `YYYY-MM` **UTC**
- Only **successful** requests recorded; cache hits and failures never counted
- Month rolls over automatically; previous months preserved
- **A corrupt ledger reads as fully spent, not unlimited** — fails safe
- `require(n)` raises `QuotaExceeded` *before* the request, not after

**`wholesale_engine/providers/cache.py`** — `ResponseCache`
- Dir: `wholesale_engine/data/cache/` (git-ignored)
- **Cache key excludes credentials** — rotating the key does not invalidate the
  cache, and no cache file can contain a key (both asserted by tests)
- Only successful responses cached; corrupt/expired/timestamp-less entry = miss, never an exception
- TTLs: `TTL_PROPERTY_RECORDS` 30d, `TTL_VALUATION` 7d, `TTL_LISTINGS` 1d
- `enabled=False` for `--no-cache`; `clear()` is per-provider

---

## 7. Buy box — `wholesale_engine/buybox.py`

Editable **JSON, not code**. Lives at `config/buybox.json`, overridable with
`BUYBOX_PATH`, **outside the package so `git pull` on the server cannot clobber it**.
Committed example: `config/buybox.example.json`.

Fields: `name`, `notes`, `enabled`, `states`, `zip_codes`, `cities`, `counties`,
`property_types`, `min/max_beds`, `min_baths`, `min/max_sqft`, `min/max_year_built`,
`min/max_price`, `required_signals`, `min_signal_count`, `min_equity`,
`min_lead_score`, `min_deal_score`, `target_wholesale_fee`, `min_viable_wholesale_fee`.

Behaviour built for unattended operation:
- Corrupt JSON / unknown key / wrong type → **warning, run continues on defaults**.
  A 3am scheduled job must not die because a field was edited badly from a phone.
- `validate()` returns **every** problem at once (so a form shows the whole picture).
- `save()` validates first — an invalid buy box **cannot reach disk** — and writes
  through a temp file then renames, so an interrupted save can't leave a broken file.
- Phone-form input tolerated: `"33607, 33609"` → list; `"$2,200,000"` → number;
  **blank number = no constraint, never 0**.
- `search_count` = ZIPs + cities + counties = **API requests one run costs**.
  This IS the monthly budget.

`ALLOWED_PROPERTY_TYPES` excludes `land` and `commercial` — the ARV/rehab model
can't underwrite them.

---

## 8. Decision log — `wholesale_engine/storage/decisions.py`

Two new SQLite tables in `leads.db`: **`runs`** and **`decisions`**.

- `RunRecord`: `started_at, finished_at, trigger (manual|scheduled|api), buy_box,
  provider, mode, status (RUNNING|OK|PARTIAL|FAILED), api_requests_spent,
  cache_hits, leads_seen, leads_accepted, leads_rejected, error, notes`
- `Decision`: `dedupe_key, address, stage, outcome, reason, detail, lead_score,
  deal_score, lead_row_id, decided_at`
- Outcomes: `ACCEPTED` / `REJECTED` / `INCOMPLETE` (incomplete is **not** a rejection)
- Stages in funnel order: `search, dedupe, buy_box, lead_score, research, comps, deal_score, final`

**Every property the run saw gets a row, including rejects** — a funnel you can
only see the survivors of is one you cannot tune. `reason` is short and groupable;
`detail` is property-specific.

`render_summary(run_id)` output:
```
WHY 3 PROPERTIES WERE REJECTED

     2  (  67%)  [lead_score] below minimum lead score
     1  (  33%)  [buy_box] asking price above buy box maximum
```
`for_property(dedupe_key)` traces one property **across runs** — rejected 3 weeks
running for the same reason is a buy box problem, not a property problem.

**⚠️ The tables and API exist but NOTHING WRITES TO THEM YET.** `hunt.py` is not
yet wired to emit decisions. This is part of the next step.

---

## 9. Confirmed architecture decisions (user chose these)

| Decision | Choice |
|---|---|
| Web stack | **Flask**, minimal deps, pinned in `requirements.txt` |
| iPhone access | **Tailscale**; Flask bound to `127.0.0.1` ONLY |
| Manual search runs | **Allowed, with explicit confirmation + remaining quota shown** |
| Scheduled cadence | **Weekly** |
| Scheduler mechanism | systemd timer (VPS-native, survives reboot, logs to journald) |
| Buy box format | JSON file, editable from the dashboard |

---

## 10. Tailscale + VPS deployment plan

Tailscale is a **private overlay mesh** (WireGuard), not a home-network thing.
Each device gets a stable `100.x.y.z` address that works **from anywhere** —
cellular, hotel wifi, another state. Nothing is exposed to the public internet.
Free personal plan is sufficient.

**VPS:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # prints a login URL
tailscale ip -4            # note the 100.x.y.z
tailscale status
```
**iPhone:** App Store → Tailscale → sign in with the **same account** → toggle on.

**HTTPS:** admin console → DNS → enable **MagicDNS**, then **HTTPS Certificates**. Then:
```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
```
Dashboard becomes `https://<vps-hostname>.<tailnet>.ts.net` with a valid cert.

**⚠️ Two warnings, both load-bearing:**
- **Bind Flask to `127.0.0.1`, NEVER `0.0.0.0`.** `tailscale serve` proxies from
  the tailnet to localhost. Binding to localhost makes public exposure impossible
  even if the VPS firewall is misconfigured. This default must be hard-coded.
- **NEVER run `tailscale funnel`.** One letter from `serve`, and it deliberately
  publishes to the public internet.

**User status:** was given these steps and asked to run them; **completion not yet
confirmed.** A verification test was suggested (`python3 -m http.server 8000
--bind 127.0.0.1` + `tailscale serve`) — ask whether it worked.

---

## 11. Security requirements

- **API key storage** — never `export RENTCAST_API_KEY=...` (lands in shell history):
  ```bash
  cd ~/wholesale-code
  read -rs RENTCAST_KEY                              # paste; nothing echoes
  printf 'RENTCAST_API_KEY=%s\n' "$RENTCAST_KEY" >> .env
  unset RENTCAST_KEY && chmod 600 .env
  ```
  Verify without printing: `grep -c '^RENTCAST_API_KEY=.\+' .env` and `git check-ignore -v .env`
- `.env` is git-ignored. Never committed, excluded from `--backup` by default.
- Credentials never reach logs, error text, exports, or the response cache (tested).
- Parameterized SQL only; sort columns from an allow-list; paths validated against traversal.
- `security.py` audit runs as a test and fails the build on violations.
- Web layer will need: no secrets in templates, CSRF protection on the buy-box
  editor and run-now POST, and no arbitrary shell execution.

**Git-ignored local state:**
```
.env
wholesale_engine/data/leads.db
wholesale_engine/data/cache/
wholesale_engine/data/api_usage.json
rentcast_sample.json
config/buybox.json
```

---

## 12. Known gaps, blockers, and unfinished work

**No known failing tests. No known bugs.** 957 tests pass.

**Not built yet:**
1. RentCast provider adapter (`providers/rentcast.py` + `rentcast_schema.py`)
2. Wiring `hunt.py` to write to the decision log
3. Wiring the buy box into `HuntCriteria` / `LeadHunterConfig`
4. `service/` orchestration layer
5. `web/` Flask app + templates
6. `deploy/` systemd service + weekly timer + `requirements.txt` + runbook

**Blockers / unknowns:**
- **RentCast field names are UNVERIFIED.** The mapper design treats an unmatched
  field as *unknown* (never a fabricated default), so wrong key names degrade
  safely — but they need confirming against a live response.
- **UNKNOWN: does `/avm/value` return comparables in the same response?** If yes,
  value + comps is ONE request instead of two — significant at this budget.
  Needs a separate 1-request test after the adapter exists.
- **UNKNOWN: does the free plan hard-stop at 50, or charge overage?** The local
  hard cap protects either way. User should check their dashboard.
- **Claude's sandbox cannot reach `rentcast.io` or `propertyreach.com`** — the
  egress proxy blocks them (`curl` returns `000`, WebFetch returns `EGRESS_BLOCKED`).
  **You cannot read RentCast docs or make live calls from the Claude environment.**
  All live verification happens on the user's VPS.
- **The user has no computer.** The probe was originally designed to run on their
  machine; it must now run on the VPS. **Plan of record: expose it as a one-click
  "verify RentCast connection" button in the dashboard** that spends exactly one
  request, so the user never needs a terminal after initial setup. This was
  proposed and not objected to, but not explicitly confirmed.

---

## 13. EXACT NEXT STEP

**Build step 3 of 6: the RentCast provider adapter.**

Create `wholesale_engine/providers/rentcast_schema.py` and
`wholesale_engine/providers/rentcast.py`, following the exact pattern already
established in `providers/propertyreach.py` / `propertyreach_schema.py`:

- Schema module holds all wire-format constants and the field mapping in one
  place, separating **confirmed** from **unverified**, so finishing it later is
  an edit there rather than a rewrite.
- Adapter uses `SafeHttpClient` with `auth_header="X-Api-Key"`, `auth_scheme=""`,
  `min_interval_seconds=1.0`.
- **Must integrate `QuotaLedger` and `ResponseCache`**: check cache → check quota
  with `require(1)` → make request → record success only → cache the response.
- Map onto the **existing** `Lead`, `Property`, `Comp` models. **Do not create a
  second deal-analysis system.**
- Register as `--source rentcast` in `providers/registry.py` with
  `required_settings=("RENTCAST_API_KEY",)`.
- Declare capabilities honestly: SEARCH, PROPERTY, OWNER, VALUATION, COMPS at
  minimum (owner/tax/distress data come free inside `/properties` records).
- Mocked unit tests only — **no live calls from the Claude environment.**
  Add `tests/test_rentcast.py`.

Then run `python3 -m unittest discover -s tests` and confirm all tests pass.

**Also ask the user:**
1. Did Tailscale set up successfully on the VPS and iPhone?
2. Confirm the "verify RentCast connection" button approach (probe moves into
   the dashboard, so no terminal is needed after setup).

After step 3: `service/` layer → Flask UI → systemd deployment.

---

## 14. Useful commands

```bash
python3 -m unittest discover -s tests                 # 957 tests
python3 -m wholesale_engine.main --mode TEST --health --provider-status \
    --integrations --security-audit --budget-status
python3 -m wholesale_engine.main --mode TEST --daily
python3 -m wholesale_engine.main --hunt --source csv --states FL,TX,MO \
    --min-price 0 --max-price 2200000
python3 scripts/rentcast_probe.py --zip 33607 --dry-run
```

**Budget defaults:** `MAX_RAW_LEADS=1000`, `MAX_RESEARCH=100`, `MAX_REACH=100`,
`MAX_COMPS=25`, `MAX_SKIP_TRACES=10`, `MAX_RENTCAST=50`

**All env vars:** `WHOLESALE_MODE`, `DATA_PROVIDER`, `COMPS_PROVIDER`,
`SKIP_TRACE_PROVIDER`, `NOTIFICATION_PROVIDER`, `MAX_RAW_LEADS`, `MAX_RESEARCH`,
`MAX_COMPS`, `MAX_SKIP_TRACES`, `MAX_REACH`, `MAX_RENTCAST`, `MIN_PROPERTY_PRICE`,
`MAX_PROPERTY_PRICE`, `RENTCAST_API_KEY`, `RENTCAST_BASE_URL`,
`PROPERTYREACH_API_KEY`, `PROPERTYREACH_BASE_URL`, `PROPERTY_DATA_API_KEY`,
`PROPERTY_DATA_BASE_URL`, `PUBLIC_RECORDS_API_KEY`, `COMPS_API_KEY`,
`SKIP_TRACE_API_KEY`, `BUYBOX_PATH`
