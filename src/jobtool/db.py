"""Database tables. One SQLite file under ``<home>/data``."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import NaiveDatetime
from sqlalchemy import UniqueConstraint, event
from sqlmodel import Field, Session, SQLModel, create_engine, select

# Where a listing sits in the review flow.
MAIN, IN_PROGRESS, SET_ASIDE = "main", "in_progress", "set_aside"
REJECTED, APPLIED, FILTERED = "rejected", "applied", "filtered"
STATES = (MAIN, IN_PROGRESS, SET_ASIDE, REJECTED, APPLIED, FILTERED)

# Where an application stands after it was sent.
STATUSES = ("applied", "online assessment", "interview", "offer", "turned down", "withdrawn")


class Source(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True)
    repo: str
    branch: str = "HEAD"
    path: str = "README.md"
    parser: str = "markdown-table"
    default_role: str | None = None
    enabled: bool = True
    etag: str | None = None
    last_synced: NaiveDatetime | None = None
    last_row_count: int = 0
    last_error: str | None = None


class Listing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    company: str
    title: str
    url: str
    company_url: str | None = None
    locations_json: str = "[]"
    terms_json: str = "[]"
    sponsorship: str | None = None
    degrees_json: str = "[]"
    category: str | None = None
    date_posted: date | None = None
    first_seen: NaiveDatetime
    # Sync run that first found this listing. Newer batches go to the front of the queue.
    batch: int = Field(default=0, index=True)
    # False once no source lists it as open any more.
    open: bool = Field(default=True, index=True)
    role_type: str = "off-target"
    score: int = 0
    score_json: str = "{}"
    flags_json: str = "[]"
    state: str = Field(default=MAIN, index=True)
    state_reason: str | None = None
    # Set when the user overrides a filter, so a later sync does not filter it again.
    pinned: bool = False
    note: str = ""
    decided_at: NaiveDatetime | None = None
    norm_company: str = Field(default="", index=True)
    norm_title: str = ""
    repeat_of: int | None = None

    @property
    def locations(self) -> list[str]:
        return json.loads(self.locations_json)

    @property
    def terms(self) -> list[str]:
        return json.loads(self.terms_json)

    @property
    def degrees(self) -> list[str]:
        return json.loads(self.degrees_json)

    @property
    def flags(self) -> list[str]:
        return json.loads(self.flags_json)

    @property
    def score_detail(self) -> dict:
        return json.loads(self.score_json)


class ListingSource(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("listing_id", "source_id"),)
    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    source_id: int = Field(foreign_key="source.id", index=True)
    row_id: str | None = None
    active: bool = True
    missing_count: int = 0
    first_seen: NaiveDatetime


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", unique=True)
    status: str = "applied"
    applied_at: NaiveDatetime
    notes: str = ""
    next_followup: date | None = None


class Event(SQLModel, table=True):
    """Every change the user makes, which gives the timeline and Undo."""

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    at: NaiveDatetime
    kind: str  # "state" or "status"
    from_value: str | None = None
    to_value: str
    note: str = ""
    undone: bool = False


class JobText(SQLModel, table=True):
    """The text of a posting as last fetched from the employer's site."""

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", unique=True, index=True)
    # "ok", "dead", "unreadable", or "error" (see jobtool.enrich.posting).
    status: str
    ats: str = "web"
    fetched_at: NaiveDatetime
    text: str = ""
    content_hash: str = ""
    error: str = ""


class Requirements(SQLModel, table=True):
    """What Claude read out of a posting. Redone only when the posting text changes."""

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", unique=True, index=True)
    content_hash: str
    data_json: str
    model: str
    created_at: NaiveDatetime


class Usage(SQLModel, table=True):
    """One row per Claude API call, so spending can be shown."""

    id: int | None = Field(default=None, primary_key=True)
    at: NaiveDatetime
    purpose: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class Meta(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


def db_path(home: Path) -> Path:
    return home / "data" / "jobs.db"


def open_engine(home: Path):
    path = db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(connection, _record):
        # Lets the page you are looking at load while a background read is saving.
        connection.execute("PRAGMA journal_mode=WAL")

    SQLModel.metadata.create_all(engine)
    return engine


def memory_engine():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def get_meta(session: Session, key: str, default: str = "") -> str:
    row = session.get(Meta, key)
    return row.value if row else default


def set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(Meta, key)
    if row:
        row.value = value
    else:
        row = Meta(key=key, value=value)
    session.add(row)


def sources_by_slug(session: Session) -> dict[str, Source]:
    return {source.slug: source for source in session.exec(select(Source)).all()}
