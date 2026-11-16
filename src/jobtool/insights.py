"""What needs attention today, and how the search is going.

Reminders and statistics are worked out from the database each time they are
shown. Nothing here is stored, so there is nothing to fall out of date.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

from sqlmodel import Session, select

from jobtool import db, llm
from jobtool.config import Config
from jobtool.db import (
    Application,
    Document,
    Event,
    Listing,
    ListingSource,
    Requirements,
    Source,
    Usage,
)

FOLLOW_UP, QUIET, DEADLINE, UNFINISHED = "follow-up", "quiet", "deadline", "unfinished"
# An application in one of these is over: nothing more to chase.
FINISHED = ("offer", "turned down", "withdrawn")
# Statuses that mean the employer answered, in the order they are reached.
STAGES = ("online assessment", "interview", "offer")
# A started application left this many days is brought up again.
UNFINISHED_AFTER_DAYS = 2
WEEKS_SHOWN = 8


# ----- reminders ----------------------------------------------------------------


@dataclass
class Reminder:
    kind: str
    listing: Listing
    text: str
    # The day it was or is due. Reminders are shown soonest first.
    due: date
    application: Application | None = None


def _last_activity(session: Session) -> dict[int, datetime]:
    """When each application's status last changed, by listing id."""
    latest: dict[int, datetime] = {}
    rows = session.exec(select(Event).where(Event.kind == "status").order_by(Event.at)).all()
    for event in rows:
        latest[event.listing_id] = event.at
    return latest


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _deadline_of(row: Requirements) -> date | None:
    try:
        return date.fromisoformat(json.loads(row.data_json).get("deadline") or "")
    except (ValueError, AttributeError):
        return None


def reminders(session: Session, config: Config, today: date | None = None) -> list[Reminder]:
    """Everything worth a look today, most pressing first."""
    today = today or date.today()
    settings = config.tracking
    found: list[Reminder] = []

    changed = _last_activity(session)
    rows = session.exec(
        select(Application, Listing).join(Listing, Listing.id == Application.listing_id)
    ).all()
    for application, listing in rows:
        if application.status in FINISHED:
            continue
        if application.next_followup is not None:
            if application.next_followup <= today:
                late = (today - application.next_followup).days
                when = "today" if late == 0 else f"{_days(late)} ago"
                found.append(
                    Reminder(
                        FOLLOW_UP, listing, f"Follow-up was set for {when}.",
                        application.next_followup, application,
                    )  # fmt: skip
                )
            continue
        if settings.quiet_after_days <= 0:
            continue
        last = changed.get(listing.id, application.applied_at)
        last = max(last, application.applied_at).date()
        silent = (today - last).days
        if silent >= settings.quiet_after_days:
            what = "since you applied" if application.status == "applied" else "since the last news"
            found.append(
                Reminder(
                    QUIET, listing, f"Nothing for {_days(silent)} {what}.",
                    last + timedelta(days=settings.quiet_after_days), application,
                )  # fmt: skip
            )

    waiting = (db.MAIN, db.SET_ASIDE, db.IN_PROGRESS)
    if settings.deadline_within_days > 0:
        readings = session.exec(
            select(Requirements, Listing)
            .join(Listing, Listing.id == Requirements.listing_id)
            .where(Listing.state.in_(waiting), Listing.open == True)  # noqa: E712
            .where(Requirements.data_json.contains('"deadline"'))
        ).all()
        for reading, listing in readings:
            deadline = _deadline_of(reading)
            if deadline is None:
                continue
            left = (deadline - today).days
            if not 0 <= left <= settings.deadline_within_days:
                continue
            when = "today" if left == 0 else "tomorrow" if left == 1 else f"in {_days(left)}"
            found.append(Reminder(DEADLINE, listing, f"Applications close {when}.", deadline))

    started = session.exec(select(Listing).where(Listing.state == db.IN_PROGRESS)).all()
    for listing in started:
        since = (today - listing.decided_at.date()).days if listing.decided_at else 0
        if since >= UNFINISHED_AFTER_DAYS:
            found.append(
                Reminder(
                    UNFINISHED, listing,
                    f"You started this application {_days(since)} ago and have not marked it sent.",
                    listing.decided_at.date(),
                )  # fmt: skip
            )

    order = {DEADLINE: 0, FOLLOW_UP: 1, UNFINISHED: 2, QUIET: 3}
    return sorted(found, key=lambda item: (order[item.kind], item.due, item.listing.id))


