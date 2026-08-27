"""BatchData wire format: endpoints, auth, and skip-trace field mapping.

CONFIRMED facts come from BatchData's published documentation and help
centre. UNVERIFIED facts are documented field *names* never checked against a
live response from this account. NOT KNOWN means the shape is genuinely
unknown and the adapter says so rather than filling it in.

Sources:

* https://help.batchservice.com/en/articles/9887341 — authentication
* https://batchdata.io/blog/implement-bulk-skip-tracing-batchdata-api
* https://batchdata.io/blog/clean-verify-crm-data-batchdata-skip-tracing-api
"""

from __future__ import annotations

#: CONFIRMED — from BatchData's help centre and API documentation.
BASE_URL = "https://api.batchdata.com/api/v1"
AUTH_HEADER = "Authorization"
AUTH_SCHEME = ""  # raw token, no "Bearer" prefix, per BatchData's own docs
API_KEY_VAR = "BATCHDATA_API_KEY"

#: CONFIRMED — the skip-trace endpoint. Single and bulk share one path; the
#: difference is how many entries the ``requests`` array carries.
SKIP_TRACE_PATH = "property/skip-trace"
SKIP_TRACE_BULK_PATH = "property/skip-trace"

#: CONFIRMED — bulk limit, from BatchData's documentation.
MAX_BULK_PROPERTIES = 100

#: CONFIRMED — token types. A *server* token is what this adapter needs. A
#: *sandbox* token returns mock data at no cost, which is the right way to
#: prove the wiring before spending on a real batch.
TOKEN_TYPES = ("server", "client", "sandbox")

#: Seconds between requests. BatchData publishes no per-second limit for this
#: endpoint, so the client stays deliberately polite.
MIN_SECONDS_BETWEEN_CALLS = 1.0

#: NOT KNOWN — BatchData's per-lookup price depends on your contract. It is
#: left at zero rather than invented; set SKIP_TRACE_COST_PER_LOOKUP in .env
#: with the rate from your own agreement and the cost report will use it.
COST_VAR = "SKIP_TRACE_COST_PER_LOOKUP"

#: UNVERIFIED — request field names.
REQUEST_FIELD_MAP_UNVERIFIED = {
    "address": "address",
    "city": "city",
    "state": "state",
    "zip_code": "zip_code",
}

#: UNVERIFIED — response field names. The adapter tries each candidate in
#: order and leaves the field blank when none match. A blank phone is a
#: correct answer; a guessed one is a call to a stranger.
RESPONSE_FIELDS_CONFIRMED = False
RESULT_LIST_KEYS = ("results", "data", "persons")
PHONE_CANDIDATE_KEYS = ("phones", "phone_numbers", "phoneNumbers")
EMAIL_CANDIDATE_KEYS = ("emails", "email_addresses", "emailAddresses")
MAILING_ADDRESS_CANDIDATE_KEYS = ("mailing_address", "mailingAddress")
CONFIDENCE_CANDIDATE_KEYS = ("confidence_score", "confidenceScore", "score", "confidence")

#: UNVERIFIED — the key inside one phone/email entry that carries the value
#: itself. BatchData's entries are objects, not bare strings, and which key
#: holds the number has not been checked against a live response.
PHONE_VALUE_KEYS = ("number", "phone", "phoneNumber", "phone_number", "value")
PHONE_TYPE_KEYS = ("type", "phoneType", "phone_type", "lineType", "line_type")
EMAIL_VALUE_KEYS = ("address", "email", "emailAddress", "email_address", "value")

#: NOT KNOWN — BatchData documents a DNC scrub endpoint, but this path has
#: not been confirmed and it is *not* called by the adapter. DNC scrubbing is
#: a compliance step you must put in place deliberately, not a side effect of
#: a skip trace.
DNC_SCRUB_PATH_UNVERIFIED = "dnc/scrub"
