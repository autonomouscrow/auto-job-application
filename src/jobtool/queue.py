"""The review flow: what is next, and what each button does."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc
from sqlmodel import Session, func, select

from jobtool import db
from jobtool.db import Application, Event, Listing


def _now() -> datetime:
    return datetime.now()


def _ordered(state: str):
    return (
        select(Listing)
        .where(Listing.state == state, Listing.open == True)  # noqa: E712
        .order_by(
            desc(Listing.batch), desc(Listing.score), desc(Listing.date_posted), Listing.id
        )
    )


def next_listing(session: Session, state: str = db.MAIN) -> Listing | None:
    """The job to show now. An application already started always comes first."""
    if state == db.MAIN:
        started = session.exec(
            select(Listing).where(Listing.state == db.IN_PROGRESS).order_by(Listing.decided_at)
        ).first()
        if started:
            return started
    return session.exec(_ordered(state)).first()


def listings_in(session: Session, state: str, *, open_only: bool = False) -> list[Listing]:
    query = select(Listing).where(Listing.state == state)
    if open_only:
        query = query.where(Listing.open == True)  # noqa: E712
    return list(session.exec(query.order_by(desc(Listing.decided_at), desc(Listing.batch))).all())


def counts(session: Session) -> dict[str, int]:
    rows = session.exec(
        select(Listing.state, Listing.open, func.count()).group_by(Listing.state, Listing.open)
    ).all()
    result = dict.fromkeys(db.STATES, 0)
    for state, is_open, count in rows:
        # Closed listings drop out of the two working queues.
        if state in (db.MAIN, db.SET_ASIDE) and not is_open:
            continue
        result[state] = result.get(state, 0) + count
    today = _now().date()
    result["applied_today"] = len(
        [a for a in session.exec(select(Application)).all() if a.applied_at.date() == today]
    )
    return result


def _move(session: Session, listing: Listing, to_state: str, note: str = "") -> None:
    session.add(
        Event(
            listing_id=listing.id,
            at=_now(),
            kind="state",
            from_value=listing.state,
            to_value=to_state,
            note=note,
        )
    )
    listing.state = to_state
    listing.decided_at = _now()
    if to_state == db.MAIN:
        # Brought back by hand: keep it out of the automatic filter from now on.
        listing.pinned = True
        listing.state_reason = None
    if note:
        listing.note = note
    session.add(listing)


def start_application(session: Session, listing: Listing) -> None:
    _move(session, listing, db.IN_PROGRESS)
    session.commit()


def mark_applied(session: Session, listing: Listing) -> Application:
    _move(session, listing, db.APPLIED)
    application = session.exec(
        select(Application).where(Application.listing_id == listing.id)
    ).first() or Application(listing_id=listing.id, applied_at=_now())
    application.status, application.applied_at = "applied", _now()
    session.add(application)
    session.commit()
    return application


def set_aside(session: Session, listing: Listing, note: str = "") -> None:
    _move(session, listing, db.SET_ASIDE, note)
    session.commit()


def reject(session: Session, listing: Listing) -> None:
    _move(session, listing, db.REJECTED)
    session.commit()


def back_to_main(session: Session, listing: Listing) -> None:
    _move(session, listing, db.MAIN)
    session.commit()


def undo(session: Session) -> Listing | None:
    """Reverse the most recent button press. Returns the listing it affected."""
    event = session.exec(
        select(Event)
        .where(Event.kind == "state", Event.undone == False)  # noqa: E712
        .order_by(desc(Event.id))
    ).first()
    if event is None or event.from_value is None:
        return None
    listing = session.get(Listing, event.listing_id)
    if listing is None:
        return None
    if event.to_value == db.APPLIED:
        application = session.exec(
            select(Application).where(Application.listing_id == listing.id)
        ).first()
        if application:
            session.delete(application)
    listing.state = event.from_value
    event.undone = True
    session.add_all([listing, event])
    session.commit()
    return listing


def set_status(session: Session, application: Application, status: str) -> None:
    if status not in db.STATUSES:
        raise ValueError(f"unknown status '{status}'")
    if status == application.status:
        return
    session.add(
        Event(
            listing_id=application.listing_id,
            at=_now(),
            kind="status",
            from_value=application.status,
            to_value=status,
        )
    )
    application.status = status
    session.add(application)
    session.commit()


def timeline(session: Session, listing_id: int) -> list[Event]:
    return list(
        session.exec(
            select(Event)
            .where(Event.listing_id == listing_id, Event.undone == False)  # noqa: E712
            .order_by(Event.id)
        ).all()
    )
