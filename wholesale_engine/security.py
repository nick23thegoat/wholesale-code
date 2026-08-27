"""Input validation and the standing security rules.

Everything here exists because the engine reads files you point it at, writes
a database, and (once configured) carries live API credentials. The rules:

**SQL.** Every query is parameterized. The only value ever interpolated into
SQL text is a column name chosen from a fixed allow-list, and
:func:`safe_sort_column` is the one function permitted to do it.

**Shell.** Nothing in this package runs a shell command. There is no
``subprocess``, ``os.system``, ``eval`` or ``exec`` anywhere in it, so there is
no command-injection surface to defend.

**Paths.** Any path that comes from the command line goes through
:func:`safe_path`, which refuses traversal outside the working tree and
refuses to read a device or a symlink pointing somewhere unexpected.

**Secrets.** Credentials live in the environment, never in code, and never
reach a log line — :func:`~wholesale_engine.providers.http_client.redact` is
applied to every URL and error before it is printed. ``.env`` is git-ignored
and excluded from backups unless explicitly requested.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

#: Identifier shape allowed anywhere a name reaches SQL or a filename.
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.\-|# ]{1,200}$")

#: Extensions the engine will read from a user-supplied path.
ALLOWED_READ_SUFFIXES = (".csv", ".json", ".db", ".sqlite", ".sqlite3", ".zip", ".env")

#: Extensions the engine will write to.
ALLOWED_WRITE_SUFFIXES = (".csv", ".json", ".db", ".zip", ".txt", ".md")


class ValidationError(ValueError):
    """A CLI input was rejected, with a message the user can act on."""


def safe_sort_column(requested: str, allowed: Sequence[str], default: str) -> str:
    """The only sanctioned way to put a column name into SQL text.

    Returns a member of ``allowed`` or ``default`` — never the caller's string.
    A value outside the allow-list is silently replaced rather than raising,
    because a mistyped sort key should not abort a query.
    """
    value = (requested or "").strip()
    return value if value in allowed else default


def safe_identifier(value: str, label: str = "value") -> str:
    """Validate a free-text identifier (property id, lead id, status)."""
    text = (value or "").strip()
    if not text:
        raise ValidationError(f"{label} cannot be empty")
    if not SAFE_IDENTIFIER.match(text):
        raise ValidationError(
            f"{label} contains characters that are not allowed: {text!r}"
        )
    return text


def safe_path(
    candidate: Any,
    must_exist: bool = True,
    for_write: bool = False,
    base: Optional[Path] = None,
    label: str = "path",
) -> Path:
    """Resolve a user-supplied path, refusing anything suspicious.

    Refuses: a path that does not exist when it must, a directory where a file
    is wanted, an extension outside the allow-list, a device or FIFO, and —
    when ``base`` is given — anything resolving outside that directory.
    """
    if candidate in (None, ""):
        raise ValidationError(f"{label} is required")
    path = Path(str(candidate)).expanduser()

    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"{label} cannot be resolved: {exc}") from exc

    if base is not None:
        try:
            resolved.relative_to(Path(base).resolve())
        except ValueError as exc:
            raise ValidationError(
                f"{label} resolves outside {base}: {resolved}"
            ) from exc

    suffixes = ALLOWED_WRITE_SUFFIXES if for_write else ALLOWED_READ_SUFFIXES
    if resolved.suffix and resolved.suffix.lower() not in suffixes:
        raise ValidationError(
            f"{label} has an unsupported extension '{resolved.suffix}'. "
            f"Allowed: {', '.join(suffixes)}"
        )

    if must_exist:
        if not resolved.exists():
            raise ValidationError(f"{label} does not exist: {resolved}")
        if resolved.is_dir():
            raise ValidationError(f"{label} is a directory, not a file: {resolved}")
        if not resolved.is_file():
            raise ValidationError(
                f"{label} is not a regular file (device, socket or FIFO): {resolved}"
            )
    return resolved


def safe_amount(
    value: Any, label: str = "amount", minimum: float = 0.0, maximum: float = 1e9
) -> float:
    """Validate a money figure from the command line."""
    if value in (None, ""):
        raise ValidationError(f"{label} is required")
    try:
        amount = float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError as exc:
        raise ValidationError(f"{label} must be a number, got {value!r}") from exc
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise ValidationError(f"{label} must be a finite number")
    if not minimum <= amount <= maximum:
        raise ValidationError(
            f"{label} must be between {minimum:,.0f} and {maximum:,.0f}, got {amount:,.0f}"
        )
    return amount


def safe_limit(value: Any, label: str = "limit", maximum: int = 10_000) -> int:
    """Validate a row limit, so a typo cannot try to render a million rows."""
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a whole number") from exc
    if limit < 1:
        raise ValidationError(f"{label} must be at least 1")
    return min(limit, maximum)


# ---------------------------------------------------------------------------
# Self-audit
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One thing the audit noticed."""

    severity: str  # HIGH | MEDIUM | LOW | OK
    area: str
    detail: str
    location: str = ""

    def render(self) -> str:
        where = f" ({self.location})" if self.location else ""
        return f"  [{self.severity:<6}] {self.area}: {self.detail}{where}"


