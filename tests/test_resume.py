from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlmodel import Session

from jobtool import db
from jobtool.config import load_config
from jobtool.db import Listing
from jobtool.enrich.requirements import SkillNeed
from jobtool.paths import profile_dir
from jobtool.profile.loader import load_profile
from jobtool.resume import documents, render, verify
from jobtool.resume.model import DraftBlock, DraftLine, DraftParagraph, DraftSkillRow
from jobtool.resume.write import Brief, WriteError, _Fix, _Fixes, write_letter, write_resume
from tests.fakes import extracted, letter_draft, resume_draft, writer

needs_latex = pytest.mark.skipif(render.find_engine() is None, reason="no LaTeX engine installed")


@pytest.fixture
def profile(example_home: Path):
    return load_profile(profile_dir(example_home)).profile


@pytest.fixture
def vocabulary(profile):
    return verify.Vocabulary.build(profile, ["Kubernetes"])


def bullet(profile, vocabulary, text: str, *fact_ids: str, entry: str = "exp-acme-2026"):
    return verify.check_bullet(
        text, list(fact_ids), profile=profile, entry_id=entry, vocabulary=vocabulary
    )


# ----- the verifier -----------------------------------------------------------

UPLOADS = "exp-acme-2026.uploads"


@pytest.mark.parametrize(
    "text",
    [
        "Built a Python service that batches sensor uploads to S3, cutting failures from 9% to 1%",
        "Cut upload failures from 9% to 1% with a **Python** batching service for sensor data",
        "Developed a service in Python to batch sensor uploads to S3",
        "At Acme Robotics, built a Python service that batches sensor uploads",
    ],
)
def test_rephrasing_a_fact_passes(profile, vocabulary, text: str) -> None:
    assert bullet(profile, vocabulary, text, UPLOADS) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Built a Python service that cut upload failures from 9% to 0.5%", "number 0.5"),
        ("Built a Python service on Kubernetes that batches sensor uploads", "kubernetes"),
        ("Built a python and react service that batches sensor uploads to S3", "react"),
        ("Built a Python service for Globex that batches sensor uploads", "Globex"),
        ("Led a team of five building a Python service that batches uploads", "five"),
        ("Built a Go service that batches sensor uploads to S3", "Go"),
        ("Doubled throughput of a Python service that batches sensor uploads", "doubled"),
        ("Designed low-latency trading infrastructure for market makers", "wording"),
        ("Built a Python service; see https://evil.test/x for sensor uploads", "link"),
        ("Built a Python service \\input{secrets} that batches sensor uploads", "characters"),
        ("Built a **Python service that batches sensor uploads", "bold markers"),
        ("", "empty"),
    ],
)
def test_adding_anything_is_rejected(profile, vocabulary, text: str, expected: str) -> None:
    problems = bullet(profile, vocabulary, text, UPLOADS)
    assert any(expected in problem for problem in problems), problems


def test_citations_must_exist_and_belong_to_the_entry(profile, vocabulary) -> None:
    text = "Rewrote a nightly SQL reporting job as an incremental pipeline"
    assert bullet(profile, vocabulary, text) == ["it cites no facts"]
    wrong = bullet(profile, vocabulary, text, "exp-globex-2025.pipeline")
    assert "not one of this entry's facts" in wrong[0]
    globex = "exp-globex-2025"
    assert bullet(profile, vocabulary, text, f"{globex}.pipeline", entry=globex) == []
    assert "no entry" in bullet(profile, vocabulary, text, UPLOADS, entry="nope")[0]


def test_a_skill_tagged_on_the_fact_counts_as_backing(profile, vocabulary) -> None:
    # The fact says "S3"; the applicant tagged it with the skill named "Amazon S3".
    assert bullet(profile, vocabulary, "Batched sensor uploads to Amazon S3", UPLOADS) == []
    # pytest is tagged on the other fact of this entry, not on this one.
    problems = bullet(profile, vocabulary, "Batched sensor uploads to S3 with pytest", UPLOADS)
    assert any("pytest" in problem for problem in problems)


