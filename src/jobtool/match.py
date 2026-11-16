"""Role type, filters, flags, and the provisional (title-only) match score.

Phase 1 has no job descriptions yet, so everything here works from the title,
the location, and what the source says. The full score arrives with Phase 2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from jobtool.config import Config
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
) -> Assessment:
    role = role_type(title, category, default_role)
    flags: list[str] = []
    reason: str | None = None

    if role == OFF_TARGET:
        reason = "role: not a software, quant, or adjacent role"
    if reason is None:
        reason = _term_problem(title, terms, config)
    if reason is None:
        reason = _degree_problem(title, degrees or [], config.applicant.degree.lower())

    in_region = in_target_regions(locations, config.targets.regions)
    if reason is None and in_region is False:
        reason = "region: " + ", ".join(locations)
    if in_region is None and config.targets.regions:
        flags.append("location unclear")

    sponsor = (sponsorship or "").lower()
    citizenships = set(profile.identity.citizenships) if profile else set()
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
    ):
        reason = f"stale: posted {date_posted.isoformat()}, and this source never marks closures"

    # Provisional score from the title alone.
    targets = [role_name.lower() for role_name in config.targets.roles]
    role_points = 55 if role in targets else 30 if role == ADJACENT else 0
    hits = _skill_hits(title, profile)
    skill_points = min(25, 12 * len(hits))
    term_points = 10 if {t.lower() for t in terms} & {
        label.lower() for label in config.targets.term.labels()
    } else 5
    region_points = 10 if in_region else 5 if in_region is None else 0
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
    return Assessment(role, score, detail, flags, reason)
