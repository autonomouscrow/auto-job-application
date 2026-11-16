"""Stand-ins for the network: a fake job site and a fake Claude."""

from __future__ import annotations

from jobtool.enrich.posting import Response
from jobtool.enrich.requirements import Extracted, SkillNeed
from jobtool.llm import LlmProblem, Reply

LONG_TEXT = (
    "Software Engineering Intern, Summer 2027. You will build backend services in Python "
    "and Go with the platform team. " + "We care about clear code and careful testing. " * 12
)


def page(text: str = LONG_TEXT, status: int = 200) -> str:
    return f"<html><body><main><h1>Job</h1><p>{text}</p></main></body></html>"


class FakeSite:
    """A getter that serves canned pages by URL and counts requests."""

    def __init__(self, pages: dict[str, tuple[int, str]] | None = None, default=(200, None)):
        self.pages = pages or {}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> Response:
        self.calls.append(url)
        status, body = self.pages.get(url, self.default)
        return Response(status, page() if body is None else body, url)


def extracted(**changes) -> Extracted:
    """A plausible reading of a posting. Override any field by keyword."""
    values = {
        "is_job_posting": True,
        "closed": False,
        "summary": "Build backend services with the platform team.",
        "role_type": "swe",
        "must_have_skills": [
            SkillNeed(name="Python", profile_skill="python"),
            SkillNeed(name="Go", profile_skill=None),
        ],
        "nice_to_have_skills": [SkillNeed(name="Docker", profile_skill=None)],
        "responsibilities": ["Build services", "Write tests"],
        "degree_levels": ["bachelor"],
        "graduation_earliest": None,
        "graduation_latest": None,
        "season": "Summer 2027",
        "start_date": "2027-06",
        "end_date": "2027-08",
        "duration_months": 3,
        "work_mode": "hybrid",
        "locations": ["Toronto, Canada"],
        "posting_language": "english",
        "required_languages": ["english"],
        "work_authorization": "not_stated",
        "citizenship_countries": [],
        "work_authorization_quote": None,
        "pay": "$40/hour",
        "deadline": None,
        "application_extras": ["cover letter"],
        "experience_relevance": 7,
        "relevance_reason": "Has built backend services before.",
        "relevant_fact_ids": [],
    }
    values.update(changes)
    return Extracted(**values)


class FakeReader:
    """Answers every call with the same reading, or raises the given problem.

    ``answers`` maps a schema class to the reply for it, or to a list of replies
    used in turn (the last one repeats), for code that makes several kinds of call.
    """

    model = "fake-reader"

    def __init__(
        self,
        answer: Extracted | None = None,
        problem: LlmProblem | None = None,
        answers: dict | None = None,
    ):
        self.answer = answer or extracted()
        self.problem = problem
        self.answers = answers or {}
        self.calls: list[dict] = []

    def check(self) -> None:
        if self.problem:
            raise self.problem

    def parse(
        self, *, system: str, user: str, schema, max_tokens: int = 4000, thinking: bool = False
    ) -> Reply:
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.problem:
            raise self.problem
        answer = self.answers.get(schema, self.answer)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        return Reply(answer.model_copy(deep=True), self.model, 1000, 200)


# ----- writing ----------------------------------------------------------------


def resume_draft(**changes):
    """A resume for the example applicant in which every bullet restates its facts."""
    from jobtool.resume.model import DraftBlock, DraftLine, DraftSkillRow, ResumeDraft

    def line(text: str, *fact_ids: str, priority: int = 2) -> DraftLine:
        return DraftLine(text=text, fact_ids=list(fact_ids), priority=priority)

    values = {
        "section_order": ["education", "experience", "projects"],
        "education": [
            DraftBlock(
                entry_id="edu-example-u",
                lines=[line("**Cumulative average of 88%**", "edu-example-u.gpa", priority=1)],
            )
        ],
        "experience": [
            DraftBlock(
                entry_id="exp-globex-2025",
                lines=[
                    line(
                        "Rewrote a nightly SQL reporting job as an incremental pipeline, "
                        "**cutting its run time from 50 minutes to 6 minutes**",
                        "exp-globex-2025.pipeline",
                        priority=1,
                    )
                ],
            ),
            DraftBlock(
                entry_id="exp-acme-2026",
                lines=[
                    line(
                        "Built a **Python** service that batches sensor uploads to S3, "
                        "cutting upload failures from 9% to 1%",
                        "exp-acme-2026.uploads",
                        priority=1,
                    ),
                    line(
                        "Wrote 120 pytest cases covering the upload path, run on every merge",
                        "exp-acme-2026.tests",
                        priority=3,
                    ),
                ],
            ),
        ],
        "projects": [
            DraftBlock(
                entry_id="proj-orderbook",
                lines=[
                    line(
                        "Implemented a price-time priority matching engine in **C++** that "
                        "processes 1.2 million orders per second",
                        "proj-orderbook.engine",
                    )
                ],
            )
        ],
        "skills": [
            DraftSkillRow(label="Languages", skill_ids=["python", "cpp", "sql"]),
            DraftSkillRow(label="Tools", skill_ids=["pytest", "aws-s3"]),
        ],
    }
    values.update(changes)
    return ResumeDraft(**values)


def letter_draft(**changes):
    """A cover letter body for the example applicant that passes the fact check."""
    from jobtool.resume.model import DraftParagraph, LetterDraft

    values = {
        "paragraphs": [
            DraftParagraph(
                text=(
                    "I am applying for the Software Engineer Intern role at Initech. At Acme "
                    "Robotics I built a Python service that batches sensor uploads to S3, "
                    "which cut upload failures from 9% to 1%."
                ),
                fact_ids=["exp-acme-2026.uploads"],
            ),
            DraftParagraph(
                text=(
                    "At Globex Analytics I rewrote a nightly SQL reporting job as an "
                    "incremental pipeline, reducing its run time from 50 minutes to 6 minutes."
                ),
                fact_ids=["exp-globex-2025.pipeline"],
            ),
            DraftParagraph(
                text="I would be glad to talk about the role whenever it suits you.",
                fact_ids=[],
            ),
        ]
    }
    values.update(changes)
    return LetterDraft(**values)


def writer(resume=None, letter=None, fixes=None) -> FakeReader:
    """A fake writing model: answers by schema, so one object serves every kind of call."""
    from jobtool.resume.model import LetterDraft, ResumeDraft
    from jobtool.resume.write import _Fixes

    return FakeReader(
        answers={
            ResumeDraft: resume or resume_draft(),
            LetterDraft: letter or letter_draft(),
            _Fixes: fixes or _Fixes(fixes=[]),
        }
    )
