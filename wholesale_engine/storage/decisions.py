"""Why each property was accepted or rejected, kept where you can query it.

The funnel already decides — cheap filters, lead score, research, comps, deal
score — and each stage already produces reasons. What was missing is that the
reasons lived in memory and died with the run. On a server you review from a
phone days later, that is the difference between "22 leads rejected" and
"22 leads rejected, 14 of them because your minimum lead score is too high".

Two rules:

* **every property the run saw gets a row**, accepted or rejected. A funnel you
  can only see the survivors of is one you cannot tune.
* **the reason is a short, stable phrase; the detail is the specifics.** The
  phrase groups ("below minimum lead score"), the detail explains this one
  property ("lead score 42.0 is below the minimum of 60"). Grouping is what
  makes a rejection summary useful on a small screen.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# --- outcomes --------------------------------------------------------------
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
#: Kept but incomplete — the engine could not decide, which is not a rejection.
INCOMPLETE = "INCOMPLETE"

# --- stages, in funnel order ----------------------------------------------
STAGE_SEARCH = "search"
STAGE_DEDUPE = "dedupe"
STAGE_BUY_BOX = "buy_box"
STAGE_LEAD_SCORE = "lead_score"
STAGE_RESEARCH = "research"
STAGE_COMPS = "comps"
STAGE_DEAL_SCORE = "deal_score"
STAGE_FINAL = "final"

STAGE_ORDER = (
    STAGE_SEARCH, STAGE_DEDUPE, STAGE_BUY_BOX, STAGE_LEAD_SCORE,
    STAGE_RESEARCH, STAGE_COMPS, STAGE_DEAL_SCORE, STAGE_FINAL,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Decision:
    """One accept/reject, on one property, at one stage."""

    dedupe_key: str = ""
    address: str = ""
    stage: str = STAGE_FINAL
    outcome: str = REJECTED
    #: Short, stable, groupable. Not property-specific.
    reason: str = ""
    #: The specifics for this property. Free text.
    detail: str = ""
    lead_score: Optional[float] = None
    deal_score: Optional[float] = None
    lead_row_id: Optional[int] = None
    decided_at: str = field(default_factory=_now)

    @property
    def was_rejected(self) -> bool:
        return self.outcome == REJECTED

    def render(self) -> str:
        mark = {"ACCEPTED": "✓", "REJECTED": "✗", "INCOMPLETE": "?"}.get(
            self.outcome, "·"
        )
        line = f"{mark} {self.address or self.dedupe_key} — {self.reason}"
        return f"{line}\n    {self.detail}" if self.detail else line


@dataclass
class RunRecord:
    """One run of the engine, for the run-history view."""

    run_id: Optional[int] = None
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    trigger: str = "manual"          # manual | scheduled | api
    buy_box: str = ""
    provider: str = ""
    mode: str = "TEST"
    status: str = "RUNNING"          # RUNNING | OK | FAILED | PARTIAL
    api_requests_spent: int = 0
    cache_hits: int = 0
    leads_seen: int = 0
    leads_accepted: int = 0
    leads_rejected: int = 0
    error: str = ""
    notes: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in ("OK", "PARTIAL")


class DecisionLog:
    """Reads and writes the ``runs`` and ``decisions`` tables.

    Takes a live connection rather than a path so it shares the LeadStore's
    transaction and cannot end up pointing at a different database.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(
        self,
        trigger: str = "manual",
        buy_box: str = "",
        provider: str = "",
        mode: str = "TEST",
    ) -> RunRecord:
        record = RunRecord(
            trigger=trigger, buy_box=buy_box, provider=provider, mode=mode
        )
        cursor = self.connection.execute(
            "INSERT INTO runs (started_at, trigger, buy_box, provider, mode, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record.started_at, trigger, buy_box, provider, mode, "RUNNING"),
        )
        self.connection.commit()
        record.run_id = cursor.lastrowid
        return record

    def finish_run(
        self,
        record: RunRecord,
        status: str = "OK",
        error: str = "",
        **counts: int,
    ) -> RunRecord:
        """Close a run out. Always called, including when the run failed.

        A run that crashed still gets a row saying so — a scheduled job that
        silently vanishes is worse than one that records its own failure.
        """
        record.status = status
        record.error = error[:2000]
        record.finished_at = _now()
        for name, value in counts.items():
            if hasattr(record, name):
                setattr(record, name, int(value))
        if record.run_id is None:
            return record
        self.connection.execute(
            "UPDATE runs SET finished_at = ?, status = ?, error = ?, "
            "api_requests_spent = ?, cache_hits = ?, leads_seen = ?, "
            "leads_accepted = ?, leads_rejected = ?, notes = ? WHERE id = ?",
            (
                record.finished_at, record.status, record.error,
                record.api_requests_spent, record.cache_hits, record.leads_seen,
                record.leads_accepted, record.leads_rejected, record.notes,
                record.run_id,
            ),
        )
        self.connection.commit()
        return record

    def recent_runs(self, limit: int = 20) -> List[RunRecord]:
        rows = self.connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_run(self, run_id: int) -> Optional[RunRecord]:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        return self._row_to_run(row) if row else None

    def last_successful_run(self) -> Optional[RunRecord]:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE status IN ('OK', 'PARTIAL') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return self._row_to_run(row) if row else None

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            trigger=row["trigger"],
            buy_box=row["buy_box"],
            provider=row["provider"],
            mode=row["mode"],
            status=row["status"],
            api_requests_spent=row["api_requests_spent"],
            cache_hits=row["cache_hits"],
            leads_seen=row["leads_seen"],
            leads_accepted=row["leads_accepted"],
            leads_rejected=row["leads_rejected"],
            error=row["error"],
            notes=row["notes"],
        )

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def record(self, run_id: Optional[int], decision: Decision) -> None:
        self.connection.execute(
            "INSERT INTO decisions (run_id, lead_row_id, dedupe_key, address, "
            "stage, outcome, reason, detail, lead_score, deal_score, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, decision.lead_row_id, decision.dedupe_key,
                decision.address, decision.stage, decision.outcome,
                decision.reason, decision.detail, decision.lead_score,
                decision.deal_score, decision.decided_at,
            ),
        )

    def record_many(
        self, run_id: Optional[int], decisions: Iterable[Decision]
    ) -> int:
        """Write a batch in one transaction. Returns how many were written."""
        written = 0
        for decision in decisions:
            self.record(run_id, decision)
            written += 1
        self.connection.commit()
        return written

    def for_run(
        self, run_id: int, outcome: str = "", limit: int = 500
    ) -> List[Decision]:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        params: List[Any] = [int(run_id)]
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        sql += " ORDER BY id LIMIT ?"
        params.append(int(limit))
        rows = self.connection.execute(sql, params).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def for_property(self, dedupe_key: str, limit: int = 100) -> List[Decision]:
        """Every decision ever made about one property, newest first.

        This is the "why is this not in my list?" answer, and it spans runs —
        a property rejected three weeks running for the same reason is a buy
        box problem, not a property problem.
        """
        rows = self.connection.execute(
            "SELECT * FROM decisions WHERE dedupe_key = ? "
            "ORDER BY id DESC LIMIT ?",
            (dedupe_key, int(limit)),
        ).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def rejection_summary(self, run_id: int) -> List[tuple]:
        """``(stage, reason, count)`` for one run, commonest first.

        The single most useful view for tuning a buy box: it says which rule is
        actually doing the throwing away.
        """
        rows = self.connection.execute(
            "SELECT stage, reason, COUNT(*) AS n FROM decisions "
            "WHERE run_id = ? AND outcome = ? "
            "GROUP BY stage, reason ORDER BY n DESC",
            (int(run_id), REJECTED),
        ).fetchall()
        return [(row["stage"], row["reason"], row["n"]) for row in rows]

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> Decision:
        return Decision(
            dedupe_key=row["dedupe_key"],
            address=row["address"],
            stage=row["stage"],
            outcome=row["outcome"],
            reason=row["reason"],
            detail=row["detail"],
            lead_score=row["lead_score"],
            deal_score=row["deal_score"],
            lead_row_id=row["lead_row_id"],
            decided_at=row["decided_at"],
        )

    # ------------------------------------------------------------------

    def render_summary(self, run_id: int) -> str:
        """The rejection breakdown, as text. Used by the CLI and the daily log."""
        summary = self.rejection_summary(run_id)
        if not summary:
            return "No rejections recorded for this run."
        total = sum(count for _, _, count in summary)
        lines = [f"WHY {total} PROPERTIES WERE REJECTED", ""]
        for stage, reason, count in summary:
            share = count / total * 100
            lines.append(f"  {count:>4}  ({share:>4.0f}%)  [{stage}] {reason}")
        return "\n".join(lines)
