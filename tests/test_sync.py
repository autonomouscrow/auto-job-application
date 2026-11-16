from __future__ import annotations

import json
from datetime import date

import pytest
from sqlmodel import Session, select

from jobtool import db, queue
from jobtool.config import Config
from jobtool.db import Listing, ListingSource
from jobtool.ingest.fetch import Fetched, FetchError
from jobtool.sync import sync_all

TODAY = date(2026, 11, 16)


def make_config() -> Config:
    return Config.model_validate(
        {
            "targets": {
                "roles": ["quant", "swe"],
                "term": {"earliest_start": "2027-05-15"},
                "regions": ["north-america", "europe"],
            },
            "sources": [
                {"repo": "a/json", "path": "listings.json", "parser": "json"},
                {"repo": "b/table", "path": "README.md", "parser": "markdown-table"},
            ],
        }
    )


def row(n: int, title: str = "Software Engineer Intern", **extra) -> dict:
    base = {
        "id": f"id-{n}", "company_name": f"Company {n}", "title": title,
        "url": f"https://jobs.test/{n}?utm_source=list", "locations": ["NYC"],
        "terms": ["Summer 2027"], "active": True, "is_visible": True,
        "date_posted": 1794700800,
    }  # fmt: skip
    base.update(extra)
    return base


def table(*rows: tuple[str, str, str]) -> str:
    lines = ["| Company | Title | Location | Link | Date |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {c} | {t} | London, UK | [Apply]({u}) | Nov 10 |" for c, t, u in rows]
    return "\n".join(lines)


class World:
    """Stands in for GitHub: holds what each source would return."""

    def __init__(self) -> None:
        self.files: dict[str, str | Exception] = {"a/json": "[]", "b/table": table()}

    def fetch(self, source, etag):
        content = self.files[source.repo]
        if isinstance(content, Exception):
            raise content
        return Fetched(content, etag="v1")


@pytest.fixture
def session():
    with Session(db.memory_engine()) as session:
        yield session


@pytest.fixture
def world() -> World:
    return World()


def run(session: Session, world: World):
    return sync_all(session, make_config(), fetcher=world.fetch, today=TODAY)


def titles(session: Session, state: str) -> list[str]:
    return sorted(
        item.company for item in session.exec(select(Listing).where(Listing.state == state))
    )


def test_first_sync_fills_the_queue_and_filters(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps(
        [row(1), row(2, "Marketing Intern"), row(3, terms=["Fall 2026"]), row(4, active=False)]
    )
    result = run(session, world)
    # Company 4 was already closed when first seen, so it is not tracked at all.
    assert result.new == 3
    assert titles(session, db.MAIN) == ["Company 1"]
    assert titles(session, db.FILTERED) == ["Company 2", "Company 3"]
    assert queue.next_listing(session).company == "Company 1"
    assert queue.counts(session)[db.MAIN] == 1


def test_same_job_from_two_repos_is_one_listing(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1)])
    world.files["b/table"] = table(("Company 1", "SWE Intern", "https://jobs.test/1?ref=other"))
    run(session, world)
    listings = session.exec(select(Listing)).all()
    assert len(listings) == 1
    assert len(session.exec(select(ListingSource)).all()) == 2
    # The richer source's term survives the poorer one.
    assert listings[0].terms == ["Summer 2027"]


def test_new_jobs_go_to_the_front(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1), row(2)])
    run(session, world)
    world.files["a/json"] = json.dumps([row(1), row(2), row(3, "Data Science Intern")])
    result = run(session, world)
    assert result.new == 1
    first = queue.next_listing(session)
    assert first.company == "Company 3"
    # Even though the older jobs score higher.
    older = session.exec(select(Listing).where(Listing.company == "Company 1")).one()
    assert older.score > first.score


