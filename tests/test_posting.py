from __future__ import annotations

import json

import httpx
import pytest

from jobtool.enrich.posting import (
    DEAD,
    ERROR,
    MAX_TEXT,
    OK,
    UNREADABLE,
    Posting,
    Response,
    fetch_posting,
    html_to_text,
)

# ----- a fake getter ----------------------------------------------------------


class FakeGet:
    """Answers from a dict of URL -> (status, body) or (status, body, final_url)."""

    def __init__(self, routes: dict[str, tuple]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> Response:
        self.calls.append((url, headers))
        if url not in self.routes:
            return Response(599, "unregistered url in test", url)
        status, body, *final = self.routes[url]
        if not isinstance(body, str):
            body = json.dumps(body)
        return Response(status, body, final[0] if final else url)

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


DUTIES = (
    "You will join a small team that builds the scheduling engine behind our delivery "
    "network. In your first months you will pair with senior engineers on route planning, "
    "write well-tested Python services, review pull requests, and take part in the on-call "
    "rotation once you feel ready. We care about clear writing, careful testing, and a calm "
    "approach to incidents. The internship runs for twelve weeks over the summer and ends with "
    "a short presentation of your project to the wider engineering group."
)
SKILLS = (
    "Experience with Python or another typed language, familiarity with SQL databases, "
    "and curiosity about logistics problems. Coursework in algorithms is a plus."
)

# A filler page with enough words to pass MIN_TEXT, used for generic pages.
LIVE_PAGE = f"""<!doctype html>
<html><head><title>Software Engineering Intern | Quillfern Logistics</title>
<style>body {{ font-family: sans-serif; }}</style>
<script>window.dataLayer = [{{"event": "page_view"}}];</script></head>
<body>
<nav><a href="/">Home</a> <a href="/jobs">All jobs</a> NAVIGATION-LINK</nav>
<main>
<h1>Software Engineering Intern</h1>
<p>{DUTIES}</p>
<h2>What we look for</h2>
<ul><li>Python or another typed language</li><li>SQL databases &amp; data modelling</li></ul>
<p>{SKILLS}</p>
</main>
<footer>Copyright Quillfern Logistics FOOTER-TEXT</footer>
</body></html>"""

# ----- Greenhouse ---------------------------------------------------------------

GH_API = "https://boards-api.greenhouse.io/v1/boards/quillfern/jobs/4012345008"
GH_JOB = {
    "id": 4012345008,
    "internal_job_id": 3012345,
    "title": "Software Engineering Intern",
    "updated_at": "2026-10-02T12:00:00-04:00",
    "requisition_id": "ENG-104",
    "location": {"name": "Austin, TX"},
    "absolute_url": "https://job-boards.greenhouse.io/quillfern/jobs/4012345008",
    "metadata": None,
    "content": (
        "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;"
        + DUTIES
        + "&lt;/p&gt;&lt;h3&gt;You have&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; SQL"
        "&lt;/li&gt;&lt;li&gt;R&amp;amp;D curiosity&lt;/li&gt;&lt;/ul&gt;"
    ),
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 2, "name": "Austin"}],
}


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/quillfern/jobs/4012345008",
        "https://job-boards.greenhouse.io/quillfern/jobs/4012345008?gh_src=abc123",
    ],
)
def test_greenhouse_live(url: str) -> None:
    get = FakeGet({GH_API: (200, GH_JOB)})
    posting = fetch_posting(url, get)
    assert posting.status == OK
    assert posting.ats == "greenhouse"
    assert posting.title == "Software Engineering Intern"
    assert "Location: Austin, TX" in posting.text
    assert "scheduling engine" in posting.text
    # Escaped HTML is decoded once into tags and once more into text.
    assert "- Python & SQL" in posting.text
    assert "R&D curiosity" in posting.text
    assert "&amp;" not in posting.text and "<p>" not in posting.text
    assert get.urls == [GH_API]


