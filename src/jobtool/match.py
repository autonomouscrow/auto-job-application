"""Role type, filters, flags, and the match score.

Before a posting has been read, everything works from the title, the
location, and what the source says, and the score is marked provisional. Once
the posting has been read (see :mod:`jobtool.enrich`), the same function adds
the filters that need the description and replaces the score with the full
one. One function decides both, so a sync and a read always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from jobtool.config import Config
from jobtool.enrich.requirements import Extracted, SkillNeed
from jobtool.geo import in_target_regions
from jobtool.normalize import fold
from jobtool.profile.models import Profile

QUANT, SWE, ADJACENT, OFF_TARGET = "quant", "swe", "adjacent", "off-target"

_QUANT = re.compile(
    r"\b(quant\w*|trader|trading|market mak\w*|algo(rithmic)? trad\w*|strateg\w+ (and )?"
    r"(trad|research)\w*)\b"
)
_SWE = re.compile(
    r"\b(software|swe|sde|sdet|developer|programmer|backend|back end|frontend|front end|"
    r"full stack|fullstack|devops|sre|site reliability|firmware|embedded|"
    r"(platform|infrastructure|systems?|cloud|mobile|ios|android|web|tools?|automation|"
    r"test|qa|compiler|kernel|low latency|c\+\+|python|java|golang|rust|application|"
    r"applications|network|database|game|graphics|simulation|distributed|api|ui|ux) "
    r"(engineer\w*|develop\w*))\b"
)
_ADJACENT = re.compile(
    r"\b(machine learning|ml|ai|artificial intelligence|deep learning|nlp|computer vision|"
    r"data (scien\w+|engineer\w*|analy\w+)|analytics|research\w*|scientist|security|cyber\w*|"
    r"hardware|fpga|asic|robotics|electrical|technical program|solutions engineer|"
    r"engineer\w*|technology|tech|it|computing|computer science|aiml|genai)\b"
)
_DEGREE_WORDS = {
    "bachelor": re.compile(r"\b(bs|ba|bsc|basc|bachelor\w*|undergrad\w*)\b"),
    "master": re.compile(r"\b(ms|msc|meng|master\w*|mba|graduate)\b"),
    "phd": re.compile(r"\b(phd|doctoral|doctorate|postdoc\w*)\b"),
}
# What a source's own category means when the title alone is not conclusive.
_HINTS = {
    "quant": QUANT, "qr": QUANT, "qt": QUANT, "qd": QUANT, "qr fellowship": QUANT,
    "swe": SWE, "software": SWE, "software engineering": SWE, "devops/sre": SWE,
    "hw": ADJACENT, "fpga": ADJACENT, "ml": ADJACENT, "hardware": ADJACENT,
    "ai/ml/data": None, "product": None,
}  # fmt: skip

_SEASON = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*(20\d\d)\b", re.IGNORECASE)
_MONTHS = re.compile(r"\b(\d{1,2})\s*[- ]?\s*months?\b", re.IGNORECASE)


def role_type(title: str, category: str | None = None, default: str | None = None) -> str:
    folded = fold(title).replace("c++", "c++ ")
    if _QUANT.search(folded):
        return QUANT
    hint = _HINTS.get((category or "").strip().lower())
    if hint == QUANT:
        return QUANT
    if _SWE.search(folded):
        return SWE
    if hint in (SWE, ADJACENT):
        return hint
    if _ADJACENT.search(folded):
        return ADJACENT
    return default or OFF_TARGET


@dataclass
class Assessment:
    role_type: str
    score: int
    detail: dict
    flags: list[str] = field(default_factory=list)
    filter_reason: str | None = None


def _term_problem(title: str, terms: list[str], config: Config) -> str | None:
    wanted = {label.lower() for label in config.targets.term.labels()}
    if not wanted:
        return None
    named = {term.lower() for term in terms if term and term.upper() != "N/A"}
    if named and not named & wanted:
        return "term: listed for " + ", ".join(sorted(terms))
    in_title = {f"{season.lower().replace('autumn', 'fall')} {year}"
                for season, year in _SEASON.findall(title)}  # fmt: skip
    if in_title and not in_title & wanted and not named & wanted:
        return "term: title says " + ", ".join(sorted(in_title))
    limit = config.targets.term.max_duration_months
    if limit:
        months = [int(value) for value in _MONTHS.findall(title)]
        if months and min(months) > limit:
            return f"duration: {min(months)} months"
    return None


def _degree_problem(title: str, degrees: list[str], wanted: str) -> str | None:
    """A reason when the posting is only for a degree level other than the applicant's."""
    if wanted not in _DEGREE_WORDS:
        return None
    listed = {name for name in _DEGREE_WORDS if any(name in d.lower() for d in degrees)}
    if listed and wanted not in listed:
        return "degree: for " + ", ".join(degrees) + " students"
    folded = fold(title)
    named = {name for name, pattern in _DEGREE_WORDS.items() if pattern.search(folded)}
    if named and wanted not in named:
        return "degree: title asks for " + " or ".join(sorted(named)) + " students"
    return None