def reminder_count(session: Session, config: Config, today: date | None = None) -> int:
    """How many reminders are pressing. Quiet applications pile up, so they are not counted."""
    return sum(1 for item in reminders(session, config, today) if item.kind != QUIET)


def snooze(
    session: Session, application: Application, days: int, today: date | None = None
) -> None:
    """Ask to be reminded about this application again in ``days`` days."""
    application.next_followup = (today or date.today()) + timedelta(days=max(1, days))
    session.add(application)
    session.commit()


# ----- statistics -----------------------------------------------------------------


@dataclass
class Week:
    start: date
    count: int
    current: bool = False

    @property
    def label(self) -> str:
        return self.start.strftime("%b %d").replace(" 0", " ")


@dataclass
class Group:
    """Applications of one kind and how many of them got an answer."""

    name: str
    applied: int = 0
    answered: int = 0
    interviews: int = 0

    @property
    def answer_rate(self) -> int | None:
        return round(100 * self.answered / self.applied) if self.applied else None


@dataclass
class Spend:
    purpose: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # None when a call used a model whose price is not known.
    cost: float | None = 0.0


@dataclass
class Stats:
    total: int = 0
    this_week: int = 0
    goal: int = 0
    # Applications a day needed on the days left this week to reach the goal.
    needed_per_day: float = 0.0
    days_left: int = 0
    weeks: list[Week] = field(default_factory=list)
    average_per_week: float = 0.0
    # Current status -> count, in the order of db.STATUSES.
    statuses: dict[str, int] = field(default_factory=dict)
    # How many applications ever reached each stage: answered, assessment, interview, offer.
    reached: dict[str, int] = field(default_factory=dict)
    answer_rate: int | None = None
    median_days_to_answer: float | None = None
    by_role: list[Group] = field(default_factory=list)
    by_source: list[Group] = field(default_factory=list)
    by_resume: list[Group] = field(default_factory=list)
    # Listing state -> count: what happened to every job the tool has found.
    decisions: dict[str, int] = field(default_factory=dict)
    spend: list[Spend] = field(default_factory=list)
    spend_total: float = 0.0
    spend_week: float = 0.0
    spend_unpriced: int = 0


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _spending(session: Session, today: date) -> tuple[list[Spend], float, float, int]:
    by_purpose: dict[str, Spend] = {}
    total = week = 0.0
    unpriced = 0
    week_ago = datetime.combine(today - timedelta(days=6), datetime.min.time())
    for call in session.exec(select(Usage)).all():
        row = by_purpose.setdefault(call.purpose, Spend(call.purpose))
        row.calls += 1
        row.input_tokens += call.input_tokens
        row.output_tokens += call.output_tokens
        cost = llm.cost_usd(call.model, call.input_tokens, call.output_tokens)
        if cost is None:
            row.cost, unpriced = None, unpriced + 1
            continue
        if row.cost is not None:
            row.cost += cost
        total += cost
        if call.at >= week_ago:
            week += cost
    rows = sorted(by_purpose.values(), key=lambda item: -(item.cost or 0))
    return rows, total, week, unpriced