def test_decisions_survive_a_sync(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1), row(2), row(3)])
    run(session, world)
    by_company = {item.company: item for item in session.exec(select(Listing)).all()}
    queue.reject(session, by_company["Company 1"])
    queue.set_aside(session, by_company["Company 2"], "later")
    run(session, world)
    assert titles(session, db.REJECTED) == ["Company 1"]
    assert titles(session, db.SET_ASIDE) == ["Company 2"]
    assert titles(session, db.MAIN) == ["Company 3"]


def test_source_flag_closes_a_job(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1)])
    run(session, world)
    world.files["a/json"] = json.dumps([row(1, active=False)])
    result = run(session, world)
    assert result.sources[0].closed == 1
    assert queue.next_listing(session) is None


def test_vanished_link_closes_after_two_syncs(session: Session, world: World) -> None:
    links = [("Acme", "Quant Intern", f"https://acme.test/{n}") for n in range(3)]
    world.files["b/table"] = table(*links)
    run(session, world)
    world.files["b/table"] = table(*links[:2])
    run(session, world)
    assert queue.counts(session)[db.MAIN] == 3  # missing once: still open
    run(session, world)
    assert queue.counts(session)[db.MAIN] == 2  # missing twice: closed
    world.files["b/table"] = table(*links)
    run(session, world)
    assert queue.counts(session)[db.MAIN] == 3  # it came back


def test_job_stays_open_while_any_source_lists_it(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1)])
    world.files["b/table"] = table(("Company 1", "SWE Intern", "https://jobs.test/1"))
    run(session, world)
    world.files["a/json"] = json.dumps([row(1, active=False)])
    run(session, world)
    assert queue.next_listing(session) is not None


def test_sudden_drop_keeps_previous_data(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(n) for n in range(30)])
    run(session, world)
    world.files["a/json"] = json.dumps([row(0)])
    result = run(session, world)
    assert "Kept the previous data" in result.sources[0].error
    assert queue.counts(session)[db.MAIN] == 30


def test_failed_source_does_not_stop_the_others(session: Session, world: World) -> None:
    world.files["a/json"] = FetchError("boom")
    world.files["b/table"] = table(("Acme", "Quant Intern", "https://acme.test/1"))
    result = run(session, world)
    assert result.sources[0].error == "boom"
    assert result.new == 1


def test_possible_repeat_is_linked(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1)])
    run(session, world)
    first = session.exec(select(Listing)).one()
    queue.reject(session, first)
    reposted = row(1, url="https://jobs.test/reposted-99")
    reposted["id"] = "id-99"
    world.files["a/json"] = json.dumps([row(1), reposted])
    run(session, world)
    repeat = session.exec(select(Listing).where(Listing.id != first.id)).one()
    assert repeat.repeat_of == first.id


def test_restored_job_is_not_filtered_again(session: Session, world: World) -> None:
    world.files["a/json"] = json.dumps([row(1, "Marketing Intern")])
    run(session, world)
    listing = session.exec(select(Listing)).one()
    assert listing.state == db.FILTERED
    queue.back_to_main(session, listing)
    run(session, world)
    assert session.exec(select(Listing)).one().state == db.MAIN


def test_verdict_does_not_depend_on_source_order(session: Session, world: World) -> None:
    # Old posting: the table source alone would call it stale, but the JSON source
    # marks closures itself and still lists it as open.
    old = row(1, date_posted=1785000000)
    world.files["a/json"] = json.dumps([old])
    world.files["b/table"] = table(("Company 1", "SWE Intern", "https://jobs.test/1"))
    run(session, world)
    first = session.exec(select(Listing)).one().state
    run(session, world)
    assert session.exec(select(Listing)).one().state == first == db.MAIN


def test_closed_row_elsewhere_does_not_change_an_open_job(session: Session, world: World) -> None:
    # One list has last year's closed row for a URL another list shows as open now.
    world.files["a/json"] = json.dumps([row(1, active=False, terms=["Summer 2026"])])
    world.files["b/table"] = table(("Company 1", "SWE Intern", "https://jobs.test/1"))
    for _ in range(2):
        run(session, world)
        listing = session.exec(select(Listing)).one()
        assert (listing.state, listing.open, listing.terms) == (db.MAIN, True, [])
