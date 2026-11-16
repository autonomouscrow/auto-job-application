"""Fetch the text of a job posting and decide whether it is still live.

Listings only carry a link, so the description has to come from the employer's
site. Greenhouse, Lever, Ashby, and Workday publish postings as JSON, which is
read directly. Anything else is fetched as a web page and reduced to text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

OK, DEAD, UNREADABLE, ERROR = "ok", "dead", "unreadable", "error"

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}
# A page with less text than this was probably drawn by JavaScript we did not run.
MIN_TEXT = 400
MAX_TEXT = 30_000

# Wording an employer's site uses for a posting that is gone. Kept specific on purpose:
# a wrong match hides a live job, so loose phrases such as "no longer available" (which a
# live posting can say about something else) are not on the list.
CLOSED_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "this position is no longer available",
    "this posting is no longer available",
    "this role is no longer available",
    "job is no longer open",
    "position is no longer open",
    "position has been filled",
    "job has been filled",
    "this job has expired",
    "job posting has expired",
    "this posting has closed",
    "this position is closed",
    "this job is closed",
    "job not found",
    "the job you are looking for is no longer",
    "the job you are looking for could not be found",
    "the job you are looking for doesn't exist",
    "the page you are looking for doesn't exist",
)


@dataclass
class Posting:
    status: str
    ats: str = "web"
    text: str = ""
    title: str = ""
    error: str = ""

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:24]


@dataclass
class Response:
    status: int
    text: str
    url: str

    def json(self):
        return json.loads(self.text)


Getter = Callable[[str, dict[str, str]], Response]


def http_get(url: str, headers: dict[str, str]) -> Response:
    response = httpx.get(
        url, headers={**HEADERS, **headers}, timeout=TIMEOUT, follow_redirects=True
    )
    return Response(response.status_code, response.text, str(response.url))


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "button", "select"}
    BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td",
        "dd", "dt", "section", "article", "header",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
    except Exception:  # noqa: BLE001 - malformed pages should degrade, not crash
        return ""
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clip(text: str) -> str:
    return text[:MAX_TEXT]


def looks_closed(text: str) -> bool:
    head = text[:3000].lower()
    return any(phrase in head for phrase in CLOSED_PHRASES)


# ----- applicant tracking systems -------------------------------------------

_GREENHOUSE = re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/]+)/jobs/(\d+)")
_GREENHOUSE_EMBED = re.compile(
    r"greenhouse\.io/embed/job_app\?(?:.*&)?for=([^&]+).*[?&]token=(\d+)"
)
_LEVER = re.compile(r"jobs(?:\.eu)?\.lever\.co/([^/]+)/([0-9a-f-]{36})")
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})")
_WORKDAY = re.compile(
    r"https?://(([^./]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+)$"
)


def _greenhouse(url: str, get: Getter) -> Posting | None:
    match = _GREENHOUSE.search(url) or _GREENHOUSE_EMBED.search(url)
    if not match:
        return None
    board, job_id = match.groups()
    region = ".eu" if ".eu.greenhouse.io" in url else ""
    response = get(
        f"https://boards-api{region}.greenhouse.io/v1/boards/{board}/jobs/{job_id}", {}
    )
    if response.status == 404:
        return Posting(DEAD, "greenhouse")
    if response.status != 200:
        return None
    data = response.json()
    body = html_to_text(html.unescape(data.get("content") or ""))
    location = (data.get("location") or {}).get("name") or ""
    text = f"{data.get('title', '')}\nLocation: {location}\n\n{body}"
    return Posting(OK, "greenhouse", _clip(text), data.get("title") or "")


def _lever(url: str, get: Getter) -> Posting | None:
    match = _LEVER.search(url)
    if not match:
        return None
    site, job_id = match.groups()
    host = "api.eu.lever.co" if "jobs.eu.lever.co" in url else "api.lever.co"
    response = get(f"https://{host}/v0/postings/{site}/{job_id}", {})
    if response.status == 404:
        return Posting(DEAD, "lever")
    if response.status != 200:
        return None
    data = response.json()
    categories = data.get("categories") or {}
    parts = [
        data.get("text") or "",
        f"Location: {categories.get('location', '')}",
        f"Commitment: {categories.get('commitment', '')}",
        f"Workplace: {data.get('workplaceType', '')}",
        "",
        data.get("descriptionPlain") or html_to_text(data.get("description") or ""),
    ]
    for item in data.get("lists") or []:
        parts += ["", item.get("text") or "", html_to_text(item.get("content") or "")]
    parts += ["", data.get("additionalPlain") or html_to_text(data.get("additional") or "")]
    return Posting(OK, "lever", _clip("\n".join(parts)), data.get("text") or "")


def _ashby(url: str, get: Getter) -> Posting | None:
    match = _ASHBY.search(url)
    if not match:
        return None
    org, job_id = match.groups()
    response = get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", {})
    if response.status == 404:
        return Posting(DEAD, "ashby")
    if response.status != 200:
        return None
    jobs = response.json().get("jobs")
    if not isinstance(jobs, list):
        return None  # not a job board answer: it says nothing about the job
    for job in jobs:
        if job.get("id") == job_id:
            body = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
            text = (
                f"{job.get('title', '')}\nLocation: {job.get('location', '')}\n"
                f"Workplace: {job.get('workplaceType', '')}\n\n{body}"
            )
            return Posting(OK, "ashby", _clip(text), job.get("title") or "")
    # The board loaded and the job is not on it any more.
    return Posting(DEAD, "ashby")


def _workday(url: str, get: Getter) -> Posting | None:
    # Some lists link to the apply form of a job; the posting is the same path without it.
    match = _WORKDAY.match(re.sub(r"/apply(/[A-Za-z]+)?/?$", "", url.split("?", 1)[0]))
    if not match:
        return None
    host, tenant, site, path = match.groups()
    response = get(
        f"https://{host}/wday/cxs/{tenant}/{site}{path}", {"Accept": "application/json"}
    )
    if response.status in (404, 410):
        return Posting(DEAD, "workday")
    if response.status != 200:
        return None
    info = response.json().get("jobPostingInfo") or {}
    if not info:
        return None
    body = html_to_text(info.get("jobDescription") or "")
    text = (
        f"{info.get('title', '')}\nLocation: {info.get('location', '')}\n"
        f"Time type: {info.get('timeType', '')}\nStart date: {info.get('startDate', '')}\n\n{body}"
    )
    return Posting(OK, "workday", _clip(text), info.get("title") or "")


# ----- any other web page ----------------------------------------------------

_JSON_LD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)


def _job_posting_from_json_ld(markup: str) -> str:
    for block in _JSON_LD.findall(markup):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, (list, dict)):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items if isinstance(items, list) else [items]:
            kind = item.get("@type") if isinstance(item, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                body = html_to_text(html.unescape(str(item.get("description") or "")))
                if body:
                    return f"{item.get('title', '')}\n\n{body}"
    return ""


_JOB_ID_PARAMS = {"gh_jid", "jid", "id", "jobid", "job_id", "job", "req", "reqid", "pid", "token"}


def _has_job_id(original: str, final: str) -> bool:
    """False when we were redirected away from the specific job to a general page."""
    ids = re.findall(r"\d{5,}", urlsplit(original).path)
    # Only parameters that name a job count; a tracking number may be dropped harmlessly.
    ids += [value for name, values in parse_qs(urlsplit(original).query).items()
            if name.lower() in _JOB_ID_PARAMS for value in values
            if re.fullmatch(r"\d{5,}", value)]  # fmt: skip
    if not ids:
        return True
    return any(job_id in final for job_id in ids)


def _web(url: str, get: Getter) -> Posting:
    response = get(url, {})
    if response.status in (404, 410):
        return Posting(DEAD)
    if response.status >= 400:
        return Posting(ERROR, error=f"the site answered HTTP {response.status}")
    if not _has_job_id(url, response.url):
        return Posting(DEAD, error="redirected away from the job to a general page")
    text = _job_posting_from_json_ld(response.text) or html_to_text(response.text)
    if looks_closed(text):
        return Posting(DEAD, text=_clip(text))
    if len(text) < MIN_TEXT:
        return Posting(
            UNREADABLE, error="the page has too little text to read (it may need a browser)"
        )
    return Posting(OK, text=_clip(text))


def fetch_posting(url: str, get: Getter = http_get) -> Posting:
    """Never raises: every failure comes back as a status with a reason."""
    try:
        for reader in (_greenhouse, _lever, _ashby, _workday):
            try:
                posting = reader(url, get)
            except (ValueError, KeyError, AttributeError, TypeError):
                posting = None  # unexpected shape: fall back to reading the page
            if posting is not None:
                if posting.status == OK and len(posting.text) < MIN_TEXT:
                    break
                return posting
        return _web(url, get)
    except httpx.HTTPError as exc:
        return Posting(ERROR, error=f"could not reach the site: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - one odd page must not stop a batch
        return Posting(ERROR, error=f"{type(exc).__name__}: {exc}"[:200])