def test_greenhouse_eu_host() -> None:
    api = "https://boards-api.eu.greenhouse.io/v1/boards/quillfern/jobs/4012345008"
    get = FakeGet({api: (200, GH_JOB)})
    posting = fetch_posting("https://job-boards.eu.greenhouse.io/quillfern/jobs/4012345008", get)
    assert posting.status == OK
    assert get.urls == [api]


def test_greenhouse_404_is_dead() -> None:
    get = FakeGet({GH_API: (404, {"status": 404, "error": "Job not found"})})
    posting = fetch_posting("https://boards.greenhouse.io/quillfern/jobs/4012345008", get)
    assert posting.status == DEAD
    assert posting.ats == "greenhouse"


def test_greenhouse_server_error_falls_back_to_page() -> None:
    url = "https://boards.greenhouse.io/quillfern/jobs/4012345008"
    get = FakeGet({GH_API: (503, "unavailable"), url: (200, LIVE_PAGE)})
    posting = fetch_posting(url, get)
    assert posting.status == OK
    assert posting.ats == "web"
    assert "scheduling engine" in posting.text
    assert get.urls == [GH_API, url]


# ----- Lever --------------------------------------------------------------------

LEVER_ID = "5f3c2a1b-9d8e-4f70-a1b2-c3d4e5f60718"
LEVER_API = f"https://api.lever.co/v0/postings/brightmoss/{LEVER_ID}"
LEVER_JOB = {
    "id": LEVER_ID,
    "text": "Data Analyst Intern",
    "categories": {
        "commitment": "Internship",
        "department": "Analytics",
        "location": "Remote - US",
        "team": "Insights",
        "allLocations": ["Remote - US"],
    },
    "country": "US",
    "createdAt": 1760000000000,
    "description": f"<div>{DUTIES}</div>",
    "descriptionPlain": DUTIES,
    "lists": [
        {"text": "What you'll need", "content": "<li>SQL</li><li>Spreadsheets</li>"},
        {"text": "Nice to have", "content": "<li>dbt</li>"},
    ],
    "additional": "<div>We sponsor equipment for remote work.</div>",
    "additionalPlain": "We sponsor equipment for remote work.",
    "hostedUrl": f"https://jobs.lever.co/brightmoss/{LEVER_ID}",
    "applyUrl": f"https://jobs.lever.co/brightmoss/{LEVER_ID}/apply",
    "workplaceType": "remote",
}


def test_lever_live() -> None:
    get = FakeGet({LEVER_API: (200, LEVER_JOB)})
    posting = fetch_posting(f"https://jobs.lever.co/brightmoss/{LEVER_ID}", get)
    assert posting.status == OK
    assert posting.ats == "lever"
    assert posting.title == "Data Analyst Intern"
    for piece in (
        "Location: Remote - US",
        "Commitment: Internship",
        "Workplace: remote",
        "scheduling engine",
        "What you'll need",
        "- SQL",
        "- Spreadsheets",
        "We sponsor equipment",
    ):
        assert piece in posting.text
    assert get.urls == [LEVER_API]


def test_lever_eu_host_and_apply_suffix() -> None:
    api = f"https://api.eu.lever.co/v0/postings/brightmoss/{LEVER_ID}"
    get = FakeGet({api: (200, LEVER_JOB)})
    posting = fetch_posting(f"https://jobs.eu.lever.co/brightmoss/{LEVER_ID}/apply", get)
    assert posting.status == OK
    assert get.urls == [api]


def test_lever_404_is_dead() -> None:
    get = FakeGet({LEVER_API: (404, {"ok": False, "error": "Document not found"})})
    posting = fetch_posting(f"https://jobs.lever.co/brightmoss/{LEVER_ID}", get)
    assert posting.status == DEAD
    assert posting.ats == "lever"


def test_lever_rate_limit_falls_back_to_page() -> None:
    url = f"https://jobs.lever.co/brightmoss/{LEVER_ID}"
    get = FakeGet({LEVER_API: (429, "slow down"), url: (200, LIVE_PAGE)})
    posting = fetch_posting(url, get)
    assert posting.status == OK
    assert posting.ats == "web"
    assert get.urls == [LEVER_API, url]


# ----- Ashby --------------------------------------------------------------------

