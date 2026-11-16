"""Pull every source into the database.

Rules that matter here:

* The same job found in several repos is stored once, keyed by its cleaned URL.
* A listing is open while at least one source still lists it as open.
* A link that vanishes from a source only counts as closed after it has been
  missing for several syncs in a row, so one bad parse cannot close everything.
* If a source suddenly yields far fewer rows than last time, its sync is
  abandoned and the previous data is kept.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlmodel import Session, select

from jobtool import db
from jobtool.config import Config, SourceConfig
from jobtool.db import JobText, Listing, ListingSource, Requirements, Source
from jobtool.enrich.requirements import Extracted
from jobtool.ingest import registry
from jobtool.ingest.base import ParseError, RawListing
from jobtool.ingest.fetch import Fetched, FetchError
from jobtool.match import assess
from jobtool.normalize import canonical_url, listing_key, norm_company, norm_title
from jobtool.profile.models import Profile

Fetcher = Callable[[SourceConfig, str | None], Fetched]


@dataclass
class SourceResult:
    slug: str
    rows: int = 0
    new: int = 0
    closed: int = 0
    unchanged: bool = False
    error: str | None = None


@dataclass
class SyncResult:
    batch: int
    sources: list[SourceResult] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(source.new for source in self.sources)


def now() -> datetime:
    """Local wall-clock time. This is a single-user app on one machine."""
    return datetime.now()


def ensure_sources(session: Session, config: Config) -> dict[str, Source]:
    """Mirror the configured sources into the database."""
    existing = db.sources_by_slug(session)
    wanted = {source.slug for source in config.sources}
    for item in config.sources:
        row = existing.get(item.slug) or Source(slug=item.slug, repo=item.repo)
        if (row.branch, row.parser) != (item.branch, item.parser):
            row.etag = None
        row.repo, row.branch, row.path = item.repo, item.branch, item.path
        row.parser, row.default_role, row.enabled = item.parser, item.default_role, item.enabled
        session.add(row)
        existing[item.slug] = row
    for slug, row in existing.items():
        if slug not in wanted:
            row.enabled = False
            session.add(row)
    session.flush()
    return existing


def load_reads(session: Session) -> dict[int, tuple[Extracted | None, str | None]]:
    """For every posting fetched so far: what was read from it, and the fetch status."""
    reads: dict[int, tuple[Extracted | None, str | None]] = {}
    for row in session.exec(select(JobText)).all():
        reads[row.listing_id] = (None, row.status)
    for row in session.exec(select(Requirements)).all():
        status = reads.get(row.listing_id, (None, None))[1]
        try:
            reads[row.listing_id] = (Extracted.model_validate_json(row.data_json), status)
        except ValueError:
            continue  # written by an older version: it is read again when next fetched
    return reads


def apply_assessment(
    listing: Listing,
    sources: list[Source],
    *,
    config: Config,
    profile: Profile | None,
    today: date,
    requirements: Extracted | None = None,
    posting_status: str | None = None,
) -> None:
    """Score one listing and move it between the queue and the filtered list.

    A listing can come from several sources. The verdict must not depend on which
    one happened to be read last, so it looks at all of them.
    """
    default_role = next((item.default_role for item in sources if item.default_role), None)
    result = assess(
        title=listing.title,
        locations=listing.locations,
        terms=listing.terms,
        sponsorship=listing.sponsorship,
        degrees=listing.degrees,
        category=listing.category,
        default_role=default_role,
        date_posted=listing.date_posted,
        source_marks_closed=any(registry.has_closed_marker(item.parser) for item in sources),
        config=config,
        profile=profile,
        today=today,
        requirements=requirements,
        posting_status=posting_status,
    )
    listing.role_type = result.role_type
    listing.score = result.score
    listing.score_json = json.dumps(result.detail)
    listing.flags_json = json.dumps(result.flags)
    # Only the automatic states are touched; a decision the user made is never overridden.
    if listing.state in (db.MAIN, db.FILTERED) and not listing.pinned:
        if result.filter_reason:
            listing.state, listing.state_reason = db.FILTERED, result.filter_reason
        else:
            listing.state, listing.state_reason = db.MAIN, None


def sources_of(session: Session, listing_id: int) -> list[Source]:
    return list(
        session.exec(
            select(Source)
            .join(ListingSource, ListingSource.source_id == Source.id)
            .where(ListingSource.listing_id == listing_id)
            .order_by(Source.id)
        ).all()
    )


def _apply_assessment(listing: Listing, ctx: _Context) -> None:
    sources = [ctx.sources[source_id] for source_id in sorted(ctx.listed_by[listing.id])]
    requirements, status = ctx.reads.get(listing.id, (None, None))
    apply_assessment(
        listing, sources, config=ctx.config, profile=ctx.profile, today=ctx.today,
        requirements=requirements, posting_status=status,
    )  # fmt: skip


@dataclass
class _Context:
    config: Config
    profile: Profile | None
    today: date
    batch: int
    at: datetime
    # Filled per source so a large file does not cost one query per row.
    listings: dict[str, Listing] = field(default_factory=dict)
    links: dict[int, ListingSource] = field(default_factory=dict)
    sources: dict[int, Source] = field(default_factory=dict)
    listed_by: dict[int, set[int]] = field(default_factory=dict)
    reads: dict[int, tuple[Extracted | None, str | None]] = field(default_factory=dict)


def _find_repeat(session: Session, listing: Listing) -> int | None:
    """An earlier listing from the same company with the same cleaned-up title."""
    if not listing.norm_company or not listing.norm_title:
        return None
    query = select(Listing).where(
        Listing.norm_company == listing.norm_company,
        Listing.norm_title == listing.norm_title,
        Listing.key != listing.key,
    )
    locations = set(listing.locations)
    for other in session.exec(query).all():
        if not locations or not other.locations or locations & set(other.locations):
            return other.id
    return None


def _upsert(
    session: Session, raw: RawListing, source: Source, ctx: _Context
) -> tuple[int, bool] | None:
    key = listing_key(raw.url)
    listing = ctx.listings.get(key)
    created = listing is None
    if listing is None:
        if not raw.active:
            # Already closed and never seen before: nothing to track.
            return None
        listing = Listing(
            key=key,
            company=raw.company,
            title=raw.title,
            url=canonical_url(raw.url),
            first_seen=ctx.at,
            batch=ctx.batch,
            norm_company=norm_company(raw.company),
            norm_title=norm_title(raw.title),
        )
    # Richer sources fill gaps left by poorer ones, but never erase what is known.
    # A row its own source calls closed says nothing reliable about the job today
    # (employers reuse a posting's URL from one year to the next), so it adds no details.
    if raw.active:
        listing.company_url = listing.company_url or raw.company_url
        if raw.locations and (created or not listing.locations):
            listing.locations_json = json.dumps(raw.locations)
        if raw.terms:
            listing.terms_json = json.dumps(raw.terms)
        if raw.degrees:
            listing.degrees_json = json.dumps(raw.degrees)
        listing.sponsorship = raw.sponsorship or listing.sponsorship
        listing.category = raw.category or listing.category
        listing.date_posted = raw.date_posted or listing.date_posted
    if created:
        session.add(listing)
        session.flush()
        ctx.listings[key] = listing
        listing.repeat_of = _find_repeat(session, listing)

    link = ctx.links.get(listing.id)
    if link is None:
        link = ListingSource(listing_id=listing.id, source_id=source.id, first_seen=ctx.at)
        ctx.links[listing.id] = link
        ctx.listed_by.setdefault(listing.id, set()).add(source.id)
    link.row_id, link.active, link.missing_count = raw.row_id, raw.active, 0
    session.add(link)

    _apply_assessment(listing, ctx)
    session.add(listing)
    return listing.id, created


def _refresh_open(session: Session, listing_ids: set[int]) -> int:
    """Recompute ``open`` for the given listings. Returns how many just closed."""
    closed = 0
    for listing_id in listing_ids:
        listing = session.get(Listing, listing_id)
        if listing is None:
            continue
        links = session.exec(
            select(ListingSource).where(ListingSource.listing_id == listing_id)
        ).all()
        is_open = any(link.active for link in links)
        if listing.open and not is_open:
            closed += 1
        listing.open = is_open
        session.add(listing)
    return closed


def sync_source(
    session: Session, source: Source, ctx: _Context, fetcher: Fetcher
) -> SourceResult:
    result = SourceResult(slug=source.slug)
    item = SourceConfig(
        repo=source.repo, branch=source.branch, path=source.path, parser=source.parser
    )
    try:
        fetched = fetcher(item, source.etag)
        if fetched.content is None:
            result.unchanged, result.rows = True, source.last_row_count
            source.last_synced, source.last_error = ctx.at, None
            session.add(source)
            return result
        rows = registry.parse(source.parser, fetched.content, ctx.today)
    except (FetchError, ParseError) as exc:
        result.error = source.last_error = str(exc)
        session.add(source)
        return result

    result.rows = len(rows)
    if source.last_row_count >= 20 and len(rows) < source.last_row_count * 0.5:
        result.error = source.last_error = (
            f"parsed {len(rows)} rows, down from {source.last_row_count}. "
            "Kept the previous data in case the source's format changed."
        )
        session.add(source)
        return result

    ctx.listings = {item.key: item for item in session.exec(select(Listing)).all()}
    ctx.links = {
        link.listing_id: link
        for link in session.exec(
            select(ListingSource).where(ListingSource.source_id == source.id)
        ).all()
    }
    ctx.sources = {item.id: item for item in session.exec(select(Source)).all()}
    ctx.listed_by = {}
    for link in session.exec(select(ListingSource)).all():
        ctx.listed_by.setdefault(link.listing_id, set()).add(link.source_id)
    seen: set[int] = set()
    touched: set[int] = set()
    for raw in rows:
        outcome = _upsert(session, raw, source, ctx)
        if outcome is None:
            continue
        listing_id, created = outcome
        if listing_id in seen:
            continue
        seen.add(listing_id)
        touched.add(listing_id)
        result.new += created
    session.flush()

    # Links this source used to have but no longer lists.
    limit = ctx.config.ingest.missing_syncs_to_close
    for link in ctx.links.values():
        if link.listing_id in seen:
            continue
        link.missing_count += 1
        if link.active and link.missing_count >= limit:
            link.active = False
            touched.add(link.listing_id)
        session.add(link)
    session.flush()

    result.closed = _refresh_open(session, touched)
    source.etag, source.last_synced = fetched.etag, ctx.at
    source.last_row_count, source.last_error = len(rows), None
    session.add(source)
    return result


def sync_all(
    session: Session,
    config: Config,
    profile: Profile | None = None,
    *,
    fetcher: Fetcher = registry.fetch,
    today: date | None = None,
    only: str | None = None,
) -> SyncResult:
    sources = ensure_sources(session, config)
    batch = int(db.get_meta(session, "batch", "0")) + 1
    ctx = _Context(config, profile, today or date.today(), batch, now())
    ctx.reads = load_reads(session)
    result = SyncResult(batch=batch)
    for source in sources.values():
        if not source.enabled or (only and source.slug != only):
            continue
        result.sources.append(sync_source(session, source, ctx, fetcher))
        session.commit()
    if result.new:
        db.set_meta(session, "batch", str(batch))
    db.set_meta(session, "last_sync", ctx.at.strftime("%b %d, %H:%M"))
    db.set_meta(session, "last_sync_at", ctx.at.isoformat(timespec="seconds"))
    session.commit()
    return result
