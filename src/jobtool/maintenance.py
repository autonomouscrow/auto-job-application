"""Backups and CSV export."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from pathlib import Path

from sqlmodel import Session, select

from jobtool.db import Application, Listing, db_path

KEEP_BACKUPS = 14


def backup(home: Path, today: date | None = None) -> Path | None:
    """Copy the database into ``data/backups``, once per day. Returns the new file."""
    source = db_path(home)
    if not source.exists():
        return None
    folder = source.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"jobs-{(today or date.today()).isoformat()}.db"
    if target.exists():
        return None
    # The sqlite backup API copies a consistent snapshot even while the app is running.
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    for old in sorted(folder.glob("jobs-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return target


COLUMNS = [
    "company", "title", "status", "applied_at", "role_type", "score", "locations",
    "url", "next_followup", "notes",
]  # fmt: skip


def export_csv(session: Session) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    rows = session.exec(
        select(Application, Listing)
        .join(Listing, Listing.id == Application.listing_id)
        .order_by(Application.applied_at)
    ).all()
    for application, listing in rows:
        writer.writerow(
            [
                listing.company,
                listing.title,
                application.status,
                application.applied_at.isoformat(timespec="minutes"),
                listing.role_type,
                listing.score,
                "; ".join(listing.locations),
                listing.url,
                application.next_followup.isoformat() if application.next_followup else "",
                application.notes,
            ]
        )
    return buffer.getvalue()
