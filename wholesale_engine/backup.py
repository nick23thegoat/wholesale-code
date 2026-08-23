"""``--backup``: a timestamped copy of everything you would hate to lose.

Backs up the SQLite database, the non-secret configuration, and the generated
reports. **Secrets are excluded by default** — a ``.env`` full of live API keys
should not end up in a zip in your Downloads folder because you asked for a
backup. ``include_secrets=True`` is available and is deliberately awkward.

The database is copied through SQLite's own backup API rather than by copying
the file, so a backup taken while the engine is mid-write is still consistent.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Config files worth keeping. Note what is absent: .env.
CONFIG_FILES = (".env.example", "wholesale_engine/config.py")

#: Report files worth keeping.
REPORT_PATTERNS = ("*.csv", "*.json")

#: Never included unless explicitly requested.
SECRET_FILES = (".env", ".env.local", "service-account.json")


@dataclass
class BackupResult:
    """What a backup actually contains."""

    path: Optional[Path] = None
    database_rows: int = 0
    files: List[str] = field(default_factory=list)
    skipped_secrets: List[str] = field(default_factory=list)
    bytes_written: int = 0
    created_at: Optional[datetime] = None

    def render(self) -> str:
        lines = [
            "BACKUP",
            f"  Archive:      {self.path}",
            f"  Size:         {self.bytes_written / 1024:.1f} KB",
            f"  Lead rows:    {self.database_rows}",
            f"  Files:        {len(self.files)}",
        ]
        for name in self.files[:20]:
            lines.append(f"    {name}")
        if len(self.files) > 20:
            lines.append(f"    ... and {len(self.files) - 20} more")
        if self.skipped_secrets:
            lines.append(
                "  Excluded (secrets): " + ", ".join(self.skipped_secrets)
            )
            lines.append(
                "    Pass --include-secrets only if you understand where this "
                "archive is going to end up."
            )
        return "\n".join(lines)


def _safe_name(base: Path, candidate: Path) -> Optional[str]:
    """The archive name for ``candidate``, or None if it escapes ``base``.

    Guards against path traversal: a symlink or a ``..`` component that
    resolves outside the project is never written into the archive.
    """
    try:
        resolved = candidate.resolve()
        base_resolved = base.resolve()
        return str(resolved.relative_to(base_resolved))
    except (ValueError, OSError):
        return None


def backup_database(source: Path, destination: Path) -> int:
    """Copy a SQLite database consistently, even while it is being written.

    Returns the number of lead rows in the copy, as a sanity check.
    """
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return 0
    with sqlite3.connect(str(source)) as origin, sqlite3.connect(str(destination)) as copy:
        origin.backup(copy)
        try:
            return copy.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        except sqlite3.Error:
            return 0


def create_backup(
    database: Path,
    destination_dir: Path,
    project_root: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
    include_secrets: bool = False,
    timestamp: Optional[datetime] = None,
) -> BackupResult:
    """Write a timestamped zip containing the database, config and reports."""
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d-%H%M%S")
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive = destination_dir / f"wholesale-backup-{stamp}.zip"
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent

    result = BackupResult(path=archive, created_at=timestamp or datetime.now())

    # Snapshot the database first, so the archive holds a consistent copy.
    snapshot = destination_dir / f".leads-{stamp}.db"
    result.database_rows = backup_database(Path(database), snapshot)

    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            if snapshot.exists():
                bundle.write(snapshot, "database/leads.db")
                result.files.append("database/leads.db")

            for relative in CONFIG_FILES:
                candidate = root / relative
                if candidate.exists() and candidate.is_file():
                    name = _safe_name(root, candidate)
                    if name:
                        bundle.write(candidate, f"config/{Path(name).name}")
                        result.files.append(f"config/{Path(name).name}")

            for name in SECRET_FILES:
                candidate = root / name
                if not candidate.exists():
                    continue
                if include_secrets:
                    bundle.write(candidate, f"secrets/{name}")
                    result.files.append(f"secrets/{name}")
                else:
                    result.skipped_secrets.append(name)

            if reports_dir:
                reports = Path(reports_dir)
                if reports.exists():
                    for pattern in REPORT_PATTERNS:
                        for candidate in sorted(reports.glob(pattern)):
                            if not candidate.is_file():
                                continue
                            bundle.write(candidate, f"reports/{candidate.name}")
                            result.files.append(f"reports/{candidate.name}")
    finally:
        if snapshot.exists():
            snapshot.unlink()

    result.bytes_written = archive.stat().st_size if archive.exists() else 0
    return result


def restore_database(archive: Path, destination: Path) -> bool:
    """Pull the database out of a backup archive.

    Every member name is validated before extraction — a crafted archive
    cannot write outside ``destination``'s directory.
    """
    archive, destination = Path(archive), Path(destination)
    if not archive.exists():
        return False
    with zipfile.ZipFile(archive) as bundle:
        member = "database/leads.db"
        if member not in bundle.namelist():
            return False
        # Never trust a member name: no absolute paths, no traversal.
        if Path(member).is_absolute() or ".." in Path(member).parts:
            raise ValueError(f"refusing to extract unsafe archive member: {member}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(member) as source, open(destination, "wb") as target:
            shutil.copyfileobj(source, target)
    return True