def _skill_hits(title: str, profile: Profile | None) -> list[str]:
    if profile is None:
        return []
    folded = f" {fold(title)} "
    hits = []
    for skill in profile.skills:
        names = [skill.name, *skill.aliases]
        if any(len(fold(name)) >= 2 and f" {fold(name)} " in folded for name in names):
            hits.append(skill.name)
    return hits


def assess(
    *,
    title: str,
    locations: list[str],
    terms: list[str],
    sponsorship: str | None,
    degrees: list[str] | None = None,
    category: str | None,
    default_role: str | None,
    date_posted: date | None,
    source_marks_closed: bool,
    config: Config,
    profile: Profile | None,
    today: date,
    requirements: Extracted | None = None,
    posting_status: str | None = None,
) -> Assessment:
    role = role_type(title, category, default_role)
    flags: list[str] = []
    reason: str | None = None
    read = requirements if requirements is not None and requirements.is_job_posting else None

    if role == OFF_TARGET:
        reason = "role: not a software, quant, or adjacent role"
    if reason is None and (posting_status == "dead" or (read is not None and read.closed)):
        reason = "closed: the employer's page says this posting is no longer open"
    if reason is None:
        reason = _term_problem(title, terms, config)
    if reason is None:
        reason = _degree_problem(title, degrees or [], config.applicant.degree.lower())

    in_region = in_target_regions(locations, config.targets.regions)
    if in_region is None and read is not None and read.locations:
        # The source was vague about where the job is; the posting may not be.
        in_region = in_target_regions(read.locations, config.targets.regions)
        if reason is None and in_region is False:
            reason = "region: " + ", ".join(read.locations)
    elif reason is None and in_region is False:
        reason = "region: " + ", ".join(locations)
    if in_region is None and config.targets.regions:
        flags.append("location unclear")

    sponsor = (sponsorship or "").lower()
    citizenships = {code.upper() for code in profile.identity.citizenships} if profile else set()
    if "citizenship" in sponsor:
        flags.append("US citizenship required")
        if reason is None and "US" not in citizenships:
            reason = "citizenship: US citizenship required"
    elif "does not offer" in sponsor or "no sponsorship" in sponsor:
        flags.append("no sponsorship offered")

    age_limit = config.ingest.max_age_days
    if (
        reason is None
        and not source_marks_closed
        and date_posted is not None
        and age_limit
        and date_posted < today - timedelta(days=age_limit)
        and posting_status != "ok"  # a posting we just read as open is not stale
    ):
        reason = f"stale: posted {date_posted.isoformat()}, and this source never marks closures"

    if read is not None:
        return _full(role, read, flags, reason, config, citizenships)

    # Provisional score from the title alone. It is sized as a guess at the full score
    # (which rarely passes 70), so that read and unread jobs sort sensibly together and
    # reading a job does not push it behind every job that has not been read yet.
    targets = [role_name.lower() for role_name in config.targets.roles]
    role_points = 40 if role in targets else 20 if role == ADJACENT else 0
    hits = _skill_hits(title, profile)
    skill_points = min(15, 8 * len(hits))
    term_points = 7 if {t.lower() for t in terms} & {
        label.lower() for label in config.targets.term.labels()
    } else 4
    region_points = 7 if in_region else 4 if in_region is None else 0
    score = min(100, role_points + skill_points + term_points + region_points)
    detail = {
        "provisional": True,
        "components": {
            "role type": role_points,
            "skills named in title": skill_points,
            "term": term_points,
            "region": region_points,
        },
        "matched_skills": hits,
    }
    if posting_status in ("unreadable", "error") or (
        requirements is not None and not requirements.is_job_posting
    ):
        detail["unread"] = "The posting could not be read automatically, so this is from the title."
    return Assessment(role, score, detail, flags, reason)


