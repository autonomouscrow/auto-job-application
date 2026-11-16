"""The resume and cover letter pages: archetypes, the job card section, and the files."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobtool import db
from jobtool.db import Archetype, Document, JobText, Listing, Requirements
from jobtool.resume import render
from jobtool.resume.model import DraftBlock, DraftLine
from jobtool.web.app import create_app
from tests.fakes import FakeReader, extracted, resume_draft, writer

UPLOADS = "exp-acme-2026.uploads"
HAS_ENGINE = render.find_engine() is not None


def add(session: Session, n: int, **extra) -> Listing:
    listing = Listing(
        key=f"key-{n}", company=f"Company {n}", title=f"Software Intern {n}",
        url=f"https://jobs.test/{n}", first_seen=datetime(2026, 11, 16), batch=1,
        score=50, role_type="swe", locations_json=json.dumps(["NYC"]),
        score_json=json.dumps({"provisional": True, "components": {"role type": 50}}),
    )  # fmt: skip
    for name, value in extra.items():
        setattr(listing, name, value)
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def give_requirements(session: Session, listing: Listing, **changes) -> None:
    """Store a reading of the posting, as the reading step would, with no network."""
    at = datetime(2026, 11, 16, 9)
    session.add(
        JobText(
            listing_id=listing.id, status="ok", fetched_at=at, text="A posting.", content_hash="abc"
        )  # fmt: skip
    )
    session.add(
        Requirements(
            listing_id=listing.id,
            content_hash="abc",
            model="fake",
            data_json=extracted(**changes).model_dump_json(),
            created_at=at,
        )  # fmt: skip
    )
    session.commit()


@pytest.fixture
def engine():
    return db.memory_engine()


@pytest.fixture
def model() -> FakeReader:
    return writer()


def make_client(home: Path, engine, model: FakeReader) -> TestClient:
    return TestClient(
        create_app(home, engine, writer_factory=lambda session: model), follow_redirects=False
    )


@pytest.fixture
def client(home: Path, engine, model: FakeReader) -> TestClient:
    return make_client(home, engine, model)


def new_archetype(client: TestClient, name: str = "Backend SWE", **fields) -> int:
    data = {"name": name, "role_type": "swe", "focus": "Backend services", "style": "", **fields}
    response = client.post("/archetypes", data=data)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def generated(client: TestClient, **fields) -> int:
    archetype_id = new_archetype(client, **fields)
    assert client.post(f"/archetypes/{archetype_id}/generate").status_code == 303
    return archetype_id


def test_nav_and_empty_page(client: TestClient) -> None:
    page = client.get("/archetypes").text
    assert 'href="/archetypes"' in page and "Resumes" in page
    assert "No archetypes yet" in page
    assert "What should this resume lean toward?" in page


def test_generating_shows_bullets_their_facts_and_the_report(
    client: TestClient, engine, home: Path
) -> None:
    archetype_id = new_archetype(client)
    page = client.get(f"/archetypes/{archetype_id}").text
    assert "not generated yet" in page and "Write it with Claude" in page
    assert "Where every line comes from" not in page

    assert client.post(f"/archetypes/{archetype_id}/generate").status_code == 303
    page = client.get(f"/archetypes/{archetype_id}").text
    assert "Write it again" in page and "replaces the current text" in page
    assert "draft" in page and "Approve" in page
    assert "Where every line comes from" in page
    assert "<strong>Python</strong>" in page
    from jobtool.profile.loader import load_profile

    fact = load_profile(home / "profile").profile.facts()[UPLOADS].text
    assert f"From your profile: {fact}" in page
    assert "sentences checked" in page and "Every sentence passed" in page

    listing_page = client.get("/archetypes").text
    assert "Backend SWE" in listing_page and "draft" in listing_page


def test_an_invented_number_is_reported(home: Path, engine) -> None:
    invented = DraftLine(
        text="Built a Python service on Kubernetes that cut upload failures by 95%",
        fact_ids=[UPLOADS], priority=1,
    )  # fmt: skip
    model = writer(resume=resume_draft(
        experience=[DraftBlock(entry_id="exp-acme-2026", lines=[invented])]
    ))  # fmt: skip
    client = make_client(home, engine, model)
    archetype_id = generated(client)
    page = client.get(f"/archetypes/{archetype_id}").text
    assert "Every sentence passed" not in page
    assert "Kubernetes that cut upload failures by 95%" in page  # shown as the rejected text
    assert "What was done: replaced with your own wording." in page


def test_approved_archetype_is_reused_for_a_job(client: TestClient, engine, model) -> None:
    with Session(engine) as session:
        job = add(session, 1)
        give_requirements(session, job)
        job_id = job.id
    card = client.get(f"/job/{job_id}").text
    assert "Resume and cover letter" in card
    assert "Tailor for this job (uses Claude)" in card
    assert "Set up an archetype resume to reuse" in card

    archetype_id = generated(client)
    assert client.post(f"/archetypes/{archetype_id}/approve").status_code == 303
    assert "approved" in client.get("/archetypes").text
    card = client.get(f"/job/{job_id}").text
    # The recommended way comes first, as the primary button.
    first = re.search(r'<button name="mode" value="([^"]+)" class="primary"', card)
    assert first and first.group(1) == "reuse"
    assert f'<option value="{archetype_id}" selected>' in card

    calls = len(model.calls)
    response = client.post(
        f"/job/{job_id}/resume", data={"mode": "reuse", "archetype_id": archetype_id}
    )
    assert response.status_code == 303
    assert len(model.calls) == calls  # reusing asks no model
    card = client.get(f"/job/{job_id}").text
    assert "Reused Backend SWE as it is." in card
    assert f'href="/job/{job_id}/resume.tex"' in card
    assert "Make it again" in card
    with Session(engine) as session:
        document = session.exec(select(Document)).one()
        assert document.mode == "reuse" and document.archetype_id == archetype_id
    if HAS_ENGINE:
        assert f'href="/job/{job_id}/resume.pdf"' in card
    # The same section shows on the queue.
    assert "Reused Backend SWE as it is." in client.get("/").text


def test_tailor_works_with_no_archetypes(client: TestClient, engine, model) -> None:
    with Session(engine) as session:
        job_id = add(session, 1).id
    response = client.post(f"/job/{job_id}/resume", data={"mode": "tailor"})
    assert response.status_code == 303
    assert len(model.calls) >= 1
    card = client.get(f"/job/{job_id}").text
    assert "Written for this job by Claude." in card
    assert "sentences checked" in card


def test_files_are_404_before_anything_is_made(client: TestClient, engine) -> None:
    with Session(engine) as session:
        job_id = add(session, 1).id
    assert client.get(f"/job/{job_id}/resume.pdf").status_code == 404
    assert client.get(f"/job/{job_id}/letter.tex").status_code == 404
    archetype_id = new_archetype(client)
    assert client.get(f"/archetypes/{archetype_id}/resume.pdf").status_code == 404


@pytest.mark.skipif(not HAS_ENGINE, reason="no LaTeX engine installed")
def test_pdfs_are_served(client: TestClient, engine) -> None:
    with Session(engine) as session:
        job_id = add(session, 1).id
    client.post(f"/job/{job_id}/resume", data={"mode": "tailor"})
    response = client.get(f"/job/{job_id}/resume.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    archetype_id = generated(client)
    page = client.get(f"/archetypes/{archetype_id}").text
    assert f'<iframe src="/archetypes/{archetype_id}/resume.pdf"' in page
    pdf = client.get(f"/archetypes/{archetype_id}/resume.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    tex = client.get(f"/archetypes/{archetype_id}/resume.tex")
    assert tex.status_code == 200 and "documentclass" in tex.text


def test_changing_style_keeps_wording_and_asks_no_model(client: TestClient, engine, model) -> None:
    styles = render.list_styles(None)
    assert len(styles) >= 2, "needs two built-in styles"
    archetype_id = generated(client)
    with Session(engine) as session:
        job_id = add(session, 1).id
    client.post(f"/job/{job_id}/resume", data={"mode": "tailor"})

    def bullets(doc_json: str) -> list:
        return [b["lines"] for s in json.loads(doc_json)["sections"] for b in s["blocks"]]

    def lines() -> tuple[list, list]:
        with Session(engine) as session:
            archetype = session.get(Archetype, archetype_id)
            document = session.exec(select(Document)).one()
            return bullets(archetype.doc_json), bullets(document.doc_json)

    before, calls = lines(), len(model.calls)
    other = styles[-1].name
    response = client.post(f"/archetypes/{archetype_id}/style", data={"style": other})
    assert response.status_code == 303
    response = client.post(f"/job/{job_id}/document/resume/style", data={"style": other})
    assert response.status_code == 303
    assert lines() == before
    assert len(model.calls) == calls
    with Session(engine) as session:
        assert session.get(Archetype, archetype_id).style == other
        assert session.exec(select(Document)).one().style == other
    page = client.get(f"/archetypes/{archetype_id}").text
    assert f'<option value="{other}"' in page and "Same wording, no cost." in page


def test_cover_letter_is_written(client: TestClient, engine, model) -> None:
    with Session(engine) as session:
        job_id = add(session, 1).id
    card = client.get(f"/job/{job_id}").text
    assert "Write a cover letter (uses Claude)" in card
    assert client.post(f"/job/{job_id}/letter").status_code == 303
    with Session(engine) as session:
        letter = session.exec(select(Document).where(Document.kind == "letter")).one()
        assert letter.mode == "tailor"
    card = client.get(f"/job/{job_id}").text
    assert f'href="/job/{job_id}/letter.tex"' in card
    assert f'action="/job/{job_id}/document/letter/approve"' in card
    assert client.post(f"/job/{job_id}/document/letter/approve").status_code == 303
    card = client.get(f"/job/{job_id}").text
    assert f'action="/job/{job_id}/document/letter/unapprove"' in card


def test_model_text_is_escaped_but_bold_is_kept(client: TestClient, engine) -> None:
    archetype_id = generated(client)
    with Session(engine) as session:
        archetype = session.get(Archetype, archetype_id)
        doc = json.loads(archetype.doc_json)
        doc["sections"][0]["blocks"][0]["lines"][0]["text"] = (
            '<script>alert("x")</script> led **a team of <b>four</b>**'
        )
        archetype.doc_json = json.dumps(doc)
        session.add(archetype)
        session.commit()
    page = client.get(f"/archetypes/{archetype_id}").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; led " in page
    assert "<strong>a team of &lt;b&gt;four&lt;/b&gt;</strong>" in page


def test_editing_and_deleting_an_archetype(client: TestClient, engine) -> None:
    archetype_id = new_archetype(client, name="Quant research")
    response = client.post(
        f"/archetypes/{archetype_id}/edit",
        data={"name": "Quant trading", "role_type": "quant", "focus": "Probability"},
    )
    assert response.status_code == 303
    page = client.get(f"/archetypes/{archetype_id}").text
    assert "Quant trading" in page and "Probability" in page
    assert '<option value="quant" selected>' in page

    response = client.post(f"/archetypes/{archetype_id}/delete")
    assert response.status_code == 303 and response.headers["location"] == "/archetypes"
    assert "Quant trading" not in client.get("/archetypes").text
    assert client.get(f"/archetypes/{archetype_id}").status_code == 404