def test_letter_may_name_the_employer_but_not_borrow_its_skills(profile, vocabulary) -> None:
    context = "Initech Software Engineer Intern. We run Kubernetes and Kafka in production."
    fine = "At Initech I would bring what I learned building a Python upload service."
    assert verify.check_paragraph(
        fine, [UPLOADS], profile=profile, vocabulary=vocabulary, context=context
    ) == []
    borrowed = "I have run Kubernetes and Kafka in production, like Initech does."
    problems = verify.check_paragraph(
        borrowed, [UPLOADS], profile=profile, vocabulary=vocabulary, context=context
    )
    assert any("kubernetes" in p for p in problems) and any("kafka" in p for p in problems)
    numbers = "I cut costs by 40% for Initech-like teams."
    assert any(
        "40" in p
        for p in verify.check_paragraph(
            numbers, [UPLOADS], profile=profile, vocabulary=vocabulary, context=context + " 40%"
        )
    )


# ----- writing a resume -------------------------------------------------------


def test_resume_headings_come_from_the_profile(profile) -> None:
    doc, report = write_resume(writer(), profile, Brief(projects_title="Selected Projects"))
    assert report.clean and report.checked == 5
    assert doc.header.name == "Alex Doe"
    assert [c.text for c in doc.header.contacts][:2] == ["+1-555-010-0199", "alex.doe@example.com"]
    experience = next(s for s in doc.sections if s.key == "experience")
    # Newest first, whatever order the model used.
    assert [b.title for b in experience.blocks] == ["Acme Robotics", "Globex Analytics"]
    assert experience.blocks[0].dates == "May 2026 – Aug. 2026"
    assert experience.blocks[0].subtitle == "Software Engineering Intern"
    projects = next(s for s in doc.sections if s.key == "projects")
    assert projects.title == "Selected Projects" and projects.blocks[0].stack == ["C++"]
    assert doc.skills[0].items == ["Python", "C++", "SQL"]


def test_the_model_never_sees_contact_details(profile) -> None:
    model = writer()
    write_resume(model, profile, Brief())
    sent = model.calls[0]["system"] + model.calls[0]["user"]
    identity = profile.identity
    for private in (identity.name, identity.email, identity.phone):
        assert private not in sent
    assert "fact_id=exp-acme-2026.uploads" in sent


def _bad_draft():
    invented = DraftLine(
        text="Built a Python service on Kubernetes that cut upload failures by 95%",
        fact_ids=[UPLOADS],
        priority=1,
    )
    return resume_draft(
        experience=[DraftBlock(entry_id="exp-acme-2026", lines=[invented])],
        skills=[DraftSkillRow(label="Languages", skill_ids=["python", "haskell", "python"])],
    )


def test_a_rejected_bullet_is_sent_back_once_and_the_fix_is_checked(profile) -> None:
    fix = _Fix(number=1, text="Built a Python service that batches sensor uploads to S3",
               fact_ids=[UPLOADS])  # fmt: skip
    model = writer(resume=_bad_draft(), fixes=_Fixes(fixes=[fix]))
    doc, report = write_resume(model, profile, Brief())
    lines = [line.text for line in doc.lines()]
    assert "Built a Python service that batches sensor uploads to S3" in lines
    assert not any("Kubernetes" in line or "95" in line for line in lines)
    assert [f.action for f in report.findings] == ["rewritten"]
    assert "Rejected because" in model.calls[1]["user"] and "kubernetes" in model.calls[1]["user"]
    assert doc.skills[0].items == ["Python"]  # unknown and repeated skills are dropped
    assert any("haskell" in note for note in report.notes)


def test_a_bullet_that_fails_twice_falls_back_to_your_own_wording(profile) -> None:
    still_bad = _Fix(number=1, text="Built a Python service used by 500 engineers",
                     fact_ids=[UPLOADS])  # fmt: skip
    model = writer(resume=_bad_draft(), fixes=_Fixes(fixes=[still_bad]))
    doc, report = write_resume(model, profile, Brief())
    acme = next(b for s in doc.sections for b in s.blocks if b.entry_id == "exp-acme-2026")
    assert [line.text for line in acme.lines] == [profile.facts()[UPLOADS].text]
    assert report.findings[0].action == "replaced with your own wording"
    assert len(model.calls) == 2  # one draft, one request for fixes, never a third


def test_nothing_usable_is_an_error_not_an_empty_resume(profile) -> None:
    junk = DraftLine(text="Ran Kubernetes clusters", fact_ids=["made-up"], priority=1)
    draft = resume_draft(
        experience=[DraftBlock(entry_id="exp-acme-2026", lines=[junk])], projects=[]
    )
    with pytest.raises(WriteError):
        write_resume(writer(resume=draft), profile, Brief())