# ----- with the posting read --------------------------------------------------

# What each part of the full score is worth. They add up to 100.
WEIGHTS = {
    "must-have skills": 40,
    "nice-to-have skills": 15,
    "experience relevance": 20,
    "role type": 10,
    "eligibility": 10,
    "work mode": 5,
}
# Share of the points given when a posting lists no skills of that kind at all.
_NOTHING_LISTED = 0.7


def _parse_day(value: str | None, *, end_of_month: bool) -> date | None:
    """Read 'YYYY-MM-DD' or 'YYYY-MM'. A bare month counts as its last or first day."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", value.strip())
    if not match:
        return None
    year, month, day = int(match[1]), int(match[2]), match[3]
    try:
        if day:
            return date(year, month, int(day))
        if not end_of_month:
            return date(year, month, 1)
        following = date(year + (month == 12), month % 12 + 1, 1)
        return following - timedelta(days=1)
    except ValueError:
        return None


def _month_number(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", (value or "").strip())
    return int(match[1]) * 12 + int(match[2]) if match else None


# A start this long before the wanted date is still shown, with a flag.
_EARLY_START_GRACE = timedelta(days=45)


def _term_from_posting(read: Extracted, config: Config, flags: list[str]) -> str | None:
    term = config.targets.term
    start = _parse_day(read.start_date, end_of_month=True)
    start_first = _parse_day(read.start_date, end_of_month=False)
    end = _parse_day(read.end_date, end_of_month=False)
    if term.latest_end and start_first and start_first > term.latest_end:
        return f"term: starts {read.start_date}"
    if term.earliest_start and start:
        if start < term.earliest_start - _EARLY_START_GRACE:
            return f"term: starts {read.start_date}"
        if start < term.earliest_start:
            flags.append(f"starts {read.start_date}")
    if term.latest_end and end and end > term.latest_end + timedelta(days=7):
        flags.append(f"runs until {read.end_date}")
    limit = term.max_duration_months
    if limit and read.duration_months and read.duration_months > limit:
        return f"duration: {read.duration_months} months"
    wanted = {label.lower() for label in term.labels()}
    season = " ".join((read.season or "").lower().replace("autumn", "fall").split())
    dated_in_window = start is not None  # the dates above already passed
    if wanted and _SEASON.fullmatch(season) and season not in wanted and not dated_in_window:
        return f"term: posting says {read.season}"
    return None


def _language_problem(read: Extracted, config: Config) -> str | None:
    wanted = config.targets.working_language.strip().lower()
    if not wanted:
        return None
    other = [language for language in read.required_languages if language != wanted]
    if other:
        return "language: requires " + ", ".join(other)
    if read.posting_language and read.posting_language != wanted:
        return f"language: the posting is written in {read.posting_language}"
    return None


def _eligibility(
    read: Extracted, config: Config, citizenships: set[str], flags: list[str]
) -> tuple[int, str | None]:
    """Points out of 10, plus a filter reason when the posting rules the applicant out."""
    points = WEIGHTS["eligibility"]
    reason = None
    wanted_degree = config.applicant.degree.lower()
    if read.degree_levels and wanted_degree not in read.degree_levels:
        reason = "degree: for " + ", ".join(read.degree_levels) + " students"

    kind = read.work_authorization
    if kind == "citizenship_required":
        accepted = set(read.citizenship_countries)
        if accepted and not accepted & citizenships:
            reason = reason or "citizenship: requires citizenship of " + ", ".join(sorted(accepted))
        elif not accepted:
            flags.append("citizenship required")
            points = 0
    elif kind == "security_clearance":
        flags.append("security clearance required")
        points = 0
    elif kind == "existing_right_to_work":
        flags.append("must already have the right to work there")
        points = min(points, 4)
    elif kind == "no_sponsorship" and "no sponsorship offered" not in flags:
        flags.append("no sponsorship offered")

    graduation = _month_number(config.applicant.graduation)
    earliest = _month_number(read.graduation_earliest)
    latest = _month_number(read.graduation_latest)
    if graduation and ((earliest and graduation < earliest) or (latest and graduation > latest)):
        window = " to ".join(v for v in (read.graduation_earliest, read.graduation_latest) if v)
        flags.append(f"wants graduation {window}")
        points = min(points, 4)
    return points, reason


def _share(needs: list[SkillNeed]) -> float:
    if not needs:
        return _NOTHING_LISTED
    return sum(1 for need in needs if need.profile_skill) / len(needs)


def _full(
    title_role: str,
    read: Extracted,
    flags: list[str],
    reason: str | None,
    config: Config,
    citizenships: set[str],
) -> Assessment:
    # A clear title wins. A vague one ("Engineering Intern") takes the posting's word,
    # but the posting can only move a job toward the target roles, never out of them.
    role = title_role
    if title_role == ADJACENT and read.role_type in (QUANT, SWE):
        role = read.role_type

    if reason is None:
        reason = _term_from_posting(read, config, flags)
    if reason is None:
        reason = _language_problem(read, config)
    eligibility, blocked = _eligibility(read, config, citizenships, flags)
    reason = reason or blocked

    targets = [role_name.lower() for role_name in config.targets.roles]
    preference = [mode.lower() for mode in config.targets.work_mode_preference]
    if read.work_mode in preference:
        mode_points = max(0, WEIGHTS["work mode"] - preference.index(read.work_mode))
    else:
        unknown = read.work_mode == "unknown" or not preference
        mode_points = WEIGHTS["work mode"] - 1 if unknown else 0
    components = {
        "must-have skills": round(WEIGHTS["must-have skills"] * _share(read.must_have_skills)),
        "nice-to-have skills": round(
            WEIGHTS["nice-to-have skills"] * _share(read.nice_to_have_skills)
        ),
        "experience relevance": round(
            WEIGHTS["experience relevance"] * read.experience_relevance / 10
        ),
        "role type": 10 if role in targets else 5 if role == ADJACENT else 0,
        "eligibility": eligibility,
        "work mode": mode_points,
    }
    if read.deadline:
        flags.append(f"apply by {read.deadline}")
    detail = {
        "provisional": False,
        "components": components,
        "out_of": WEIGHTS,
        "matched_must": [n.name for n in read.must_have_skills if n.profile_skill],
        "missing_must": [n.name for n in read.must_have_skills if not n.profile_skill],
        "matched_nice": [n.name for n in read.nice_to_have_skills if n.profile_skill],
        "missing_nice": [n.name for n in read.nice_to_have_skills if not n.profile_skill],
    }
    return Assessment(role, min(100, sum(components.values())), detail, flags, reason)