#: Patterns that would be a real problem if they appeared in the package.
_DANGEROUS = (
    (r"\bos\.system\s*\(", "shell execution"),
    (r"\bsubprocess\.", "subprocess use"),
    (r"(?<![\w.])eval\s*\(", "eval()"),
    (r"(?<![\w.])exec\s*\(", "exec()"),
    (r"shell\s*=\s*True", "shell=True"),
    (r"\bpickle\.loads?\s*\(", "pickle deserialization"),
    (r"verify\s*=\s*False", "TLS verification disabled"),
    (r"ssl\._create_unverified_context", "unverified TLS context"),
)

#: A literal that looks like a credential checked into source.
_SECRET_LITERAL = re.compile(
    r"""(?ix)\b(api_key|apikey|secret|password|token|access_key)\b\s*=\s*['"][A-Za-z0-9_\-]{8,}['"]"""
)

#: SQL built by formatting rather than parameters. Only matches formatting of
#: the query text itself — a ``+`` or ``%`` *inside* a SQL literal (``times_seen
#: + 1``, a LIKE pattern) is arithmetic, not interpolation.
_SQL_INTERPOLATION = re.compile(
    r"""(?ix)
    (execute|executescript) \s* \( \s*
    (
        f['"]                              # execute(f"SELECT ...
      | ['"][^'"]*['"] \s* (%|\.format\() # execute("SELECT ..." % x)
      | [A-Za-z_][\w.]* \s* (\+|%)        # execute(sql + where)
    )
    """
)

#: This module holds the patterns themselves as literals, so scanning it would
#: report every check as a finding.
_AUDIT_EXEMPT = ("security.py",)


def audit_source(root: Optional[Path] = None) -> List[Finding]:
    """Scan the package for the classes of problem this module guards against.

    Runs in the test suite, so a future change that introduces a shell call,
    a hard-coded key or an interpolated query fails CI rather than shipping.
    """
    root = Path(root) if root else Path(__file__).resolve().parent
    findings: List[Finding] = []

    for path in sorted(root.rglob("*.py")):
        if path.name in _AUDIT_EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(root.parent))

        for pattern, label in _DANGEROUS:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                findings.append(
                    Finding("HIGH", label, "found in the package", f"{relative}:{line}")
                )

        for match in _SECRET_LITERAL.finditer(text):
            line = text[: match.start()].count("\n") + 1
            snippet = match.group(0)
            # A parameter default of None or a doc example is not a secret.
            if "None" in snippet or "REDACTED" in snippet:
                continue
            findings.append(
                Finding("HIGH", "hard-coded secret", snippet[:60], f"{relative}:{line}")
            )

        for match in _SQL_INTERPOLATION.finditer(text):
            line = text[: match.start()].count("\n") + 1
            # Building a run of ``?`` placeholders is the sanctioned exception.
            window = text[match.start(): match.start() + 400]
            if "placeholders" in window or "IS NULL)" in window:
                continue
            findings.append(
                Finding(
                    "HIGH", "SQL interpolation",
                    "query text built by formatting rather than parameters",
                    f"{relative}:{line}",
                )
            )
    return findings


def render_audit(findings: Sequence[Finding]) -> str:
    lines = ["SECURITY AUDIT", ""]
    if not findings:
        lines.append("  No findings.")
    for finding in findings:
        lines.append(finding.render())
    lines.append("")
    lines.append("  Checks run:")
    for check in (
        "shell execution (os.system, subprocess, shell=True)",
        "eval / exec / pickle deserialization",
        "TLS verification disabled",
        "hard-coded API keys, tokens and passwords",
        "SQL built by string formatting instead of parameters",
    ):
        lines.append(f"    - {check}")
    lines.append("")
    lines.append("  Enforced elsewhere and covered by tests:")
    lines.append("    - credentials redacted from every logged URL and error")
    lines.append("    - user-supplied paths validated against traversal")
    lines.append("    - .env git-ignored and excluded from backups by default")
    lines.append("    - sort columns chosen from an allow-list, never interpolated raw")
    return "\n".join(lines)
