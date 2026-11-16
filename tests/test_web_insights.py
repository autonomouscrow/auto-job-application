"""The Today, Board and Stats pages, and the reminder badge in the menu."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobtool import db
from jobtool.db import Application, Event, Listing, ListingSource, Requirements, Source, Usage
from jobtool.web.app import create_app
from tests.fakes import extracted

TODAY = date.today()
NOW = datetime.now()
EVIL = "<script>alert(1)</script>"


def add(session: Session, n: int, **extra) -> Listing:
    listing = Listing(
        key=f"key-{n}", company=f"Company {n}", title=f"Software Intern {n}",
        url=f"https://jobs.test/{n}", first_seen=NOW - timedelta(days=60), batch=1,
        score=50, role_type="swe", locations_json=json.dumps(["NYC"]),
        score_json=json.dumps({"provisional": True, "components": {"role type": 50}}),
    )  # fmt: skip
    for name, value in extra.items():
        setattr(listing, name, value)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def applied(
    session: Session, n: int, days_ago: int, *, status: str = "applied", **extra
) -> tuple[Listing, Application]:
    """A job marked sent ``days_ago`` days ago. Application fields go in by keyword."""
    fields = {key: extra.pop(key) for key in ("next_followup", "notes") if key in extra}
    listing = add(session, n, state=db.APPLIED, **extra)
    application = Application(
        listing_id=listing.id, status=status, applied_at=NOW - timedelta(days=days_ago), **fields
    )
    session.add(application)
    session.commit()
    session.refresh(application)
    return listing, application


def give_deadline(session: Session, listing: Listing, deadline: date) -> None:
    session.add(
        Requirements(
            listing_id=listing.id, content_hash="abc", model="fake", created_at=NOW,
            data_json=extracted(deadline=deadline.isoformat()).model_dump_json(),
        )  # fmt: skip
    )
    session.commit()


def section(page: str, kind: str) -> str:
    """The part of the Today page under one reminder heading, or '' when it is not there."""
    found = re.search(rf'<section class="reminders" id="{kind}">(.*?)</section>', page, re.S)
    return found.group(1) if found else ""


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


# ----- menu ------------------------------------------------------------------------


def test_menu_links_and_no_badge_without_reminders(client: TestClient) -> None:
    page = client.get("/").text
    for href in ("/", "/today", "/board", "/tracker", "/stats", "/list/set-aside",
                 "/list/rejected", "/list/filtered", "/archetypes", "/sources"):  # fmt: skip
        assert f'href="{href}"' in page
    assert 'class="badge"' not in page


def test_badge_counts_reminders(client: TestClient, session: Session) -> None:
    applied(session, 1, 20)
    applied(session, 2, 3, next_followup=TODAY)
    page = client.get("/tracker").text
    assert re.search(r'<span class="badge"[^>]*>1</span>', page)


# ----- today -----------------------------------------------------------------------


def test_today_empty(client: TestClient) -> None:
    page = client.get("/today")
    assert page.status_code == 200
    assert "Nothing needs attention today." in page.text
    assert "0 sent this week, goal 30." in page.text
    assert "tracking.quiet_after_days" in page.text
    assert "tracking.deadline_within_days" in page.text


def test_today_pace_when_goal_met(client: TestClient, session: Session, home: Path) -> None:
    config = home / "config.yaml"
    config.write_text(config.read_text().replace("weekly_goal: 30", "weekly_goal: 1"))
    applied(session, 1, 0)
    assert "1 sent this week, goal 1." in client.get("/today").text
    assert "Goal met." in client.get("/today").text
    config.write_text(config.read_text().replace("weekly_goal: 1", "weekly_goal: 0"))
    page = client.get("/today").text
    assert "1 sent this week." in page and "goal" not in page.split("</p>")[0].split("sent")[-1]


def test_today_pace_when_behind(client: TestClient) -> None:
    days_left = 7 - TODAY.weekday()
    per_day = -(-30 // days_left)
    page = client.get("/today").text
    plural = "day" if days_left == 1 else "days"
    assert f"About {per_day} a day for the {days_left} {plural} left." in page


def test_each_kind_under_its_heading(client: TestClient, session: Session) -> None:
    quiet, quiet_app = applied(session, 1, 20, company="Quiet Co")
    due, due_app = applied(session, 2, 5, company="Due Co", next_followup=TODAY - timedelta(days=1))
    soon = add(session, 3, company="Soon Co")
    give_deadline(session, soon, TODAY + timedelta(days=2))
    started = add(
        session, 4, company="Started Co", state=db.IN_PROGRESS, decided_at=NOW - timedelta(days=3)
    )
    page = client.get("/today").text

    headings = ["Deadlines coming up", "Follow-ups due", "Applications you started", "No news yet"]
    assert [page.index(text) for text in headings] == sorted(page.index(t) for t in headings)

    deadline = section(page, "deadline")
    assert "Soon Co" in deadline and "Applications close in 2 days." in deadline
    assert f'href="/job/{soon.id}">Open the job' in deadline and "snooze" not in deadline

    follow = section(page, "follow-up")
    assert "Due Co" in follow and "Follow-up was set for 1 day ago." in follow
    assert follow.count(f'action="/application/{due_app.id}/snooze"') == 2
    assert 'name="days" value="7"' in follow and 'name="days" value="3"' in follow
    assert f'action="/application/{due_app.id}"' in follow  # Done
    assert 'name="has_followup" value="1"' in follow
    assert f'href="/job/{due.id}"' in follow

    unfinished = section(page, "unfinished")
    assert "Started Co" in unfinished and f'href="/job/{started.id}">Open the job' in unfinished

    silent = section(page, "quiet")
    assert "Quiet Co" in silent and "Nothing for 20 days since you applied." in silent
    assert silent.count(f'action="/application/{quiet_app.id}/snooze"') == 2
    assert "has_followup" not in silent  # no Done: there is no date to clear
    assert "Nothing needs attention today." not in page


def test_snooze_sets_date_and_hides_reminder(client: TestClient, session: Session) -> None:
    _, application = applied(session, 1, 20, company="Quiet Co")
    assert "Quiet Co" in client.get("/today").text
    response = client.post(
        f"/application/{application.id}/snooze", data={"days": "3"},
        headers={"referer": "http://testserver/today"},
    )  # fmt: skip
    assert response.status_code == 303 and response.headers["location"] == "http://testserver/today"
    session.expire_all()
    assert session.get(Application, application.id).next_followup == TODAY + timedelta(days=3)
    assert "Quiet Co" not in client.get("/today").text


def test_done_clears_follow_up(client: TestClient, session: Session) -> None:
    _, application = applied(session, 1, 3, company="Due Co", next_followup=TODAY)
    assert "Due Co" in section(client.get("/today").text, "follow-up")
    # A cleared date arrives as "not sent", so the form says it carries the date.
    client.post(f"/application/{application.id}", data={"has_followup": "1", "next_followup": ""})
    session.expire_all()
    assert session.get(Application, application.id).next_followup is None
    assert "Due Co" not in client.get("/today").text


def test_snooze_missing_application(client: TestClient) -> None:
    assert client.post("/application/999/snooze", data={"days": "7"}).status_code == 404


# ----- board -----------------------------------------------------------------------


def test_board_empty(client: TestClient) -> None:
    page = client.get("/board")
    assert page.status_code == 200
    assert "I submitted it" in page.text and 'href="/tracker"' in page.text


def test_board_columns_and_counts(client: TestClient, session: Session) -> None:
    applied(session, 1, 2, company="Alpha", notes="Met Sam at the fair\nsecond line")
    applied(session, 2, 4, company="Beta")
    applied(session, 3, 9, company="Gamma", status="interview", next_followup=TODAY)
    page = client.get("/board").text
    columns = re.findall(r'<section class="column" data-status="([^"]+)">\s*<h3>[^<]*'
                         r'<span class="count">(\d+)</span>', page)  # fmt: skip
    assert columns == [
        ("applied", "2"), ("online assessment", "0"), ("interview", "1"),
        ("offer", "0"), ("turned down", "0"), ("withdrawn", "0"),
    ]  # fmt: skip
    assert page.count('<p class="none">None</p>') == 4
    assert "Met Sam at the fair" in page and "second line" not in page
    assert "(due)" in page
    assert page.index("Gamma") > page.index("Alpha")


def test_board_move_card(client: TestClient, session: Session) -> None:
    listing, application = applied(session, 1, 2, company="Alpha")
    response = client.post(
        f"/application/{application.id}", data={"status": "online assessment"},
        headers={"referer": "http://testserver/board"},
    )  # fmt: skip
    assert response.headers["location"] == "http://testserver/board"
    session.expire_all()
    assert session.get(Application, application.id).status == "online assessment"
    event = session.exec(select(Event).where(Event.listing_id == listing.id)).one()
    assert (event.kind, event.from_value, event.to_value) == (
        "status", "applied", "online assessment",
    )  # fmt: skip
    page = client.get("/board").text
    assert re.search(r'data-status="online assessment">\s*<h3>[^<]*<span class="count">1', page)


# ----- stats -----------------------------------------------------------------------


def test_stats_empty(client: TestClient) -> None:
    page = client.get("/stats")
    assert page.status_code == 200
    assert "No applications yet." in page.text
    assert "Claude spending" not in page.text


def test_stats_seeded(client: TestClient, session: Session) -> None:
    source = Source(slug="acme/jobs:README.md", repo="acme/jobs")
    session.add(source)
    session.commit()
    rows = [applied(session, n, days) for n, days in enumerate((0, 0, 7, 7), start=1)]
    rows.append(applied(session, 5, 7, role_type="quant"))
    for listing, _ in rows:
        session.add(ListingSource(listing_id=listing.id, source_id=source.id, first_seen=NOW))
    listing, application = rows[2]
    application.status = "interview"
    session.add(application)
    session.add(
        Event(listing_id=listing.id, at=application.applied_at + timedelta(days=4),
              kind="status", from_value="applied", to_value="interview")
    )  # fmt: skip
    session.add(Usage(at=NOW, purpose="reading", model="claude-haiku-5-5",
                      input_tokens=100_000, output_tokens=20_000))  # fmt: skip
    session.add(Usage(at=NOW, purpose="writing", model="mystery-model",
                      input_tokens=500, output_tokens=50))  # fmt: skip
    session.commit()

    page = client.get("/stats").text
    values = re.findall(r'<span class="value"[^>]*>(\d+)</span>', page)
    assert len(values) == 8 and values[-2:] == ["3", "2"]
    assert values[:-2] == ["0"] * 6
    assert "This week" in page and "Goal 30" in page
    assert "20%</span> heard back (1 of 5)" in page
    assert "within 4.0 days" in page
    assert re.search(
        r"<td>acme/jobs</td>\s*<td class=\"n\">5</td>\s*<td class=\"n\">1</td>"
        r"\s*<td class=\"n\">1</td>\s*<td class=\"n\">20%</td>", page,
    )  # fmt: skip
    assert "<td>Software</td>" in page and "<td>Quant</td>" in page
    assert "<td>reading</td>" in page and "$0.02" in page
    assert "<td>writing</td>" in page and "price not known" in page
    assert "1 call used a model with no known price" in page
    assert "estimate" in page


# ----- escaping --------------------------------------------------------------------


def test_text_is_escaped_on_every_page(client: TestClient, session: Session) -> None:
    source = Source(slug="evil:README.md", repo=EVIL)
    session.add(source)
    session.commit()
    listing, _ = applied(session, 1, 20, company=EVIL, notes=EVIL)
    session.add(ListingSource(listing_id=listing.id, source_id=source.id, first_seen=NOW))
    session.add(Usage(at=NOW, purpose=EVIL, model="claude-haiku-5-5"))
    session.commit()
    for path in ("/today", "/board", "/stats"):
        page = client.get(path).text
        assert EVIL not in page, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, path
