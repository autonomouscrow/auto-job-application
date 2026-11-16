"""The application panel: details, files and answers to copy into an employer's form."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_plus

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobtool import db
from jobtool.assist import AnswerDraft, answers_path
from jobtool.db import JobAnswer, Listing
from jobtool.llm import LlmProblem
from jobtool.profile.loader import load_profile
from jobtool.resume import documents
from jobtool.resume.write import _Fix, _Fixes
from jobtool.web.app import create_app
from tests.fakes import FakeReader

UPLOADS = "exp-acme-2026.uploads"
GOOD = AnswerDraft(
    text=(
        "At Acme Robotics I built a Python service that batches sensor uploads to S3, "
        "cutting upload failures from 9% to 1%."
    ),
    fact_ids=[UPLOADS],
)
BAD = AnswerDraft(
    text="At Acme I led a team of 40 engineers on Kubernetes, cutting costs by 95%.",
    fact_ids=[UPLOADS],
)
RELOCATE = "Are you willing to relocate for this internship?"


def add(session: Session, n: int, **extra) -> Listing:
    listing = Listing(
        key=f"key-{n}", company=f"Company {n}", title=f"Software Intern {n}",
        url=f"https://jobs.test/{n}", first_seen=datetime(2026, 11, 16), batch=1,
        score=50 - n, role_type="swe", locations_json=json.dumps(["NYC"]),
        score_json=json.dumps({"provisional": True, "components": {"role type": 50}}),
    )  # fmt: skip
    for name, value in extra.items():
        setattr(listing, name, value)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def model(draft=GOOD, fix=None, problem=None) -> FakeReader:
    fixes = _Fixes(fixes=[fix] if fix else [])
    return FakeReader(problem=problem, answers={AnswerDraft: draft, _Fixes: fixes})


def make_client(home: Path, engine, reader: FakeReader) -> TestClient:
    return TestClient(
        create_app(home, engine, writer_factory=lambda session: reader), follow_redirects=False
    )


@pytest.fixture
def engine():
    return db.memory_engine()


@pytest.fixture
def reader() -> FakeReader:
    return model()


@pytest.fixture
def client(home: Path, engine, reader: FakeReader) -> TestClient:
    return make_client(home, engine, reader)


@pytest.fixture
def job_id(engine) -> int:
    with Session(engine) as session:
        return add(session, 1, state=db.IN_PROGRESS, decided_at=datetime(2026, 11, 16, 10)).id


def from_page(job: int) -> dict:
    return {"referer": f"http://testserver/job/{job}"}


def rows(engine, job: int | None = None) -> list[JobAnswer]:
    with Session(engine) as session:
        query = select(JobAnswer).order_by(JobAnswer.id)
        if job is not None:
            query = query.where(JobAnswer.listing_id == job)
        return list(session.exec(query).all())


def message_of(response) -> str:
    location = response.headers["location"]
    return unquote_plus(location.split("?message=", 1)[1].split("#", 1)[0])


def keep(client: TestClient, job: int, **data) -> object:
    return client.post(f"/job/{job}/answer", data=data, headers=from_page(job))


# ----- where the panel shows --------------------------------------------------


def test_queue_shows_the_panel_only_once_applying(client: TestClient, engine) -> None:
    with Session(engine) as session:
        job = add(session, 1).id
    page = client.get("/").text
    assert "Software Intern 1" in page and 'id="assist"' not in page

    assert client.post(f"/job/{job}/apply").status_code == 303
    page = client.get("/").text
    assert 'id="assist"' in page and 'id="answers"' in page
    assert "Your details" in page and 'class="applygrid"' in page
    assert '<main class="wide">' in page


def test_job_page_shows_it_open_when_applying_and_folded_otherwise(
    client: TestClient, engine, job_id: int
) -> None:
    page = client.get(f"/job/{job_id}").text
    assert 'class="card assistcard"' in page and 'id="assist"' in page
    assert "assistlater" not in page

    with Session(engine) as session:
        other = add(session, 2).id
    page = client.get(f"/job/{other}").text
    assert '<details class="card assistlater">' in page and 'id="assist"' in page
    assert "assistcard" not in page


def test_a_broken_profile_gives_one_line(client: TestClient, home: Path, job_id: int) -> None:
    (home / "profile" / "identity.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    page = client.get(f"/job/{job_id}").text
    assert "profile has a problem" in page
    assert "<h4>Your details</h4>" not in page and "data-copy=" not in page


def test_details_have_copy_values(client: TestClient, job_id: int) -> None:
    page = client.get(f"/job/{job_id}").text
    for value in ("Alex Doe", "alex.doe@example.com", "+1-555-010-0199",
                  "https://github.com/alexdoe-example"):  # fmt: skip
        assert f'data-copy="{value}"' in page
    assert 'data-copy="Alex"' in page and 'data-copy="Doe"' in page
    assert "Work authorization" in page and "Eligible for a J-1 intern visa" in page
    assert "Acme Robotics, Software Engineering Intern" in page


def test_file_paths_show_once_the_pdf_exists(client: TestClient, home: Path, job_id: int) -> None:
    page = client.get(f"/job/{job_id}").text
    assert page.count("Not made yet.") == 2
    assert f'href="/job/{job_id}/resume.pdf"' not in page

    profile = load_profile(home / "profile").profile
    path = documents.pdf_path(documents.listing_dir(home, job_id), profile, documents.RESUME)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"%PDF-1.5 test")
    page = client.get(f"/job/{job_id}").text
    assert f'data-copy="{path}"' in page and "Copy path" in page
    assert f'href="/job/{job_id}/resume.pdf"' in page
    assert page.count("Not made yet.") == 1  # the cover letter


# ----- saved answers ----------------------------------------------------------


def test_lookup_finds_a_similar_saved_answer(client: TestClient, job_id: int) -> None:
    asked = "Are you open to relocating for the internship?"
    page = client.get(f"/job/{job_id}", params={"q": asked}).text
    assert "Saved answers to similar questions" in page
    assert RELOCATE in page and 'name="bank_id" value="ans-relocate"' in page
    assert f'name="question" value="{asked}"' in page

    page = client.get(f"/job/{job_id}", params={"q": "What is your favourite colour?"}).text
    assert "No saved answer is close" in page
    assert 'name="bank_id"' not in page
    # The queue page looks up the same way.
    page = client.get("/", params={"q": "Tell us about a project you are proud of"}).text
    assert 'value="ans-proud-project"' in page


def test_using_a_saved_answer_keeps_it_for_this_job(
    client: TestClient, engine, job_id: int
) -> None:
    asked = "Would you relocate for the role?"
    response = keep(client, job_id, bank_id="ans-relocate", question=asked)
    assert response.status_code == 303
    assert response.headers["location"] == f"http://testserver/job/{job_id}#answers"
    (row,) = rows(engine)
    assert (row.source, row.question, row.answer) == ("bank", asked, "Yes.")
    page = client.get(f"/job/{job_id}").text
    assert "From your saved answers" in page and asked in page
    # The question differs from the saved one, so it can be saved as a new one.
    assert f'action="/job/{job_id}/answer/{row.id}/bank"' in page

    keep(client, job_id, bank_id="ans-proud-project", question="")
    row = rows(engine)[-1]
    assert row.question == "Tell us about a project you are proud of."
    assert row.fact_ids == ["proj-orderbook.engine", "proj-orderbook.replay"]
    page = client.get(f"/job/{job_id}").text
    assert f'action="/job/{job_id}/answer/{row.id}/bank"' not in page  # already saved

    assert keep(client, job_id, bank_id="ans-nothing").status_code == 404


def test_saved_answers_are_listed_with_copy_buttons(client: TestClient, job_id: int) -> None:
    page = client.get(f"/job/{job_id}").text
    assert "All saved answers (2)" in page
    assert 'data-copy="Yes."' in page


# ----- answers written by hand ------------------------------------------------


def test_write_edit_and_delete_an_answer(client: TestClient, engine, job_id: int) -> None:
    response = keep(client, job_id, question="Why  this team?", answer="I like testing.\n")
    assert response.status_code == 303
    (row,) = rows(engine)
    assert (row.question, row.answer) == ("Why this team?", "I like testing.")
    assert row.source == "written"
    page = client.get(f"/job/{job_id}").text
    assert "Written by you" in page and ">I like testing.</textarea>" in page
    assert f'data-copy-from="answer-{row.id}"' in page

    keep(client, job_id, answer_id=row.id, question=row.question, answer="I like careful tests.")
    (row,) = rows(engine)
    assert row.answer == "I like careful tests." and row.source == "written"

    response = keep(client, job_id, question="Why this team?", answer="  ")
    assert "needs a question and some text" in message_of(response)
    assert len(rows(engine)) == 1

    response = client.post(f"/job/{job_id}/answer/{row.id}/delete", headers=from_page(job_id))
    assert response.status_code == 303 and rows(engine) == []
    assert "Written by you" not in client.get(f"/job/{job_id}").text


def test_adding_to_saved_answers(client: TestClient, engine, home: Path, job_id: int) -> None:
    before = answers_path(home).read_text(encoding="utf-8")
    response = keep(
        client, job_id, question="What are your pronouns?", answer="They/them.", to_bank="1"
    )
    assert message_of(response) == "Added to your saved answers"
    after = answers_path(home).read_text(encoding="utf-8")
    assert after.startswith(before)
    profile = load_profile(home / "profile").profile
    assert [item.answer for item in profile.answers][-1] == "They/them."
    page = client.get(f"/job/{job_id}").text
    assert "All saved answers (3)" in page
    (row,) = rows(engine)
    assert f'action="/job/{job_id}/answer/{row.id}/bank"' not in page

    # The same question again is refused with a message, and the file is unchanged.
    response = keep(
        client, job_id, question="what are your pronouns", answer="They.", to_bank="1"
    )
    assert message_of(response).startswith("Not added to your saved answers")
    assert answers_path(home).read_text(encoding="utf-8") == after
    response = client.post(f"/job/{job_id}/answer/{row.id}/bank", headers=from_page(job_id))
    assert message_of(response).startswith("Not added to your saved answers")
    assert len(yaml.safe_load(answers_path(home).read_text(encoding="utf-8"))) == 3


def test_add_a_kept_answer_to_saved_answers(
    client: TestClient, engine, home: Path, job_id: int
) -> None:
    keep(client, job_id, question="When can you start?", answer="In May 2027.")
    (row,) = rows(engine)
    response = client.post(f"/job/{job_id}/answer/{row.id}/bank", headers=from_page(job_id))
    assert message_of(response) == "Added to your saved answers"
    assert response.headers["location"].endswith("#answers")
    questions = [item.question for item in load_profile(home / "profile").profile.answers]
    assert "When can you start?" in questions


# ----- drafting with Claude ---------------------------------------------------


def test_drafting_keeps_a_checked_answer(
    client: TestClient, engine, reader: FakeReader, job_id: int
) -> None:
    page = client.get(f"/job/{job_id}").text
    assert "Draft an answer (uses Claude)" in page
    assert f'formaction="/job/{job_id}/answer/draft"' in page

    response = client.post(
        f"/job/{job_id}/answer/draft",
        data={"question": "Tell us about backend work you did.", "words": "80"},
        headers=from_page(job_id),
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"http://testserver/job/{job_id}#answers"
    assert "in at most 80 words" in reader.calls[0]["user"]
    (row,) = rows(engine)
    assert row.source == "drafted" and row.answer == GOOD.text and row.fact_ids == [UPLOADS]
    page = client.get(f"/job/{job_id}").text
    assert "Drafted by Claude" in page and "Tell us about backend work you did." in page
    assert "Check before sending" not in page

    # An edit to the draft is checked again, as advice only.
    keep(client, job_id, answer_id=row.id, question=row.question, answer=BAD.text)
    (row,) = rows(engine)
    assert row.answer == BAD.text and row.remarks
    page = client.get(f"/job/{job_id}").text
    assert "Check before sending:" in page and "the number 40 is not in the cited facts" in page


def test_drafting_with_a_claude_problem_stores_nothing(
    home: Path, engine, job_id: int
) -> None:
    client = make_client(home, engine, model(problem=LlmProblem("bad_key")))
    response = client.post(
        f"/job/{job_id}/answer/draft", data={"question": "Why us?"}, headers=from_page(job_id)
    )
    assert response.status_code == 303
    assert message_of(response) == "Your Claude API key was rejected"
    assert response.headers["location"].endswith("#assist")
    assert rows(engine) == []


def test_a_draft_that_keeps_failing_the_check_is_not_kept(
    home: Path, engine, job_id: int
) -> None:
    reader = model(draft=BAD, fix=_Fix(number=1, text=BAD.text, fact_ids=BAD.fact_ids))
    client = make_client(home, engine, reader)
    response = client.post(
        f"/job/{job_id}/answer/draft", data={"question": "Why us?"}, headers=from_page(job_id)
    )
    assert response.status_code == 303
    assert "Write this one yourself" in message_of(response)
    assert len(reader.calls) == 2
    assert rows(engine) == []


def test_a_draft_fixed_on_the_second_try_is_kept(home: Path, engine, job_id: int) -> None:
    reader = model(draft=BAD, fix=_Fix(number=1, text=GOOD.text, fact_ids=GOOD.fact_ids))
    client = make_client(home, engine, reader)
    client.post(f"/job/{job_id}/answer/draft", data={"question": "Why us?"})
    (row,) = rows(engine)
    assert row.answer == GOOD.text and row.source == "drafted"


def test_drafting_needs_a_question(
    client: TestClient, engine, reader: FakeReader, job_id: int
) -> None:
    response = client.post(
        f"/job/{job_id}/answer/draft", data={"question": "   "}, headers=from_page(job_id)
    )
    assert message_of(response) == "Paste the question first"
    assert reader.calls == [] and rows(engine) == []


# ----- safety -----------------------------------------------------------------


def test_text_is_escaped(client: TestClient, job_id: int) -> None:
    nasty = '<script>alert("x")</script> Why?'
    keep(client, job_id, question=nasty, answer=nasty)
    page = client.get(f"/job/{job_id}", params={"q": nasty}).text
    assert "<script>alert" not in page
    escaped = "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; Why?"
    assert f'data-copy="{escaped}"' in page
    assert f'<div class="aq">{escaped} <span' in page
    assert f">{escaped}</textarea>" in page


def test_answers_stay_with_their_own_job(client: TestClient, engine, job_id: int) -> None:
    with Session(engine) as session:
        other = add(session, 2).id
    keep(client, job_id, question="Secret question for job one?", answer="Only for one.")
    (row,) = rows(engine)

    assert "Secret question for job one?" not in client.get(f"/job/{other}").text
    edit = {"answer_id": row.id, "question": "Moved?", "answer": "Changed."}
    assert keep(client, other, **edit).status_code == 404
    assert client.post(f"/job/{other}/answer/{row.id}/delete").status_code == 404
    assert client.post(f"/job/{other}/answer/{row.id}/bank").status_code == 404
    assert client.post(f"/job/{job_id}/answer/{row.id}/rename").status_code == 404
    assert client.post(f"/job/{job_id}/answer/9999/delete").status_code == 404
    assert client.post("/job/9999/answer", data={"question": "a", "answer": "b"}).status_code == 404
    (still,) = rows(engine)
    assert (still.listing_id, still.answer) == (job_id, "Only for one.")
    assert rows(engine, other) == []