def stats(session: Session, config: Config, today: date | None = None) -> Stats:
    today = today or date.today()
    out = Stats(goal=max(0, config.tracking.weekly_goal))
    rows = session.exec(
        select(Application, Listing).join(Listing, Listing.id == Application.listing_id)
    ).all()
    out.total = len(rows)

    # Pace: applications a week, Monday to Sunday.
    this_monday = _monday(today)
    per_week = Counter(_monday(application.applied_at.date()) for application, _ in rows)
    out.weeks = [
        Week(start, per_week.get(start, 0), start == this_monday)
        for start in (this_monday - timedelta(weeks=n) for n in range(WEEKS_SHOWN - 1, -1, -1))
    ]
    out.this_week = per_week.get(this_monday, 0)
    out.days_left = 7 - today.weekday()  # today counts
    out.needed_per_day = max(0, out.goal - out.this_week) / out.days_left
    if rows:
        first = _monday(min(application.applied_at.date() for application, _ in rows))
        out.average_per_week = out.total / (((this_monday - first).days // 7) + 1)

    # Outcomes. A stage counts once reached, even if the application later moved on.
    stages: dict[int, set[str]] = defaultdict(set)
    first_news: dict[int, datetime] = {}
    events = session.exec(select(Event).where(Event.kind == "status").order_by(Event.at)).all()
    for event in events:
        stages[event.listing_id].add(event.to_value)
        if event.to_value != "applied" and event.to_value != "withdrawn":
            first_news.setdefault(event.listing_id, event.at)
    out.statuses = {status: 0 for status in db.STATUSES}
    answered: set[int] = set()
    interviewed: set[int] = set()
    waits: list[float] = []
    reached = Counter()
    for application, listing in rows:
        seen = stages[listing.id] | {application.status}
        out.statuses[application.status] = out.statuses.get(application.status, 0) + 1
        if seen - {"applied", "withdrawn"}:
            answered.add(listing.id)
            reached["answered"] += 1
            if listing.id in first_news:
                gap = first_news[listing.id] - application.applied_at
                waits.append(max(0.0, gap.total_seconds() / 86400))
        # An offer implies the earlier stages even when they were never recorded.
        if seen & {"interview", "offer"}:
            interviewed.add(listing.id)
        for stage in STAGES:
            if stage in seen:
                reached[stage] += 1
    out.reached = {name: reached.get(name, 0) for name in ("answered", *STAGES)}
    out.answer_rate = round(100 * len(answered) / out.total) if out.total else None
    out.median_days_to_answer = round(median(waits), 1) if waits else None

    def grouped(names: dict[int, list[str]], fallback: str) -> list[Group]:
        groups: dict[str, Group] = {}
        for _, listing in rows:
            for name in names.get(listing.id) or [fallback]:
                group = groups.setdefault(name, Group(name))
                group.applied += 1
                group.answered += listing.id in answered
                group.interviews += listing.id in interviewed
        return sorted(groups.values(), key=lambda item: (-item.applied, item.name))

    out.by_role = grouped({listing.id: [listing.role_type] for _, listing in rows}, "unknown")
    sources: dict[int, list[str]] = defaultdict(list)
    links = session.exec(
        select(ListingSource.listing_id, Source.repo).join(
            Source, Source.id == ListingSource.source_id
        )
    ).all()
    for listing_id, repo in links:
        if repo not in sources[listing_id]:
            sources[listing_id].append(repo)
    out.by_source = grouped(sources, "no source")
    made = session.exec(select(Document).where(Document.kind == "resume")).all()
    labels = {"reuse": "archetype as is", "touch-up": "archetype, reordered", "tailor": "tailored"}
    out.by_resume = grouped(
        {doc.listing_id: [labels.get(doc.mode, doc.mode)] for doc in made}, "made outside the tool"
    )

    states = session.exec(select(Listing.state)).all()
    counted = Counter(states)
    out.decisions = {state: counted.get(state, 0) for state in db.STATES}
    out.spend, out.spend_total, out.spend_week, out.spend_unpriced = _spending(session, today)
    return out


# ----- board ------------------------------------------------------------------------


def board(session: Session) -> dict[str, list[tuple[Application, Listing]]]:
    """Applications by status, newest first in each column."""
    columns: dict[str, list[tuple[Application, Listing]]] = {status: [] for status in db.STATUSES}
    rows = session.exec(
        select(Application, Listing)
        .join(Listing, Listing.id == Application.listing_id)
        .order_by(Application.applied_at.desc())
    ).all()
    for application, listing in rows:
        columns.setdefault(application.status, []).append((application, listing))
    return columns
