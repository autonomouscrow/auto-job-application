from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobtool import db, maintenance, queue
from jobtool.db import Application, Listing
from jobtool.web.app import create_app


def add(session: Session, n: int, *, batch: int = 1, score: int = 50, **extra) -> Listing:
    listing = Listing(
        key=f"key-{n}", company=f"Company {n}", title=f"Software Intern {n}",
        url=f"https://jobs.test/{n}", first_seen=datetime(2026, 11, 16), batch=batch,
        score=score, role_type="swe", locations_json=json.dumps(["NYC"]),
        score_json=json.dumps({"provisional": True, "components": {"role type": score}}),
    )  # fmt: skip
    for name, value in extra.items():
        setattr(listing, name, value)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


@pytest.fixture
def engine():
    return db.memory_engine()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(engine, home: Path) -> TestClient:
    return TestClient(create_app(home, engine), follow_redirects=False)


def test_order_is_newest_batch_then_score(session: Session) -> None:
    add(session, 1, batch=1, score=90)
    add(session, 2, batch=2, score=40)
    add(session, 3, batch=2, score=70)
    seen = []
    while (job := queue.next_listing(session)) is not None:
        seen.append(job.company)
        queue.reject(session, job)
    assert seen == ["Company 3", "Company 2", "Company 1"]


def test_apply_flow_and_undo(session: Session) -> None:
    first, second = add(session, 1, score=90), add(session, 2, score=10)
    queue.start_application(session, first)
    assert queue.next_listing(session).id == first.id  # stays in front while in progress
    queue.mark_applied(session, first)
    assert queue.next_listing(session).id == second.id
    assert session.exec(select(Application)).one().status == "applied"

    assert queue.undo(session).id == first.id
    assert session.exec(select(Application)).first() is None
    assert first.state == db.IN_PROGRESS
    assert queue.undo(session).id == first.id
    assert first.state == db.MAIN
    assert queue.undo(session) is None


def test_set_aside_is_a_separate_queue(session: Session) -> None:
    job = add(session, 1)
    queue.set_aside(session, job, "after exams")
    assert queue.next_listing(session) is None
    assert queue.next_listing(session, db.SET_ASIDE).note == "after exams"
    queue.back_to_main(session, job)
    assert queue.next_listing(session).id == job.id


def test_rejected_never_returns(session: Session) -> None:
    job = add(session, 1)
    queue.reject(session, job)
    assert queue.next_listing(session) is None
    assert queue.next_listing(session, db.SET_ASIDE) is None
    assert [item.id for item in queue.listings_in(session, db.REJECTED)] == [job.id]


def test_status_changes_are_recorded(session: Session) -> None:
    job = add(session, 1)
    application = queue.mark_applied(session, job)
    queue.set_status(session, application, "interview")
    assert [event.to_value for event in queue.timeline(session, job.id)] == [
        "applied", "interview"
    ]  # fmt: skip
    with pytest.raises(ValueError):
        queue.set_status(session, application, "ghosted")


def test_web_walkthrough(client: TestClient, session: Session) -> None:
    first, second = add(session, 1, score=90), add(session, 2, score=10)

    page = client.get("/")
    assert page.status_code == 200
    assert "Software Intern 1" in page.text and "provisional match" in page.text

    assert client.post(f"/job/{first.id}/apply").status_code == 303
    assert "Application in progress" in client.get("/").text
    client.post(f"/job/{first.id}/applied")
    assert "Software Intern 2" in client.get("/").text

    client.post(f"/job/{second.id}/set-aside", data={"note": "later"})
    assert "Nothing left in the queue" in client.get("/").text
    assert "later" in client.get("/list/set-aside").text
    assert "Software Intern 2" in client.get("/set-aside/work").text

    client.post(f"/job/{second.id}/reject")
    assert "Software Intern 2" in client.get("/list/rejected").text
    client.post("/undo")
    assert "Software Intern 2" in client.get("/list/set-aside").text

    tracker = client.get("/tracker")
    assert "Company 1" in tracker.text
    session.expire_all()
    application = session.exec(select(Application)).one()
    client.post(
        f"/application/{application.id}",
        data={"status": "interview", "notes": "phone screen", "next_followup": "2026-12-01"},
    )
    session.expire_all()
    application = session.exec(select(Application)).one()
    assert (application.status, application.notes) == ("interview", "phone screen")
    assert "phone screen" in client.get(f"/job/{first.id}").text

    csv = client.get("/export.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "Company 1,Software Intern 1,interview" in csv.text


def test_other_pages_render(client: TestClient, session: Session) -> None:
    add(session, 1, state=db.FILTERED, state_reason="role: not a target")
    assert "role: not a target" in client.get("/list/filtered").text
    assert client.get("/sources").status_code == 200
    assert client.get("/list/nope").status_code == 404
    assert client.get("/job/999").status_code == 404
    assert client.post("/job/1/explode").status_code == 404


def test_cross_site_posts_are_refused(client: TestClient, session: Session) -> None:
    job = add(session, 1)
    response = client.post(f"/job/{job.id}/reject", headers={"origin": "https://evil.test"})
    assert response.status_code == 403
    session.expire_all()
    assert session.get(Listing, job.id).state == db.MAIN


def test_text_from_sources_is_escaped(client: TestClient, session: Session) -> None:
    add(session, 1, title="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in client.get("/").text


def test_backup_once_a_day(home: Path) -> None:
    assert maintenance.backup(home) is None  # no database yet
    engine = db.open_engine(home)
    with Session(engine) as session:
        add(session, 1)
    engine.dispose()
    saved = maintenance.backup(home)
    assert saved is not None and saved.exists()
    assert maintenance.backup(home) is None
