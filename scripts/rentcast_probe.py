#!/usr/bin/env python3
"""Spend ONE RentCast request and report exactly what came back.

This is the first live call against RentCast, and it exists to answer one
question: *what does a /properties record actually look like?* The adapter's
field mapping is written from the answer, so nothing has to be guessed.

    python3 scripts/rentcast_probe.py --zip 33607 --dry-run   # costs 0
    python3 scripts/rentcast_probe.py --zip 33607             # costs 1

WHAT IT COSTS

One successful request. RentCast bills only successful requests, and a single
/properties call returns up to 500 records, so this one call is worth up to 500
property records — that is the whole point of using `limit` at its maximum.

The script refuses to run twice against the same ZIP once a sample exists.
Re-running would spend a second request to learn nothing new; pass --force if
you actually want a fresh pull.

WHAT IT PRINTS

A field inventory — every key seen, its type, and how many records carried it.
That summary is safe to share: owner names and mailing addresses are redacted
unless you pass --show-values. The raw JSON is written to a file that stays on
your machine.

YOUR KEY

Read from RENTCAST_API_KEY in the environment or the git-ignored .env. It is
never printed, never logged, never written to the sample file, and never sent
anywhere except api.rentcast.io over https.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from wholesale_engine.providers.http_client import (  # noqa: E402
    HttpConfig,
    HttpError,
    SafeHttpClient,
)
from wholesale_engine.settings import load_dotenv  # noqa: E402

# --- Confirmed from RentCast's published documentation ---------------------
BASE_URL = "https://api.rentcast.io/v1"
AUTH_HEADER = "X-Api-Key"
API_KEY_VAR = "RENTCAST_API_KEY"
PROPERTIES_PATH = "properties"
#: RentCast's documented maximum. One request, up to 500 records.
MAX_LIMIT = 500

DEFAULT_SAMPLE = REPO_ROOT / "rentcast_sample.json"

#: Owner identity is redacted in the printed summary. It is your own data, but
#: you will probably paste this summary into a chat window, so it does not go
#: out by default. Note that ``ownerOccupied`` is a boolean flag, not identity,
#: and is deliberately NOT redacted — it is a distress signal we want to see.
SENSITIVE_PREFIXES = ("owner.", "owner[")
SENSITIVE_EXACT = ("owner",)
SENSITIVE_MARKERS = ("mailingaddress", "ownername")


def looks_sensitive(path: str) -> bool:
    lowered = path.lower()
    if lowered in SENSITIVE_EXACT:
        return True
    if lowered.startswith(SENSITIVE_PREFIXES):
        return True
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


#: RentCast keys some objects by year ("2024") or date ("2011-06-14"). Across
#: 500 records that would be hundreds of distinct paths describing one shape,
#: so they collapse to a placeholder and the shape is reported once.
_YEAR = re.compile(r"^(19|20)\d{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def normalize_key(key: str) -> str:
    if _YEAR.match(key):
        return "{year}"
    if _DATE.match(key):
        return "{date}"
    return key


def describe_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        inner = describe_type(value[0]) if value else "?"
        return f"list[{inner}]"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def walk(record: Any, prefix: str = "") -> List[tuple]:
    """Flatten a record into (dotted_path, type, value) triples."""
    found: List[tuple] = []
    if isinstance(record, dict):
        for key, value in record.items():
            safe_key = normalize_key(key)
            path = f"{prefix}.{safe_key}" if prefix else safe_key
            found.append((path, describe_type(value), value))
            if isinstance(value, dict):
                found.extend(walk(value, path))
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                # Describe the shape of the first element only; the rest repeat.
                found.extend(walk(value[0], f"{path}[]"))
    return found


def inventory(records: List[Dict[str, Any]], show_values: bool) -> str:
    """Field names, types and fill rates across every record returned."""
    types: Dict[str, Counter] = defaultdict(Counter)
    present: Counter = Counter()
    samples: Dict[str, Any] = {}

    for record in records:
        # Collapsed paths (history.{date}, taxAssessments.{year}) repeat within
        # one record, so presence is counted per record, not per occurrence.
        # Otherwise FILLED could exceed the record count and read as nonsense.
        seen_here = set()
        for path, kind, value in walk(record):
            types[path][kind] += 1
            if value not in (None, "", [], {}):
                seen_here.add(path)
                samples.setdefault(path, value)
        for path in seen_here:
            present[path] += 1

    total = len(records)
    lines = [
        "FIELD INVENTORY",
        f"  {total} record(s) examined. 'FILLED' is how many carried a real value.",
        "",
        f"  {'FIELD':<44}{'TYPE':<16}{'FILLED':>8}  EXAMPLE",
        "  " + "-" * 108,
    ]
    for path in sorted(types):
        kind = "/".join(sorted(k for k in types[path] if k != "null")) or "null"
        filled = present.get(path, 0)
        if looks_sensitive(path) and not show_values:
            example = "<redacted — pass --show-values>"
        else:
            raw = samples.get(path, "")
            example = str(raw)
            if len(example) > 44:
                example = example[:41] + "..."
        pct = f"{filled}/{total}"
        lines.append(f"  {path:<44}{kind:<16}{pct:>8}  {example}")
    return "\n".join(lines)


def build_params(args: argparse.Namespace) -> Dict[str, Any]:
    params: Dict[str, Any] = {"zipCode": args.zip, "limit": args.limit}
    if args.property_type:
        params["propertyType"] = args.property_type
    if args.offset:
        params["offset"] = args.offset
    return params


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Spend one RentCast request and report the response shape.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--zip", required=True, help="ZIP code to search (required)")
    parser.add_argument(
        "--limit", type=int, default=MAX_LIMIT,
        help=f"records to request, max {MAX_LIMIT} (default: {MAX_LIMIT}). "
             "The cost is one request whatever this is, so leave it at the max.",
    )
    parser.add_argument("--offset", type=int, default=0, help="pagination offset")
    parser.add_argument(
        "--property-type", default=None,
        help="RentCast propertyType filter, e.g. 'Single Family'",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_SAMPLE,
        help=f"where to write the raw JSON (default: {DEFAULT_SAMPLE.name})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show the exact request without sending it. Costs nothing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="pull a fresh sample even though one already exists (spends a request)",
    )
    parser.add_argument(
        "--show-values", action="store_true",
        help="include owner names and mailing addresses in the printed summary",
    )
    args = parser.parse_args()

    if args.limit > MAX_LIMIT:
        print(f"--limit caps at {MAX_LIMIT}; using {MAX_LIMIT}.", file=sys.stderr)
        args.limit = MAX_LIMIT

    load_dotenv()
    api_key = os.environ.get(API_KEY_VAR, "").strip()

    params = build_params(args)
    query = "&".join(f"{k}={v}" for k, v in params.items())
    print("REQUEST")
    print(f"  GET {BASE_URL}/{PROPERTIES_PATH}?{query}")
    print(f"  {AUTH_HEADER}: <your key — not printed>")
    print(f"  cost: 1 request (returns up to {args.limit} records)")
    print()

    if args.dry_run:
        print("DRY RUN — nothing was sent, no request spent.")
        print(f"  key {API_KEY_VAR}: {'found' if api_key else 'NOT SET'}")
        return 0

    if not api_key:
        print(
            f"{API_KEY_VAR} is not set, so nothing was sent.\n\n"
            "  cd " + str(REPO_ROOT) + "\n"
            "  read -rs RENTCAST_KEY        # paste, press Enter, nothing echoes\n"
            "  printf 'RENTCAST_API_KEY=%s\\n' \"$RENTCAST_KEY\" >> .env\n"
            "  unset RENTCAST_KEY && chmod 600 .env",
            file=sys.stderr,
        )
        return 2

    if args.out.exists() and not args.force:
        print(
            f"{args.out} already exists, so no request was spent.\n"
            "  Re-reading the saved sample costs nothing; pass --force to pull a "
            "fresh one.",
            file=sys.stderr,
        )
        payload = json.loads(args.out.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else payload.get("data", [])
        print()
        print(inventory(records, args.show_values))
        return 0

    client = SafeHttpClient(
        BASE_URL,
        api_key,
        # One request; retries only on 5xx/429, which RentCast does not bill.
        HttpConfig(timeout_seconds=30.0, max_retries=3, min_interval_seconds=1.0),
        auth_header=AUTH_HEADER,
        auth_scheme="",          # RentCast sends the bare key, no "Bearer"
    )

    try:
        payload = client.request(PROPERTIES_PATH, params=params, method="GET")
    except HttpError as exc:
        print(f"\nThe request failed: {exc}", file=sys.stderr)
        if exc.is_auth_failure:
            print(
                "  RentCast rejected the key (401/403). Check RENTCAST_API_KEY.\n"
                "  A rejected request is NOT billed, so this cost you nothing.",
                file=sys.stderr,
            )
        elif exc.is_rate_limit:
            print(
                "  Rate limited (429). The key works; the quota does not.\n"
                "  Check your usage at https://app.rentcast.io/app/api",
                file=sys.stderr,
            )
        else:
            print("  Only successful requests are billed.", file=sys.stderr)
        return 1

    records = payload if isinstance(payload, list) else payload.get("data", payload)
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        print(f"Unexpected response shape: {type(records).__name__}", file=sys.stderr)
        records = []

    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"RESPONSE — {len(records)} record(s)")
    print(f"  raw JSON saved to {args.out}")
    print(f"  top-level shape: {'array' if isinstance(payload, list) else 'object'}")
    if isinstance(payload, dict):
        print(f"  top-level keys: {', '.join(sorted(payload))}")
    print()
    if records:
        print(inventory(records, args.show_values))
    print()
    print("SPENT: 1 request. Check your remaining quota at")
    print("  https://app.rentcast.io/app/api")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