ASHBY_ID = "0a1b2c3d-4e5f-4061-8728-394a5b6c7d8e"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/tidalforge"
ASHBY_URL = f"https://jobs.ashbyhq.com/tidalforge/{ASHBY_ID}"
ASHBY_OTHER = {
    "id": "99999999-4e5f-4061-8728-394a5b6c7d8e",
    "title": "Account Executive",
    "location": "New York",
    "descriptionPlain": "Sell things.",
    "descriptionHtml": "<p>Sell things.</p>",
    "workplaceType": "OnSite",
}
ASHBY_JOB = {
    "id": ASHBY_ID,
    "title": "Platform Engineering Intern",
    "department": "Engineering",
    "team": "Platform",
    "employmentType": "Intern",
    "location": "Berlin",
    "isListed": True,
    "isRemote": False,
    "workplaceType": "Hybrid",
    "descriptionPlain": "",
    "descriptionHtml": f"<p>{DUTIES}</p><ul><li>Go or Rust</li></ul>",
    "publishedAt": "2026-09-30T10:00:00.000+00:00",
    "jobUrl": ASHBY_URL,
    "applyUrl": f"{ASHBY_URL}/application",
}


def test_ashby_live_uses_html_when_plain_is_empty() -> None:
    get = FakeGet({ASHBY_API: (200, {"apiVersion": "1", "jobs": [ASHBY_OTHER, ASHBY_JOB]})})
    posting = fetch_posting(ASHBY_URL, get)
    assert posting.status == OK
    assert posting.ats == "ashby"
    assert posting.title == "Platform Engineering Intern"
    assert "Location: Berlin" in posting.text
    assert "Workplace: Hybrid" in posting.text
    assert "scheduling engine" in posting.text
    assert "- Go or Rust" in posting.text
    assert "Sell things" not in posting.text
    assert get.urls == [ASHBY_API]


def test_ashby_job_missing_from_board_is_dead() -> None:
    get = FakeGet({ASHBY_API: (200, {"apiVersion": "1", "jobs": [ASHBY_OTHER]})})
    posting = fetch_posting(ASHBY_URL, get)
    assert posting.status == DEAD
    assert posting.ats == "ashby"


def test_ashby_404_is_dead() -> None:
    get = FakeGet({ASHBY_API: (404, "")})
    assert fetch_posting(ASHBY_URL, get).status == DEAD


def test_ashby_server_error_falls_back_to_page() -> None:
    get = FakeGet({ASHBY_API: (500, ""), ASHBY_URL: (200, LIVE_PAGE)})
    posting = fetch_posting(ASHBY_URL, get)
    assert posting.status == OK
    assert posting.ats == "web"


def test_ashby_answer_without_jobs_key_is_not_dead() -> None:
    # A 200 that is not a job board at all says nothing about the job.
    get = FakeGet({ASHBY_API: (200, {"success": False}), ASHBY_URL: (200, LIVE_PAGE)})
    posting = fetch_posting(ASHBY_URL, get)
    assert posting.status == OK
    assert posting.ats == "web"


# ----- Workday ------------------------------------------------------------------

WD_HOST = "https://northwind-rail.wd5.myworkdayjobs.com"
WD_PATH = "/job/Denver-CO/Mechanical-Engineering-Intern_R-104233"
WD_API = f"{WD_HOST}/wday/cxs/northwind-rail/Careers{WD_PATH}"
WD_JOB = {
    "jobPostingInfo": {
        "id": "a1b2c3",
        "title": "Mechanical Engineering Intern",
        "jobDescription": f"<p><b>About the role</b></p><p>{DUTIES}</p><ul><li>CAD</li></ul>",
        "location": "Denver, CO",
        "postedOn": "Posted 3 Days Ago",
        "startDate": "2026-11-13",
        "timeType": "Full time",
        "jobReqId": "R-104233",
        "jobPostingId": "Mechanical-Engineering-Intern_R-104233",
        "country": {"descriptor": "United States of America", "id": "bc33"},
        "canApply": True,
        "externalUrl": f"{WD_HOST}/Careers{WD_PATH}",
    },
    "hiringOrganization": {"name": "Northwind Rail"},
}


