"""The apply kit: details to copy, the answer bank, and drafted answers."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session

from jobtool import assist, db, llm
from jobtool.db import JobAnswer, Listing, Usage
from jobtool.llm import LlmProblem
from jobtool.paths import profile_dir
from jobtool.profile.loader import ProfileError, load_profile
from jobtool.resume import documents
from jobtool.resume.write import WriteError, _Fix, _Fixes
from tests.fakes import FakeReader

GOOD = (
    "At Acme Robotics I built a Python service that batches sensor uploads to S3, "
    "which cut upload failures from 9% to 1%."
)
INVENTED = "At Acme Robotics I led a team of 14 engineers and shipped a Kubernetes platform."
QUESTION = "Describe a technical problem you solved."


def load(home: Path):
    return load_profile(profile_dir(home)).profile


@pytest.fixture
def profile(home: Path):
    return load(home)


@pytest.fixture
def session():
    with Session(db.memory_engine()) as session:
        yield session


@pytest.fixture
def listing(session: Session) -> Listing:
    row = Listing(
        key="k", company="Initech", title="Software Engineering Intern",
        url="https://jobs.test/1", first_seen=datetime(2026, 11, 16), batch=1, role_type="swe",
    )  # fmt: skip
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def model(*drafts: assist.AnswerDraft, fixes: _Fixes | None = None) -> FakeReader:
    return FakeReader(
        answers={assist.AnswerDraft: list(drafts), _Fixes: fixes or _Fixes(fixes=[])}
    )


def draft(text: str = GOOD, *fact_ids: str) -> assist.AnswerDraft:
    return assist.AnswerDraft(text=text, fact_ids=list(fact_ids or ["exp-acme-2026.uploads"]))


# ----- details ----------------------------------------------------------------


def test_details_come_straight_from_the_profile(profile) -> None:
    found = {(item.group, item.label): item.value for item in assist.details(profile)}
    assert found["You", "Full name"] == "Alex Doe"
    assert found["You", "First name"] == "Alex" and found["You", "Last name"] == "Doe"
    assert found["You", "Email"] == "alex.doe@example.com"
    assert found["Links", "GitHub"].startswith("https://github.com/")
    assert found["Education", "Graduation"] == "April 2028"
    assert found["Education", "Year of study"] == "3"
    assert ("Work history", "Acme Robotics, Software Engineering Intern") in found
    assert found["Work authorization", "Citizenship"] == "CA"
    assert found["Work authorization", "US"].startswith(
        "Authorized to work: no. Needs sponsorship: no."
    )
    assert "J-1" in found["Work authorization", "US"]


def test_empty_details_are_left_out(profile) -> None:
    profile.identity.phone = ""
    assert "Phone" not in {item.label for item in assist.details(profile)}


def test_document_files_lists_only_pdfs_that_exist(home: Path, profile) -> None:
    assert assist.document_files(home, profile, 7) == {"resume": None, "letter": None}
    path = documents.pdf_path(documents.listing_dir(home, 7), profile, documents.RESUME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF")
    files = assist.document_files(home, profile, 7)
    assert files["resume"] == path and files["letter"] is None


# ----- the answer bank --------------------------------------------------------


def test_similar_questions_are_matched_and_unlike_ones_are_not(profile) -> None:
    assert assist.similarity(
        "Are you willing to relocate?", "Would you relocate for this internship?"
    ) >= assist.SUGGEST_AT  # fmt: skip
    assert assist.similarity("Will you require visa sponsorship?", "Do you need sponsorship?") > 0.5
    assert assist.similarity(
        "Are you legally authorized to work in the United States?", "Are you authorized to work?"
    ) >= assist.SUGGEST_AT  # fmt: skip
    assert assist.similarity("Why do you want to work here?", "Where do you want to work?") == 0
    assert assist.similarity("What is your GPA?", "Are you willing to relocate?") == 0
    assert assist.similarity("", "Anything") == 0

    found = assist.suggest(profile, "Tell me about a project you're proud of")
    assert [item.answer.id for item in found] == ["ans-proud-project"]
    assert assist.suggest(profile, "What is your favourite colour?") == []


def test_add_to_bank_appends_and_keeps_the_file_as_written(home: Path) -> None:
    path = assist.answers_path(home)
    before = path.read_text(encoding="utf-8")
    new_id = assist.add_to_bank(
        home, "  Why do you want\n this role? ", "Because: it fits. #1 choice",
        ["exp-acme-2026.uploads", "no-such-fact"],
    )  # fmt: skip
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before), "nothing already in the file may be rewritten"
    saved = next(item for item in load(home).answers if item.id == new_id)
    assert new_id == "ans-why-do-you-want-this-role"
    assert saved.question == "Why do you want this role?"
    assert saved.answer == "Because: it fits. #1 choice"
    assert saved.facts == ["exp-acme-2026.uploads"]


def test_add_to_bank_refuses_repeats_and_blanks_and_numbers_clashing_ids(home: Path) -> None:
    with pytest.raises(ValueError, match="already"):
        assist.add_to_bank(home, "are you willing to relocate for this internship", "Yes", [])
    with pytest.raises(ValueError):
        assist.add_to_bank(home, "A question?", "   ", [])
    long = "What draws you to building trading systems at a firm like"
    first = assist.add_to_bank(home, f"{long} ours?", "One.", [])
    second = assist.add_to_bank(home, f"{long} this one?", "Two.", [])
    assert second == f"{first}-2"
    assert len(load(home).answers) == 4


def test_add_to_bank_starts_the_file_when_there_is_none(home: Path) -> None:
    assist.answers_path(home).unlink()
    assist.add_to_bank(home, "Why us?", "Because.", [])
    assert [item.question for item in load(home).answers] == ["Why us?"]


def test_add_to_bank_puts_the_file_back_if_the_profile_breaks(home: Path, monkeypatch) -> None:
    path = assist.answers_path(home)
    before = path.read_text(encoding="utf-8")
    real, calls = assist.load_profile, []

    def flaky(folder):
        calls.append(folder)
        if len(calls) == 2:
            raise ProfileError(["answers.yaml: broken"])
        return real(folder)

    monkeypatch.setattr(assist, "load_profile", flaky)
    with pytest.raises(ProfileError):
        assist.add_to_bank(home, "Why us?", "Because.", [])
    assert path.read_text(encoding="utf-8") == before


# ----- drafting ---------------------------------------------------------------


def test_a_draft_that_passes_is_kept_with_its_facts(session, listing, profile) -> None:
    reader = model(draft())
    row = assist.draft_answer(session, reader, profile, listing, f"  {QUESTION}  ")
    assert (row.question, row.answer, row.source) == (QUESTION, GOOD, assist.DRAFTED)
    assert row.fact_ids == ["exp-acme-2026.uploads"] and row.remarks == []
    assert assist.answers_for(session, listing.id) == [row]
    assert len(reader.calls) == 1
    assert len(session.exec(db.select(Usage)).all()) == 1


def test_the_model_sees_the_profile_but_no_contact_details(session, listing, profile) -> None:
    reader = model(draft())
    assist.draft_answer(session, reader, profile, listing, QUESTION, words=5000)
    sent = reader.calls[0]["system"] + reader.calls[0]["user"]
    assert "exp-acme-2026.uploads" in sent and "<saved_answers>" in sent
    assert f"<question>\n{QUESTION}\n</question>" in sent and "Initech" in sent
    assert "at most 400 words" in sent
    for private in ("alex.doe@example.com", "555-010", "github.com/alexdoe"):
        assert private not in sent


def test_an_invented_draft_is_sent_back_once_and_the_fix_is_kept(
    session, listing, profile
) -> None:
    fixes = _Fixes(fixes=[_Fix(number=1, text=GOOD, fact_ids=["exp-acme-2026.uploads"])])
    reader = model(draft(INVENTED), fixes=fixes)
    row = assist.draft_answer(session, reader, profile, listing, QUESTION)
    assert row.answer == GOOD
    assert [call["schema"] for call in reader.calls] == [assist.AnswerDraft, _Fixes]
    assert "14" in reader.calls[1]["user"] or "Kubernetes" in reader.calls[1]["user"]
    assert len(session.exec(db.select(Usage)).all()) == 2


@pytest.mark.parametrize(
    "fixes",
    [_Fixes(fixes=[]), _Fixes(fixes=[_Fix(number=1, text=INVENTED, fact_ids=[])])],
)
def test_a_draft_that_still_invents_is_refused(session, listing, profile, fixes) -> None:
    with pytest.raises(WriteError, match="Write this one yourself"):
        reader = model(draft(INVENTED), fixes=fixes)
        assist.draft_answer(session, reader, profile, listing, QUESTION)
    assert assist.answers_for(session, listing.id) == []


def test_a_claude_problem_is_recorded_for_the_banner(session, listing, profile, home) -> None:
    reader = FakeReader(problem=LlmProblem(llm.BAD_KEY))
    with pytest.raises(LlmProblem):
        assist.draft_answer(session, reader, profile, listing, QUESTION)
    assert "bad_key" in db.get_meta(session, "llm_status")
    assert session.exec(db.select(JobAnswer)).all() == []


def test_an_empty_question_never_reaches_the_model(session, listing, profile) -> None:
    reader = model(draft())
    with pytest.raises(WriteError):
        assist.draft_answer(session, reader, profile, listing, "  \n ")
    assert reader.calls == []


def test_remarks_accept_the_applicants_own_saved_words(profile) -> None:
    own = "I built a limit order book simulator with a C++ matching engine."
    assert assist.remarks_on(profile, own, [], context="") == []
    assert assist.remarks_on(profile, GOOD, ["exp-acme-2026.uploads"], context="") == []
    assert assist.remarks_on(profile, INVENTED, ["exp-acme-2026.uploads"], context="")
    assert assist.remarks_on(profile, GOOD, ["nope"], context="")


def test_saving_updates_the_same_row(session, listing) -> None:
    row = assist.save_answer(session, listing, " Why   us? ", " Because. ", ["a"], assist.BANK)
    assert (row.question, row.answer, row.fact_ids) == ("Why us?", "Because.", ["a"])
    again = assist.save_answer(
        session, listing, "Why us?", "Because it fits.", remarks=["check"], row=row
    )
    assert again.id == row.id and again.source == assist.BANK
    assert again.fact_ids == ["a"] and again.remarks == ["check"]
    assert len(assist.answers_for(session, listing.id)) == 1
