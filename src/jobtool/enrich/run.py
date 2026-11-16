"""Read the postings at the front of the queue: fetch, extract, and re-score.

Only the head of the queue is read. Reading everything a source lists would
cost money and hammer job sites for postings that will be filtered or never
reached. As jobs leave the queue, the ones behind them are read in turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from jobtool import db, llm, queue
from jobtool.config import Config
from jobtool.db import JobText, Listing, Requirements
from jobtool.enrich import posting as posting_reader
from jobtool.enrich.posting import Getter, http_get
from jobtool.enrich.requirements import Extracted, extract
from jobtool.llm import LlmProblem, Reader
from jobtool.profile.models import Profile
from jobtool.sync import apply_assessment, sources_of

ReaderFactory = Callable[[Session], Reader | None]


@dataclass
class EnrichResult:
    fetched: int = 0
    read: int = 0
    closed: int = 0
    filtered: int = 0
    unreadable: int = 0
    # Listings worked on, whether that meant fetching, reading, or both.
    attempted: int = 0
    problem: LlmProblem | None = None


def make_reader(session: Session, home: Path, model: str) -> Reader | None:
    """A reader for the configured model, or None while Claude cannot be used.

    A key that is missing, or was rejected until the user acts, is not tried again
    on every job; the alert in the app says what to do.
    """
    if llm.blocked(session, home):
        return None
    try:
        return llm.reader_for(home, model)
    except LlmProblem:
        return None


def _text_row(session: Session, listing_id: int) -> JobText | None:
    return session.exec(select(JobText).where(JobText.listing_id == listing_id)).first()


def _requirements_row(session: Session, listing_id: int) -> Requirements | None:
    return session.exec(
        select(Requirements).where(Requirements.listing_id == listing_id)
    ).first()


def pending(
    session: Session, listing: Listing, config: Config, *, have_reader: bool, at: datetime
) -> bool:
    """True when this listing still has something to fetch or to read."""
    row = _text_row(session, listing.id)
    if row is None or row.fetched_at < at - timedelta(days=config.llm.refresh_days):
        return True
    if row.status != posting_reader.OK or not have_reader:
        return False
    read = _requirements_row(session, listing.id)
    return read is None or read.content_hash != row.content_hash


def read_state(session: Session, listing: Listing) -> dict:
    """What the card needs to show about the posting: status, requirements, when."""
    row = _text_row(session, listing.id)
    read = _requirements_row(session, listing.id)
    requirements = None
    if read is not None:
        try:
            requirements = Extracted.model_validate_json(read.data_json)
        except ValueError:
            requirements = None
    return {
        "status": row.status if row else None,
        "error": row.error if row else "",
        "fetched_at": row.fetched_at if row else None,
        "requirements": requirements,
    }


def enrich_listing(
    session: Session,
    listing: Listing,
    *,
    config: Config,
    profile: Profile | None,
    reader: Reader | None,
    get: Getter = http_get,
    today: date | None = None,
    at: datetime | None = None,
    force: bool = False,
    result: EnrichResult | None = None,
) -> EnrichResult:
    """Fetch and read one posting, then re-score the listing.

    Raises :class:`LlmProblem` when Claude fails; the fetched text is saved first,
    so nothing is fetched twice because of it.
    """
    result = result or EnrichResult()
    at = at or datetime.now()
    row = _text_row(session, listing.id)
    stale = row is None or row.fetched_at < at - timedelta(days=config.llm.refresh_days)
    if stale or force:
        found = posting_reader.fetch_posting(listing.url, get)
        result.fetched += 1
        if row is None:
            row = JobText(listing_id=listing.id, status=found.status, fetched_at=at)
        # A passing failure (site down, blocked) must not throw away text read earlier.
        settled = found.status in (posting_reader.OK, posting_reader.DEAD)
        if settled or row.status != posting_reader.OK:
            row.status, row.ats, row.error = found.status, found.ats, found.error
            if found.status == posting_reader.OK:
                row.text, row.content_hash = found.text, found.content_hash
        else:
            row.error = found.error
        row.fetched_at = at
        session.add(row)
        session.commit()
        if found.status == posting_reader.DEAD:
            result.closed += 1
        elif found.status != posting_reader.OK:
            result.unreadable += 1

    read = _requirements_row(session, listing.id)
    requirements: Extracted | None = None
    if read is not None:
        try:
            requirements = Extracted.model_validate_json(read.data_json)
        except ValueError:
            read_is_usable = False
        else:
            read_is_usable = read.content_hash == row.content_hash
    else:
        read_is_usable = False

    problem: LlmProblem | None = None
    if row.status == posting_reader.OK and reader is not None and not read_is_usable:
        try:
            reply = extract(
                reader, title=listing.title, company=listing.company, text=row.text,
                profile=profile,
            )  # fmt: skip
        except LlmProblem as exc:
            problem = exc
            llm.record_problem(session, exc)
        else:
            requirements = reply.parsed
            read = read or Requirements(
                listing_id=listing.id, content_hash="", data_json="", model="", created_at=at
            )
            read.content_hash, read.model, read.created_at = row.content_hash, reply.model, at
            read.data_json = requirements.model_dump_json()
            session.add(read)
            llm.log_usage(session, "read posting", reply)
            llm.record_ok(session)
            result.read += 1

    was = listing.state
    apply_assessment(
        listing, sources_of(session, listing.id), config=config, profile=profile,
        today=today or at.date(), requirements=requirements, posting_status=row.status,
    )  # fmt: skip
    if was == db.MAIN and listing.state == db.FILTERED:
        result.filtered += 1
    session.add(listing)
    session.commit()
    if problem is not None:
        raise problem
    return result


def enrich_queue(
    session: Session,
    *,
    config: Config,
    profile: Profile | None,
    reader: Reader | None,
    get: Getter = http_get,
    limit: int | None = None,
    budget: int | None = None,
    today: date | None = None,
    at: datetime | None = None,
) -> EnrichResult:
    """Read ahead until the first ``limit`` jobs in the queue are all read.

    Reading a job can filter it out, which brings another job into the first
    ``limit``, so this repeats; a ceiling keeps one run from going on forever.
    """
    limit = config.llm.read_ahead if limit is None else limit
    at = at or datetime.now()
    result = EnrichResult()
    budget = max(limit * 4, 4) if budget is None else budget
    tried: set[int] = set()
    while budget > 0:
        head = queue.head(session, limit)
        todo = [
            listing
            for listing in head
            if listing.id not in tried
            and pending(session, listing, config, have_reader=reader is not None, at=at)
        ]
        if not todo:
            break
        for listing in todo:
            if budget <= 0:
                break
            budget -= 1
            result.attempted += 1
            tried.add(listing.id)
            try:
                enrich_listing(
                    session, listing, config=config, profile=profile, reader=reader, get=get,
                    today=today, at=at, result=result,
                )  # fmt: skip
            except LlmProblem as problem:
                # Keep checking that postings are open, but stop asking Claude this run.
                result.problem = problem
                reader = None
    return result