@pytest.mark.parametrize(
    "url",
    [
        f"{WD_HOST}/en-US/Careers{WD_PATH}",
        f"{WD_HOST}/Careers{WD_PATH}",
        f"{WD_HOST}/en-US/Careers{WD_PATH}?source=campus",
    ],
)
def test_workday_live(url: str) -> None:
    get = FakeGet({WD_API: (200, WD_JOB)})
    posting = fetch_posting(url, get)
    assert posting.status == OK
    assert posting.ats == "workday"
    assert posting.title == "Mechanical Engineering Intern"
    assert "Location: Denver, CO" in posting.text
    assert "Time type: Full time" in posting.text
    assert "- CAD" in posting.text
    assert get.calls == [(WD_API, {"Accept": "application/json"})]


@pytest.mark.parametrize("status", [404, 410])
def test_workday_gone_is_dead(status: int) -> None:
    get = FakeGet({WD_API: (status, "")})
    posting = fetch_posting(f"{WD_HOST}/en-US/Careers{WD_PATH}", get)
    assert posting.status == DEAD
    assert posting.ats == "workday"


def test_workday_error_falls_back_to_page() -> None:
    url = f"{WD_HOST}/Careers{WD_PATH}"
    get = FakeGet({WD_API: (422, ""), url: (200, LIVE_PAGE)})
    posting = fetch_posting(url, get)
    assert posting.status == OK
    assert posting.ats == "web"


def test_workday_without_posting_info_falls_back_to_page() -> None:
    url = f"{WD_HOST}/Careers{WD_PATH}"
    get = FakeGet({WD_API: (200, {"errorCode": "NOT_FOUND"}), url: (200, LIVE_PAGE)})
    assert fetch_posting(url, get).ats == "web"


# ----- surprising ATS shapes ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "api", "body"),
    [
        ("https://boards.greenhouse.io/quillfern/jobs/4012345008", GH_API, [GH_JOB]),
        ("https://boards.greenhouse.io/quillfern/jobs/4012345008", GH_API, "<html>oops"),
        (
            "https://boards.greenhouse.io/quillfern/jobs/4012345008",
            GH_API,
            {"title": "X", "location": "Austin"},
        ),
        (f"https://jobs.lever.co/brightmoss/{LEVER_ID}", LEVER_API, [LEVER_JOB]),
        (
            f"https://jobs.lever.co/brightmoss/{LEVER_ID}",
            LEVER_API,
            {**LEVER_JOB, "categories": ["Remote"]},
        ),
        (
            f"https://jobs.lever.co/brightmoss/{LEVER_ID}",
            LEVER_API,
            {**LEVER_JOB, "lists": ["not a dict"]},
        ),
        (
            f"https://jobs.lever.co/brightmoss/{LEVER_ID}",
            LEVER_API,
            {**LEVER_JOB, "descriptionPlain": {"oops": 1}},
        ),
        (f"https://jobs.lever.co/brightmoss/{LEVER_ID}", LEVER_API, {**LEVER_JOB, "lists": 3}),
        (ASHBY_URL, ASHBY_API, [ASHBY_JOB]),
        (ASHBY_URL, ASHBY_API, {"jobs": {"id": ASHBY_ID}}),
        (ASHBY_URL, ASHBY_API, {"jobs": 7}),
        (ASHBY_URL, ASHBY_API, {"jobs": ["not a dict"]}),
        (f"{WD_HOST}/Careers{WD_PATH}", WD_API, [WD_JOB]),
        (f"{WD_HOST}/Careers{WD_PATH}", WD_API, {"jobPostingInfo": ["odd"]}),
    ],
)
def test_surprising_ats_shape_falls_back_to_page(url: str, api: str, body: object) -> None:
    get = FakeGet({api: (200, body), url: (200, LIVE_PAGE)})
    posting = fetch_posting(url, get)
    assert posting.status == OK, posting.error
    assert posting.ats == "web"
    assert get.urls == [api, url]


