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
    """Answers every call with the same reading, or raises the given problem."""

    model = "fake-reader"

    def __init__(self, answer: Extracted | None = None, problem: LlmProblem | None = None):
        self.answer = answer or extracted()
        self.problem = problem
        self.calls: list[dict] = []

    def check(self) -> None:
        if self.problem:
            raise self.problem

    def parse(self, *, system: str, user: str, schema, max_tokens: int = 4000) -> Reply:
        self.calls.append({"system": system, "user": user})
        if self.problem:
            raise self.problem
        return Reply(self.answer.model_copy(deep=True), self.model, 1000, 200)
