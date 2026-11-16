"""The card after a posting is read, and the Claude problem banner."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from jobtool import db, llm
from jobtool.db import Listing
from jobtool.web.app import create_app
from tests.fakes import FakeReader, FakeSite, extracted


def add(session: Session, n: int, *, batch: int = 1, score: int = 50, **extra) -> Listing:
    listing = Listing(
        key=f"key-{n}", company=f"Company {n}", title=f"Software Intern {n}",
        url=f"https://jobs.test/{n}", first_seen=datetime(2026, 11, 16), batch=batch,
        score=score, role_type="swe", locations_json=json.dumps(["Toronto, Canada"]),
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
def keyed_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(llm.KEY_NAME, raising=False)
    (home / ".env").write_text(f"{llm.KEY_NAME}=test\n", encoding="utf-8")
    return home


def reading_client(home: Path, engine, site: FakeSite, reader: FakeReader) -> TestClient:
    app = create_app(home, engine, reading=True, get=site, reader_factory=lambda session: reader)
    return TestClient(app, follow_redirects=False)


def test_queue_reads_the_posting_and_shows_the_full_score(
    keyed_home: Path, engine, session: Session
) -> None:
    add(session, 1)
    site, reader = FakeSite(), FakeReader()
    page = reading_client(keyed_home, engine, site, reader).get("/").text

    assert site.calls == ["https://jobs.test/1"] and len(reader.calls) == 1
    assert "provisional match" not in page and "What it asks for" in page
    assert "Build backend services with the platform team." in page
    assert '<span class="chip have"' in page and '<span class="chip lack"' in page
    have = page.split('class="chip have"', 1)[1].split("</span>", 1)[0]
    assert have.endswith(">Python")
    assert "Go</span>" in page and "Docker</span>" in page
    assert '<td class="n">20 / 40</td>' in page  # one of two must-have skills
    assert '<td class="n">14 / 20</td>' in page  # experience relevance 7/10
    assert "Experience relevance 7/10" in page
    assert "$40/hour" in page and "Summer 2027" in page
    assert "Read again" in page and "Your Claude API key" not in page


def test_closed_posting_is_filtered_and_the_next_job_shown(
    keyed_home: Path, engine, session: Session
) -> None:
    add(session, 1, score=90)
    add(session, 2, score=10)
    site = FakeSite({"https://jobs.test/1": (404, "Not found")})
    page = reading_client(keyed_home, engine, site, FakeReader()).get("/").text

    assert "Software Intern 2" in page and "Software Intern 1" not in page
    session.expire_all()
    assert session.get(Listing, 1).state == db.FILTERED


def test_unreadable_page_offers_try_again(keyed_home: Path, engine, session: Session) -> None:
    job = add(session, 1)
    site = FakeSite(default=(200, "<html><body><p>Loading</p></body></html>"))
    client = reading_client(keyed_home, engine, site, FakeReader())
    page = client.get("/").text

    assert "could not be read automatically" in page
    assert f'action="/job/{job.id}/read"' in page and "Try again" in page
    response = client.post(f"/job/{job.id}/read", headers={"referer": "http://testserver/"})
    assert response.status_code == 303 and len(site.calls) == 2


def test_missing_key_banner(
    home: Path, engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(llm.KEY_NAME, raising=False)
    add(session, 1)
    client = TestClient(
        create_app(home, engine, reading=True, get=FakeSite()), follow_redirects=False
    )
    page = client.get("/").text
    assert "No Claude API key" in page and "alert-error" in page
    assert 'action="/claude/check"' in page and "Check again" in page
    assert "Software Intern 1" in page  # the rest of the page still works


def test_bad_key_banner_shows_on_every_page(keyed_home: Path, engine, session: Session) -> None:
    llm.record_problem(session, llm.LlmProblem(llm.BAD_KEY))
    session.commit()
    client = TestClient(create_app(keyed_home, engine), follow_redirects=False)
    for path in ("/tracker", "/sources", "/"):
        page = client.get(path).text
        assert "Your Claude API key was rejected" in page and "alert-error" in page


def test_text_from_the_model_is_escaped(keyed_home: Path, engine, session: Session) -> None:
    add(session, 1)
    reader = FakeReader(
        extracted(
            summary="<script>alert(1)</script>",
            work_authorization="no_sponsorship",
            work_authorization_quote='We do not sponsor "<b>visas</b>".',
        )
    )
    page = reading_client(keyed_home, engine, FakeSite(), reader).get("/").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<b>visas</b>" not in page and "Visa sponsorship is not offered." in page