def test_short_ats_text_falls_back_to_page() -> None:
    url = "https://boards.greenhouse.io/quillfern/jobs/4012345008"
    short = {**GH_JOB, "content": "&lt;p&gt;TBD&lt;/p&gt;"}
    get = FakeGet({GH_API: (200, short), url: (200, LIVE_PAGE)})
    posting = fetch_posting(url, get)
    assert posting.ats == "web"
    assert posting.status == OK


def test_closed_wording_inside_a_live_ats_posting_is_left_to_the_reader() -> None:
    """The ATS said the job exists. What its description says is for the model to judge,
    so a sentence such as "sponsorship is no longer available" cannot hide a live job."""
    body = "&lt;p&gt;This position has been filled.&lt;/p&gt;" + "x" * 500
    get = FakeGet({GH_API: (200, {**GH_JOB, "content": body})})
    posting = fetch_posting("https://boards.greenhouse.io/quillfern/jobs/4012345008", get)
    assert posting.status == OK


def test_greenhouse_embed_link_and_workday_apply_link_are_recognised() -> None:
    get = FakeGet({GH_API: (200, GH_JOB)})
    embed = "https://boards.greenhouse.io/embed/job_app?for=quillfern&token=4012345008"
    assert fetch_posting(embed, get).ats == "greenhouse"


def test_tracking_numbers_are_not_job_ids() -> None:
    from jobtool.enrich.posting import _has_job_id

    assert _has_job_id("https://x.test/jobs/view?src=8812345", "https://x.test/jobs/view")
    assert not _has_job_id("https://x.test/job?jobId=8812345", "https://x.test/careers")
    assert _has_job_id("https://x.test/job?jobId=8812345", "https://x.test/job/8812345")


# ----- any other web page ---------------------------------------------------------

WEB_URL = "https://careers.quillfern.example/jobs/882211/software-engineering-intern"


def _json_ld(data: object) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps(data)
        + "</script></head><body><nav>menu</nav><p>Cookie banner text only.</p></body></html>"
    )


JOB_LD = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Software Engineering Intern",
    "description": f"&lt;p&gt;{DUTIES}&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
    "datePosted": "2026-10-01",
    "hiringOrganization": {"@type": "Organization", "name": "Quillfern Logistics"},
}


@pytest.mark.parametrize(
    "data",
    [
        JOB_LD,
        [{"@context": "https://schema.org", "@type": "Organization", "name": "Q"}, JOB_LD],
        {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, JOB_LD]},
    ],
)
def test_web_prefers_json_ld(data: object) -> None:
    get = FakeGet({WEB_URL: (200, _json_ld(data))})
    posting = fetch_posting(WEB_URL, get)
    assert posting.status == OK
    assert posting.ats == "web"
    assert posting.text.startswith("Software Engineering Intern\n\n")
    assert "scheduling engine" in posting.text
    assert "- Python" in posting.text
    assert "Cookie banner" not in posting.text


def test_web_bad_json_ld_falls_back_to_html() -> None:
    page = LIVE_PAGE.replace(
        "</head>",
        '<script type="application/ld+json">{not json</script>'
        '<script type="application/ld+json">"just a string"</script>'
        '<script type="application/ld+json">null</script></head>',
    )
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, page)}))
    assert posting.status == OK, posting.error
    assert "scheduling engine" in posting.text


def test_web_plain_html_is_reduced_to_text() -> None:
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, LIVE_PAGE)}))
    assert posting.status == OK
    text = posting.text
    assert text.startswith("Software Engineering Intern")
    assert "- Python or another typed language\n" in text
    assert "- SQL databases & data modelling" in text
    for junk in ("NAVIGATION-LINK", "FOOTER-TEXT", "dataLayer", "font-family", "<", "| Quillfern"):
        assert junk not in text
    assert "\n\n\n" not in text


def test_html_to_text_entities_and_nbsp() -> None:
    text = html_to_text("<p>Salary&nbsp;&euro;40k &ndash; R&amp;D &#38; more</p><br><li>one</li>")
    assert text == "Salary €40k – R&D & more\n\n- one"


