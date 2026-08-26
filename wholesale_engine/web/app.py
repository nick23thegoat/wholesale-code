"""Routes. Read-only, and every one of them goes through the service.

There is no SQL in this file, no scoring, no filtering and no argparse. A route
reads query parameters, calls one or two :class:`EngineService` methods, and
hands the result to a template. That constraint is the point of the milestone:
if a page needs something the service cannot answer, the fix is a service
method, not a query here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, abort, redirect, render_template, request, url_for

from ..buybox import (
    APPLIED_FIELDS,
    DESCRIPTIVE_FIELDS,
    NOT_IMPLEMENTED_FIELDS,
    NOT_ROUTED_FIELDS,
)
from ..service import EngineService
from ..storage import ACCEPTED, INCOMPLETE, LEAD_STATUSES, REJECTED, SORT_KEYS, SearchQuery
from .formatting import FILTERS

#: Only ever bound to the loopback address by :func:`run_dev_server`. A public
#: bind on an app with no authentication is refused rather than warned about.
SAFE_HOSTS = ("127.0.0.1", "localhost", "::1")

#: Rows per page. Small on purpose: this is read on a phone.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _float(name: str) -> Optional[float]:
    """One query parameter as a number, or ``None`` if absent or unusable.

    A malformed filter is treated as no filter. Returning 400 for a typo in a
    URL would be technically defensible and useless on a phone, where the URL
    is usually something you edited by hand.
    """
    raw = (request.args.get(name) or "").strip().replace(",", "").replace("$", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = _float(name)
    return int(value) if value is not None else default


def _csv(name: str) -> tuple:
    raw = (request.args.get(name) or "").strip()
    return tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else ()


def build_query() -> SearchQuery:
    """Query parameters into the storage layer's own :class:`SearchQuery`.

    Only fields ``SearchQuery`` already supports are read. Nothing new is
    filtered here — this is the same object the CLI builds, populated from a
    URL instead of a Namespace.
    """
    sort_by = (request.args.get("sort_by") or "priority_score").strip()
    if sort_by not in SORT_KEYS:
        sort_by = "priority_score"

    # `or DEFAULT_LIMIT` would be wrong here: it makes ?limit=0 mean "fifty"
    # rather than clamping, so a deliberate zero silently becomes a full page.
    limit = _int("limit", DEFAULT_LIMIT)
    limit = max(1, min(DEFAULT_LIMIT if limit is None else limit, MAX_LIMIT))

    statuses = tuple(s for s in _csv("status") if s in LEAD_STATUSES)

    return SearchQuery(
        states=_csv("states"),
        cities=_csv("cities"),
        counties=_csv("counties"),
        zip_codes=_csv("zip_codes"),
        property_types=_csv("property_types"),
        min_price=_float("min_price"),
        max_price=_float("max_price"),
        min_arv=_float("min_arv"),
        max_arv=_float("max_arv"),
        min_fee=_float("min_fee"),
        min_lead_score=_float("min_lead_score"),
        min_deal_score=_float("min_deal_score"),
        min_priority_score=_float("min_priority_score"),
        statuses=statuses,
        exclude_closed=request.args.get("open_only") == "1",
        text=(request.args.get("q") or "").strip(),
        limit=limit,
        sort_by=sort_by,
    )


def create_app(
    db_path: Optional[Path] = None,
    buy_box_path: Optional[Path] = None,
    service: Optional[EngineService] = None,
) -> Flask:
    """Build the dashboard.

    ``service`` is injectable so tests can point the whole app at a temporary
    database without touching the environment. In normal use the app builds
    its own, which opens and closes a connection per request — the behaviour a
    threaded server needs, since a SQLite connection is not safe to share
    across threads.
    """
    app = Flask(__name__)
    app.config["ENGINE_SERVICE"] = service or EngineService(
        db_path=db_path, buy_box_path=buy_box_path
    )
    # Templates are read-only views of data this process already trusts, but
    # autoescaping stays on: an address or an owner name is provider text, and
    # provider text is not ours to render raw.
    app.jinja_env.autoescape = True
    app.jinja_env.filters.update(FILTERS)

    def engine() -> EngineService:
        return app.config["ENGINE_SERVICE"]

    @app.context_processor
    def _nav() -> Dict[str, Any]:
        return {"active_page": request.endpoint or ""}

    # ------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> Any:
        """Liveness only. Touches no database, so it stays cheap and honest."""
        return {"status": "ok", "readonly": True}

    @app.get("/")
    def home() -> Any:
        return redirect(url_for("leads"))

    # ------------------------------------------------------------------

    @app.get("/leads")
    def leads() -> Any:
        query = build_query()
        rows = engine().search_leads(query)
        return render_template(
            "leads.html",
            rows=rows,
            query=query,
            statuses=LEAD_STATUSES,
            sort_keys=SORT_KEYS,
            filtered=bool(
                request.args and any(v for v in request.args.values() if v)
            ),
        )

    @app.get("/leads/<path:dedupe_key>")
    def property_detail(dedupe_key: str) -> Any:
        row = engine().get_property(dedupe_key)
        if row is None:
            abort(404, description=f"No stored property matches '{dedupe_key}'.")
        return render_template(
            "property.html",
            row=row,
            decisions=engine().decisions_for_property(row.dedupe_key),
        )

    # ------------------------------------------------------------------

    @app.get("/runs")
    def runs() -> Any:
        history = engine().run_history(limit=_int("limit", 25) or 25)
        # The runs table carries accepted and rejected but not incomplete, so
        # the third number is counted from the decisions themselves.
        counts = {
            run.run_id: engine().run_outcome_counts(run.run_id)
            for run in history if run.run_id is not None
        }
        return render_template("runs.html", history=history, counts=counts,
                               accepted=ACCEPTED, rejected=REJECTED,
                               incomplete=INCOMPLETE)

    @app.get("/runs/<int:run_id>")
    def run_detail(run_id: int) -> Any:
        run = engine().get_run(run_id)
        if run is None:
            abort(404, description=f"No run {run_id}.")
        return render_template(
            "run.html",
            run=run,
            counts=engine().run_outcome_counts(run_id),
            rejections=engine().rejections_for_run(run_id),
            accepted=ACCEPTED, rejected=REJECTED, incomplete=INCOMPLETE,
        )

    # ------------------------------------------------------------------

    @app.get("/buybox")
    def buybox() -> Any:
        """The buy box, split by what actually filters and what does not.

        The split is the whole reason this page exists. A saved setting that
        looks like a filter and is not is how you conclude your market has no
        deals in it when really the bedroom minimum you set was never applied.
        """
        view = engine().read_buy_box()
        box = view.buy_box

        def rows(names) -> List[Dict[str, Any]]:
            out = []
            for name in names:
                value = getattr(box, name, None)
                out.append({
                    "name": name,
                    "value": value,
                    "set": bool(value) if not isinstance(value, (int, float))
                    else value is not None,
                })
            return out

        return render_template(
            "buybox.html",
            view=view,
            box=box,
            applied=rows(APPLIED_FIELDS),
            descriptive=rows(DESCRIPTIVE_FIELDS),
            not_implemented=rows(NOT_IMPLEMENTED_FIELDS),
            not_routed=rows(NOT_ROUTED_FIELDS),
        )

    # ------------------------------------------------------------------

    @app.errorhandler(404)
    def not_found(error: Any) -> Any:
        return render_template(
            "error.html", code=404,
            message=getattr(error, "description", "Not found."),
        ), 404

    return app


def run_dev_server(
    host: str = "127.0.0.1", port: int = 8000, service: Optional[EngineService] = None
) -> None:
    """Serve on the loopback address.

    A non-loopback host is refused outright rather than warned about. This
    application has no authentication, so binding it where other machines can
    reach it hands every lead and every owner name to whoever asks. The
    intended deployment is behind Tailscale, and even there an authentication
    layer belongs in front of it first.
    """
    if host not in SAFE_HOSTS:
        raise ValueError(
            f"refusing to bind {host}: this dashboard has no authentication, so "
            "binding anything other than the loopback address exposes every "
            "lead and owner name to anyone who can reach the port. Put it "
            "behind Tailscale and an auth layer, then serve it with a real "
            "WSGI server rather than this one."
        )
    create_app(service=service).run(host=host, port=port, debug=False)
