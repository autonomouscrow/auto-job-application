"""Reminders, statistics, the board, the sync timer, and the schedule command."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from jobtool import db, insights, queue
from jobtool.cli import TASK_NAME, app, schedule_plan
from jobtool.config import Config, Tracking
from jobtool.db import Application, Document, Event, Listing, ListingSource, Source, Usage
from jobtool.web.app import create_app
from tests.fakes import extracted

TODAY = date(2026, 11, 18)  # a Wednesday
NOON = datetime(2026, 11, 18, 12)


@pytest.fixture
def session():
    with Session(db.memory_engine()) as session:
        yield session


@pytest.fixture
def config() -> Config:
    return Config(tracking=Tracking(weekly_goal=10, quiet_after_days=14, deadline_within_days=5))


def job(session: Session, n: int, state: str = db.MAIN, **extra) -> Listing:
    listing = Listing(
        key=f"k{n}", company=f"Company {n}", title="Intern", url=f"https://jobs.test/{n}",
        first_seen=NOON, batch=1, role_type="swe", state=state,
    )  # fmt: skip
    for name, value in extra.items():
        setattr(listing, name, value)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def applied(session: Session, n: int, days_ago: int, status: str = "applied", **extra):
    listing = job(session, n, db.APPLIED, **extra.pop("listing", {}))
    application = Application(
        listing_id=listing.id, applied_at=NOON - timedelta(days=days_ago), **extra
    )
    session.add(application)
    session.commit()
    if status != "applied":
        move(session, application, status, NOON - timedelta(days=max(days_ago - 3, 0)))
    return application


def move(session: Session, application: Application, status: str, at: datetime) -> None:
    session.add(
        Event(
            listing_id=application.listing_id, at=at, kind="status",
            from_value=application.status, to_value=status,
        )  # fmt: skip
    )
    application.status = status
    session.add(application)
    session.commit()


def deadline(session: Session, listing: Listing, day: date) -> None:
    session.add(
        db.Requirements(
            listing_id=listing.id, content_hash="h", model="fake", created_at=NOON,
            data_json=extracted(deadline=day.isoformat()).model_dump_json(),
        )  # fmt: skip
    )
    session.commit()


def kinds(session, config) -> list[tuple[str, str]]:
    return [(r.kind, r.listing.company) for r in insights.reminders(session, config, TODAY)]


# ----- reminders ----------------------------------------------------------------


def test_follow_ups_due_today_or_earlier_are_listed_and_later_ones_are_not(session, config):
    applied(session, 1, 3, next_followup=TODAY)
    applied(session, 2, 3, next_followup=TODAY - timedelta(days=2))
    applied(session, 3, 30, next_followup=TODAY + timedelta(days=1))
    found = insights.reminders(session, config, TODAY)
    assert [(r.kind, r.listing.company) for r in found] == [
        ("follow-up", "Company 2"), ("follow-up", "Company 1"),
    ]  # fmt: skip
    assert found[0].text == "Follow-up was set for 2 days ago."
    assert found[1].text == "Follow-up was set for today."
    assert found[0].application is not None


def test_quiet_applications_count_from_the_last_news(session, config):
    applied(session, 1, 14)
    applied(session, 2, 13)
    heard = applied(session, 3, 40)
    move(session, heard, "interview", NOON - timedelta(days=5))
    stale = applied(session, 4, 40)
    move(session, stale, "online assessment", NOON - timedelta(days=20))
    found = insights.reminders(session, config, TODAY)
    assert [(r.kind, r.listing.company) for r in found] == [
        ("quiet", "Company 4"), ("quiet", "Company 1"),
    ]  # fmt: skip
    assert found[0].text == "Nothing for 20 days since the last news."
    assert found[1].text == "Nothing for 14 days since you applied."


def test_finished_applications_are_never_brought_up(session, config):
    for n, status in enumerate(insights.FINISHED):
        done = applied(session, n, 60, next_followup=TODAY)
        move(session, done, status, NOON - timedelta(days=50))
    assert kinds(session, config) == []


def test_quiet_reminders_can_be_turned_off(session, config):
    applied(session, 1, 90)
    config.tracking.quiet_after_days = 0
    assert kinds(session, config) == []


def test_deadlines_only_for_open_jobs_still_waiting_and_only_when_close(session, config):
    deadline(session, job(session, 1), TODAY)
    deadline(session, job(session, 2, db.SET_ASIDE), TODAY + timedelta(days=5))
    deadline(session, job(session, 3), TODAY + timedelta(days=6))
    deadline(session, job(session, 4), TODAY - timedelta(days=1))
    deadline(session, job(session, 5, db.REJECTED), TODAY)
    deadline(session, job(session, 6, open=False), TODAY)
    deadline(session, job(session, 7, db.APPLIED), TODAY)
    tomorrow = job(session, 8)
    deadline(session, tomorrow, TODAY + timedelta(days=1))
    found = insights.reminders(session, config, TODAY)
    assert [(r.listing.company, r.text) for r in found] == [
        ("Company 1", "Applications close today."),
        ("Company 8", "Applications close tomorrow."),
        ("Company 2", "Applications close in 5 days."),
    ]
    config.tracking.deadline_within_days = 0
    assert kinds(session, config) == []


def test_a_reading_with_no_usable_deadline_is_ignored(session, config):
    listing = job(session, 1)
    session.add(
        db.Requirements(
            listing_id=listing.id, content_hash="h", model="m", created_at=NOON,
            data_json=json.dumps({"deadline": "soon"}),
        )  # fmt: skip
    )
    session.commit()
    assert kinds(session, config) == []


def test_an_application_started_and_left_is_brought_up(session, config):
    job(session, 1, db.IN_PROGRESS, decided_at=NOON - timedelta(days=2))
    job(session, 2, db.IN_PROGRESS, decided_at=NOON - timedelta(days=1))
    found = insights.reminders(session, config, TODAY)
    assert [(r.kind, r.listing.company) for r in found] == [("unfinished", "Company 1")]
    assert "2 days ago" in found[0].text


def test_deadlines_come_first_then_follow_ups_then_the_rest(session, config):
    applied(session, 1, 30)
    job(session, 2, db.IN_PROGRESS, decided_at=NOON - timedelta(days=4))
    applied(session, 3, 2, next_followup=TODAY)
    deadline(session, job(session, 4), TODAY + timedelta(days=2))
    assert [kind for kind, _ in kinds(session, config)] == [
        "deadline", "follow-up", "unfinished", "quiet",
    ]  # fmt: skip
    assert insights.reminder_count(session, config, TODAY) == 3  # quiet ones are not counted


def test_snoozing_moves_a_reminder_into_the_future(session, config):
    application = applied(session, 1, 30)
    insights.snooze(session, application, 7, TODAY)
    assert application.next_followup == TODAY + timedelta(days=7)
    assert kinds(session, config) == []
    insights.snooze(session, application, 0, TODAY)
    assert application.next_followup == TODAY + timedelta(days=1)


# ----- statistics -----------------------------------------------------------------


def test_empty_database_gives_empty_numbers(session, config):
    numbers = insights.stats(session, config, TODAY)
    assert numbers.total == 0 and numbers.answer_rate is None
    assert numbers.median_days_to_answer is None and numbers.average_per_week == 0
    assert len(numbers.weeks) == insights.WEEKS_SHOWN and numbers.weeks[-1].current
    assert numbers.goal == 10 and numbers.needed_per_day == 2  # 10 over Wed..Sun
    assert numbers.spend == [] and numbers.by_role == []


def test_weeks_run_monday_to_sunday_and_pace_counts_today(session, config):
    applied(session, 1, 0)  # Wednesday
    applied(session, 2, 2)  # Monday, same week
    applied(session, 3, 3)  # Sunday, last week
    applied(session, 4, 16)  # two weeks before
    numbers = insights.stats(session, config, TODAY)
    assert [week.count for week in numbers.weeks][-3:] == [1, 1, 2]
    assert numbers.weeks[-1].start == date(2026, 11, 16) and numbers.weeks[-1].label == "Nov 16"
    assert numbers.this_week == 2 and numbers.days_left == 5
    assert numbers.needed_per_day == pytest.approx(8 / 5)
    assert numbers.average_per_week == pytest.approx(4 / 3)
    config.tracking.weekly_goal = 1
    assert insights.stats(session, config, TODAY).needed_per_day == 0


def test_outcomes_count_a_stage_once_reached(session, config):
    applied(session, 1, 20)
    applied(session, 2, 20, "online assessment")
    through = applied(session, 3, 20, "online assessment")
    move(session, through, "interview", NOON - timedelta(days=10))
    move(session, through, "turned down", NOON - timedelta(days=5))
    applied(session, 4, 20, "withdrawn")
    offer = applied(session, 5, 20)
    move(session, offer, "offer", NOON - timedelta(days=10))
    numbers = insights.stats(session, config, TODAY)
    assert numbers.statuses == {
        "applied": 1, "online assessment": 1, "interview": 0, "offer": 1,
        "turned down": 1, "withdrawn": 1,
    }  # fmt: skip
    assert numbers.reached == {"answered": 3, "online assessment": 2, "interview": 1, "offer": 1}
    assert numbers.answer_rate == 60
    # First news came 3, 3 and 10 days after applying.
    assert numbers.median_days_to_answer == 3.0
    assert sum(numbers.statuses.values()) == numbers.total == 5


def test_groups_by_role_source_and_resume(session, config):
    session.add_all([Source(slug="a", repo="org/a"), Source(slug="b", repo="org/b")])
    session.commit()
    first = applied(session, 1, 20, "interview")
    second = applied(session, 2, 20, listing={"role_type": "quant"})
    third = applied(session, 3, 20)
    for listing_id, source_id in ((first.listing_id, 1), (first.listing_id, 2),
                                  (second.listing_id, 1)):  # fmt: skip
        session.add(ListingSource(listing_id=listing_id, source_id=source_id, first_seen=NOON))
    session.add(Document(listing_id=first.listing_id, kind="resume", mode="tailor",
                         created_at=NOON))  # fmt: skip
    session.add(Document(listing_id=first.listing_id, kind="letter", created_at=NOON))
    session.add(Document(listing_id=second.listing_id, kind="resume", mode="reuse",
                         created_at=NOON))  # fmt: skip
    session.commit()
    numbers = insights.stats(session, config, TODAY)

    def table(groups):
        return {g.name: (g.applied, g.answered, g.interviews, g.answer_rate) for g in groups}

    assert table(numbers.by_role) == {"swe": (2, 1, 1, 50), "quant": (1, 0, 0, 0)}
    assert numbers.by_role[0].name == "swe"
    assert table(numbers.by_source) == {
        "org/a": (2, 1, 1, 50), "org/b": (1, 1, 1, 100), "no source": (1, 0, 0, 0),
    }  # fmt: skip
    assert table(numbers.by_resume) == {
        "tailored": (1, 1, 1, 100), "archetype as is": (1, 0, 0, 0),
        "made outside the tool": (1, 0, 0, 0),
    }  # fmt: skip
    assert third.listing_id and numbers.decisions["applied"] == 3


def test_decisions_and_spending(session, config):
    job(session, 1)
    job(session, 2, db.REJECTED)
    job(session, 3, db.FILTERED)
    session.add_all(
        [
            Usage(at=NOON, purpose="read posting", model="claude-haiku-5-5",
                  input_tokens=1_000_000, output_tokens=1_000_000),
            Usage(at=NOON - timedelta(days=30), purpose="read posting", model="claude-haiku-5-5",
                  input_tokens=1_000_000, output_tokens=0),
            Usage(at=NOON, purpose="resume", model="claude-opus-5-5",
                  input_tokens=0, output_tokens=1_000_000),
            Usage(at=NOON, purpose="mystery", model="some-other-model",
                  input_tokens=5, output_tokens=5),
        ]
    )  # fmt: skip
    session.commit()
    numbers = insights.stats(session, config, TODAY)
    assert numbers.decisions == {
        "main": 1, "in_progress": 0, "set_aside": 0, "rejected": 1, "applied": 0, "filtered": 1,
    }  # fmt: skip
    spend = {row.purpose: row for row in numbers.spend}
    assert numbers.spend[0].purpose == "resume" and spend["resume"].cost == pytest.approx(20.0)
    assert spend["read posting"].calls == 2
    assert spend["read posting"].cost == pytest.approx(0.70)
    assert spend["read posting"].input_tokens == 2_000_000
    assert spend["mystery"].cost is None and numbers.spend_unpriced == 1
    assert numbers.spend_total == pytest.approx(20.70)
    assert numbers.spend_week == pytest.approx(20.60)


def test_board_has_every_status_with_newest_first(session):
    applied(session, 1, 9)
    applied(session, 2, 1)
    applied(session, 3, 5, "interview")
    columns = insights.board(session)
    assert list(columns) == list(db.STATUSES)
    assert [listing.company for _, listing in columns["applied"]] == ["Company 2", "Company 1"]
    assert len(columns["interview"]) == 1 and columns["offer"] == []


def test_real_status_changes_feed_the_numbers(session, config):
    listing = job(session, 1)
    queue.start_application(session, listing)
    application = queue.mark_applied(session, listing)
    queue.set_status(session, application, "interview")
    numbers = insights.stats(session, config)
    assert numbers.reached["interview"] == 1 and numbers.answer_rate == 100


# ----- scheduled sync -------------------------------------------------------------


def write_tracking(home: Path, hours: float) -> None:
    path = home / "config.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "sync_every_hours: 6", f"sync_every_hours: {hours}"
        ),
        encoding="utf-8",
    )


def test_the_timer_syncs_only_when_the_last_sync_is_old(home: Path, monkeypatch) -> None:
    engine = db.memory_engine()
    calls = []

    def fake_sync(session, config, profile=None, **_):
        calls.append(1)
        db.set_meta(session, "last_sync_at", NOON.isoformat(timespec="seconds"))
        session.commit()

    monkeypatch.setattr("jobtool.web.app.sync_all", fake_sync)
    sync_if_due = create_app(home, engine).state.sync_if_due
    assert sync_if_due(NOON) is True  # never synced before
    assert sync_if_due(NOON + timedelta(hours=5, minutes=59)) is False
    assert sync_if_due(NOON + timedelta(hours=6)) is True
    assert len(calls) == 2

    write_tracking(home, 0)
    assert sync_if_due(NOON + timedelta(days=30)) is False


def test_a_failing_scheduled_sync_is_swallowed(home: Path, monkeypatch) -> None:
    def broken(*_, **__):
        raise RuntimeError("no network")

    monkeypatch.setattr("jobtool.web.app.sync_all", broken)
    app_ = create_app(home, db.memory_engine())
    assert app_.state.sync_if_due(NOON) is False


def test_the_real_sync_records_when_it_ran(home: Path) -> None:
    from jobtool.config import load_config
    from jobtool.sync import sync_all

    config = load_config(home)
    config.sources = []
    with Session(db.memory_engine()) as session:
        sync_all(session, config)
        assert datetime.fromisoformat(db.get_meta(session, "last_sync_at"))


# ----- commands ---------------------------------------------------------------------


def test_schedule_plan_for_windows_quotes_paths_with_spaces() -> None:
    home, repo = Path("C:/Users/A B/private"), Path("C:/Users/A B/repo")
    script, body, command = schedule_plan(home, repo, "07:30", windows=True)
    assert script.name == "sync-task.cmd"
    assert f'cd /d "{repo}"' in body and f'--home "{home}"' in body and body.endswith("\r\n")
    assert "sync.log" in body
    assert command == (
        f'schtasks /Create /F /SC DAILY /ST 07:30 /TN "{TASK_NAME}" /TR "\\"{script}\\""'
    )
    script, body, command = schedule_plan(home, repo, "07:30", windows=False)
    assert script.name == "sync-task.sh" and "30 7 * * *" in command


def test_schedule_command_writes_the_script_and_registers_nothing(home: Path) -> None:
    result = CliRunner().invoke(app, ["schedule", "--home", str(home), "--at", "09:15"])
    assert result.exit_code == 0, result.output
    assert "09:15" in result.output or "15 9" in result.output
    assert [path.name for path in home.glob("sync-task.*")] != []
    bad = CliRunner().invoke(app, ["schedule", "--home", str(home), "--at", "9am"])
    assert bad.exit_code == 2


def test_today_command_lists_reminders(home: Path) -> None:
    with Session(db.open_engine(home)) as session:
        applied(session, 1, 30, next_followup=date.today())
    result = CliRunner().invoke(app, ["today", "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert "This week: 0 sent, goal 30." in result.output
    assert "[follow-up] Company 1, Intern: Follow-up was set for today." in result.output