@pytest.mark.parametrize("status", [404, 410])
def test_web_gone_is_dead(status: int) -> None:
    assert fetch_posting(WEB_URL, FakeGet({WEB_URL: (status, LIVE_PAGE)})).status == DEAD


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 400, 405])
def test_web_refused_is_error(status: int) -> None:
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (status, LIVE_PAGE)}))
    assert posting.status == ERROR
    assert str(status) in posting.error


def test_web_too_little_text_is_unreadable() -> None:
    page = "<html><body><div id='root'></div><script>render()</script>Loading...</body></html>"
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, page)}))
    assert posting.status == UNREADABLE
    assert posting.error


@pytest.mark.parametrize(
    "phrase",
    [
        "This job is no longer accepting applications.",
        "Sorry, this position has been filled.",
        "This job has expired.",
        "Job not found",
    ],
)
def test_web_closed_phrase_is_dead(phrase: str) -> None:
    page = LIVE_PAGE.replace("<h1>", f"<p>{phrase}</p><h1>")
    assert fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, page)})).status == DEAD


def test_web_closed_phrase_far_down_the_page_is_ignored() -> None:
    page = LIVE_PAGE.replace("</main>", "<p>" + "filler " * 600 + "Job not found</p></main>")
    assert fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, page)})).status == OK


def test_web_redirect_away_from_job_id_is_dead() -> None:
    get = FakeGet({WEB_URL: (200, LIVE_PAGE, "https://careers.quillfern.example/jobs")})
    posting = fetch_posting(WEB_URL, get)
    assert posting.status == DEAD
    assert "redirected" in posting.error


def test_web_redirect_away_from_query_job_id_is_dead() -> None:
    url = "https://www.quillfern.example/careers?gh_jid=4012345008"
    get = FakeGet({url: (200, LIVE_PAGE, "https://www.quillfern.example/careers")})
    assert fetch_posting(url, get).status == DEAD


def test_web_redirect_keeping_job_id_is_not_dead() -> None:
    final = "https://careers.quillfern.example/en/jobs/882211-software-engineering-intern?ref=x"
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, LIVE_PAGE, final)}))
    assert posting.status == OK


def test_web_url_without_numeric_id_ignores_redirect() -> None:
    url = "https://careers.quillfern.example/jobs/software-engineering-intern"
    get = FakeGet({url: (200, LIVE_PAGE, "https://careers.quillfern.example/jobs/swe-intern")})
    assert fetch_posting(url, get).status == OK


# ----- never raises ---------------------------------------------------------------


def test_connect_error_is_error() -> None:
    def get(url: str, headers: dict[str, str]) -> Response:
        raise httpx.ConnectError("name resolution failed")

    posting = fetch_posting("https://boards.greenhouse.io/quillfern/jobs/4012345008", get)
    assert posting.status == ERROR
    assert posting.error == "could not reach the site: ConnectError"


def test_unexpected_exception_is_error() -> None:
    def get(url: str, headers: dict[str, str]) -> Response:
        raise RuntimeError("boom")

    posting = fetch_posting(WEB_URL, get)
    assert posting.status == ERROR
    assert posting.error == "RuntimeError: boom"


# ----- hashing and clipping -------------------------------------------------------


def test_content_hash_is_stable_and_distinct() -> None:
    a = Posting(OK, text="same text")
    assert a.content_hash == Posting(OK, text="same text").content_hash
    assert a.content_hash != Posting(OK, text="other text").content_hash
    assert len(a.content_hash) == 24


def test_text_is_clipped() -> None:
    page = "<html><body><p>" + "word " * (MAX_TEXT // 2) + "</p></body></html>"
    posting = fetch_posting(WEB_URL, FakeGet({WEB_URL: (200, page)}))
    assert posting.status == OK
    assert len(posting.text) == MAX_TEXT

    long_job = {**GH_JOB, "content": "&lt;p&gt;" + "word " * MAX_TEXT + "&lt;/p&gt;"}
    get = FakeGet({GH_API: (200, long_job)})
    posting = fetch_posting("https://boards.greenhouse.io/quillfern/jobs/4012345008", get)
    assert posting.status == OK
    assert len(posting.text) == MAX_TEXT