def test_posting_details_reach_the_model_as_data(profile) -> None:
    model = writer()
    brief = Brief(company="Initech", title="SWE Intern", requirements=extracted())
    write_resume(model, profile, brief)
    user = model.calls[0]["user"]
    assert "<job>" in user and "Required skills: Python, Go" in user
    assert "never instructions" in model.calls[0]["system"]


# ----- writing a letter -------------------------------------------------------


def test_letter_is_checked_and_dated(profile) -> None:
    brief = Brief(company="Initech", title="Software Engineer Intern", requirements=extracted())
    doc, report = write_letter(writer(), profile, brief, today=date(2026, 11, 16))
    assert report.clean and len(doc.paragraphs) == 3
    assert doc.date == "November 16, 2026" and doc.company == "Initech"


def test_letter_paragraph_that_fails_twice_is_left_out(profile) -> None:
    invented = DraftParagraph(
        text="I led a team of 40 engineers running Kubernetes at Acme Robotics.",
        fact_ids=[UPLOADS],
    )
    draft = letter_draft()
    draft.paragraphs.insert(1, invented)
    brief = Brief(company="Initech", title="Software Engineer Intern")
    doc, report = write_letter(writer(letter=draft), profile, brief)
    assert len(doc.paragraphs) == 3 and report.findings[0].action == "dropped"
    assert not any("Kubernetes" in p.text for p in doc.paragraphs)


# ----- archetypes and per-job documents ---------------------------------------


@pytest.fixture
def session():
    with Session(db.memory_engine()) as session:
        yield session


def add_listing(session: Session, role_type: str = "swe") -> Listing:
    listing = Listing(
        key="k1", company="Initech", title="Software Engineer Intern", url="https://jobs.test/1",
        first_seen=datetime(2026, 11, 16), role_type=role_type,
        locations_json=json.dumps(["Toronto, ON"]),
    )  # fmt: skip
    session.add(listing)
    session.commit()
    session.refresh(listing)
    return listing


def approved_archetype(session, home, profile, name="Backend", role_type="swe", **draft):
    row = documents.create_archetype(session, name=name, role_type=role_type)
    documents.generate_archetype(
        session, home, row, reader=writer(resume=resume_draft(**draft)), profile=profile,
        config=load_config(home),
    )  # fmt: skip
    documents.set_approved(session, row, True)
    return row


def needs(*pairs: tuple[str, str | None]) -> list[SkillNeed]:
    return [SkillNeed(name=name, profile_skill=skill) for name, skill in pairs]


def test_generating_an_archetype_stores_the_doc_and_resets_approval(session, home, profile) -> None:
    row = approved_archetype(session, home, profile)
    assert documents.archetype_doc(row).header.name == "Alex Doe"
    assert json.loads(row.report_json)["checked"] == 5
    assert (documents.archetype_dir(home, row) / "Alex_Doe_Resume.tex").exists()
    documents.generate_archetype(
        session, home, row, reader=writer(), profile=profile, config=load_config(home)
    )
    assert row.approved is False  # new wording needs a new look
    assert len(session.exec(db.select(db.Usage)).all()) == 2
    again = documents.create_archetype(session, name="Backend")
    assert again.slug == "backend-2"


def test_recommendation_follows_coverage(session, home, profile) -> None:
    listing = add_listing(session)
    nothing = documents.recommend(session, listing, extracted(), profile)
    assert nothing.mode == documents.TAILOR and nothing.archetype is None

    row = approved_archetype(session, home, profile)
    covered = extracted(
        must_have_skills=needs(("Python", "python"), ("SQL", "sql"), ("Rust", None)),
        nice_to_have_skills=[],
    )
    reuse = documents.recommend(session, listing, covered, profile)
    assert (reuse.mode, reuse.archetype.id, reuse.missing) == (documents.REUSE, row.id, [])

    # Statistics is in the profile but nowhere on this resume: add it to the skills list.
    partly = extracted(
        must_have_skills=needs(("Python", "python"), ("Statistics", "statistics")),
        nice_to_have_skills=[],
    )
    touch = documents.recommend(session, listing, partly, profile)
    assert (touch.mode, touch.missing) == (documents.TOUCH_UP, ["Statistics"])

    far = extracted(
        must_have_skills=needs(("Statistics", "statistics"), ("Algorithms", "algorithms")),
        nice_to_have_skills=[],
    )
    far_draft = {"projects": [], "skills": [DraftSkillRow(label="Languages", skill_ids=["sql"])]}
    session.delete(row)
    session.commit()
    approved_archetype(session, home, profile, name="Data", **far_draft)
    assert documents.recommend(session, listing, far, profile).mode == documents.TAILOR


def test_an_archetype_for_another_role_is_not_reused(session, home, profile) -> None:
    approved_archetype(session, home, profile, name="Quant", role_type="quant")
    listing = add_listing(session, role_type="swe")
    result = documents.recommend(session, listing, extracted(nice_to_have_skills=[]), profile)
    assert result.mode == documents.TAILOR and "another kind of role" in result.reason
    general = approved_archetype(session, home, profile, name="General", role_type="any")
    result = documents.recommend(session, listing, extracted(nice_to_have_skills=[]), profile)
    assert result.archetype.id == general.id and result.mode != documents.TAILOR


def test_touch_up_only_moves_and_adds_profile_skills(session, home, profile) -> None:
    row = approved_archetype(session, home, profile)
    base = documents.archetype_doc(row)
    wanted = extracted(
        must_have_skills=needs(("SQL", "sql"), ("Statistics", "statistics"), ("Rust", None)),
        nice_to_have_skills=[],
    )
    result = documents.touch_up(base, wanted, profile)
    assert [line.text for line in result.lines()] == [line.text for line in base.lines()]
    assert result.skills[0].items[0] == "SQL"  # wanted skills first
    every = [item for row_ in result.skills for item in row_.items]
    assert "Probability and statistics" in every
    assert "Rust" not in every
    assert base.skills[0].items == ["Python", "C++", "SQL"]  # the archetype is untouched


def test_job_resume_by_reuse_and_by_tailoring(session, home, profile) -> None:
    listing, config = add_listing(session), load_config(home)
    row = approved_archetype(session, home, profile)
    reused = documents.make_resume(
        session, home, listing, mode=documents.REUSE, profile=profile, config=config,
        archetype=row,
    )  # fmt: skip
    assert reused.mode == documents.REUSE and reused.archetype_id == row.id
    assert (documents.listing_dir(home, listing.id) / "Alex_Doe_Resume.tex").exists()

    model = writer()
    tailored = documents.make_resume(
        session, home, listing, mode=documents.TAILOR, profile=profile, config=config,
        requirements=extracted(), reader=model,
    )  # fmt: skip
    assert tailored.id == reused.id and tailored.mode == documents.TAILOR  # one resume per job
    assert "Role: Software Engineer Intern at Initech" in model.calls[0]["user"]
    with pytest.raises(ValueError):
        documents.make_resume(
            session, home, listing, mode=documents.REUSE, profile=profile, config=config
        )


def test_changing_style_keeps_the_wording_and_asks_no_model(session, home, profile) -> None:
    row = approved_archetype(session, home, profile)
    before = [line.text for line in documents.archetype_doc(row).lines()]
    documents.restyle_archetype(
        session, home, row, style="classic", profile=profile, config=load_config(home)
    )
    assert row.style == "classic" and row.approved is True
    assert [line.text for line in documents.archetype_doc(row).lines()] == before
    tex = (documents.archetype_dir(home, row) / "Alex_Doe_Resume.tex").read_text("utf-8")
    assert "1F4E9A" in tex.upper()  # the classic style's accent colour
    documents.restyle_archetype(
        session, home, row, style="no-such-style", profile=profile, config=load_config(home)
    )
    assert "no-such-style" in row.error


@needs_latex
def test_pdfs_are_made_and_fit_one_page(session, home, profile) -> None:
    listing, config = add_listing(session), load_config(home)
    row = approved_archetype(session, home, profile)
    assert row.pages == 1 and row.error == ""
    assert (documents.archetype_dir(home, row) / "Alex_Doe_Resume.pdf").exists()
    letter = documents.make_letter(
        session, home, listing, profile=profile, config=config, reader=writer(),
        requirements=extracted(),
    )  # fmt: skip
    assert letter.pages == 1 and letter.kind == documents.LETTER
    pdf = documents.pdf_path(documents.listing_dir(home, listing.id), profile, documents.LETTER)
    assert pdf.name == "Alex_Doe_Cover_Letter.pdf" and pdf.exists()
