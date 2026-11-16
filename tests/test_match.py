from __future__ import annotations

from datetime import date

import pytest

from jobtool.config import Config
from jobtool.geo import in_target_regions, region_of
from jobtool.match import assess, role_type
from jobtool.normalize import canonical_url, listing_key, norm_company, norm_title

TODAY = date(2026, 11, 16)
CONFIG = Config.model_validate(
    {
        "targets": {
            "roles": ["quant", "swe"],
            "term": {"earliest_start": "2027-05-15", "max_duration_months": 4},
            "regions": ["north-america", "europe"],
        }
    }
)


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        ("Quantitative Researcher Intern", None, "quant"),
        ("Trading Intern", None, "quant"),
        ("Software Engineer Intern", None, "swe"),
        ("2027 Software Dev Engineer Intern - Finland", None, "swe"),
        ("Backend Developer Co-op", None, "swe"),
        ("Embedded Firmware Intern", None, "swe"),
        ("Platform Engineer Intern", None, "swe"),
        ("Machine Learning Intern - Computer Vision", None, "adjacent"),
        ("Data Science Intern", None, "adjacent"),
        ("Hardware Engineer Intern", "HW", "adjacent"),
        ("Commercial Intern", "AI/ML/Data", "off-target"),
        ("Marketing Intern", None, "off-target"),
        ("Summer Intern", "Software", "swe"),
        ("Summer Intern", "Quant", "quant"),
        ("Data Science Intern", "Quant", "quant"),
    ],
)
def test_role_type(title: str, category: str | None, expected: str) -> None:
    assert role_type(title, category) == expected


def test_default_role_applies_only_to_unclear_titles() -> None:
    assert role_type("Summer Analyst", None, "quant") == "quant"
    assert role_type("Software Engineer Intern", None, "quant") == "swe"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("NYC", "north-america"),
        ("San Jose, CA", "north-america"),
        ("Toronto, ON, Canada", "north-america"),
        ("Remote in USA", "north-america"),
        ("Texas", "north-america"),
        ("London, UK", "europe"),
        ("Kerkrade, Netherlands", "europe"),
        ("Genève, Switzerland", "europe"),
        ("Singapore", "other"),
        ("Bangalore, India", "other"),
        ("Remote", None),
        ("Somewhere Unknown", None),
    ],
)
def test_region_of(location: str, expected: str | None) -> None:
    assert region_of(location) == expected


def test_in_target_regions() -> None:
    targets = ["north-america", "europe"]
    assert in_target_regions(["Singapore", "London"], targets) is True
    assert in_target_regions(["Singapore"], targets) is False
    assert in_target_regions(["Remote"], targets) is None
    assert in_target_regions([], targets) is None


def test_canonical_url_strips_tracking() -> None:
    a = "https://www.Example.com/jobs/1/?utm_source=Simplify&ref=Simplify&gh_jid=77#apply"
    b = "http://example.com/jobs/1?gh_jid=77"
    assert canonical_url(a) == canonical_url(b) == "https://example.com/jobs/1?gh_jid=77"
    assert listing_key(a) == listing_key(b)
    assert listing_key(a) != listing_key("https://example.com/jobs/2?gh_jid=77")


def test_normalised_names() -> None:
    assert norm_company("Acme Technologies, Inc.") == norm_company("ACME")
    assert norm_title("Software Engineer Intern - Summer 2027") == norm_title(
        "Software Engineer Internship"
    )


def check(**overrides):
    values = dict(
        title="Software Engineer Intern",
        locations=["NYC"],
        terms=["Summer 2027"],
        sponsorship=None,
        category=None,
        default_role=None,
        date_posted=TODAY,
        source_marks_closed=True,
        config=CONFIG,
        profile=None,
        today=TODAY,
    )
    values.update(overrides)
    return assess(**values)


def test_target_role_passes_with_provisional_score() -> None:
    result = check()
    assert result.filter_reason is None
    assert result.role_type == "swe"
    assert result.detail["provisional"] is True
    assert result.score == sum(result.detail["components"].values())


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"title": "Marketing Intern"}, "role"),
        ({"terms": ["Fall 2026"]}, "term"),
        ({"terms": [], "title": "Software Intern - Winter 2027"}, "term"),
        ({"terms": [], "title": "Software Intern (13 Months)"}, "duration"),
        ({"locations": ["Singapore"]}, "region"),
        ({"sponsorship": "U.S. Citizenship is Required"}, "citizenship"),
        (
            {"source_marks_closed": False, "date_posted": date(2026, 8, 1)},
            "stale",
        ),
    ],
)
def test_filters(overrides: dict, reason: str) -> None:
    result = check(**overrides)
    assert result.filter_reason is not None
    assert result.filter_reason.startswith(reason)


def test_things_that_do_not_filter() -> None:
    assert check(terms=[]).filter_reason is None
    assert check(terms=["Summer 2027", "Fall 2027"]).filter_reason is None
    assert check(locations=["Remote"]).filter_reason is None
    assert check(title="Software Intern (4 months)", terms=[]).filter_reason is None
    # Old, but the source marks closures itself, so age alone is not a reason.
    assert check(date_posted=date(2026, 8, 1)).filter_reason is None
    unclear = check(locations=["Remote"])
    assert "location unclear" in unclear.flags
    assert "no sponsorship offered" in check(sponsorship="Does Not Offer Sponsorship").flags


def test_profile_skills_raise_the_score(example_home) -> None:
    from jobtool.profile import load_profile

    profile = load_profile(example_home / "profile").profile
    plain = check(profile=profile)
    named = check(title="Software Engineer Intern (C++ / Python)", profile=profile)
    assert named.score > plain.score
    assert set(named.detail["matched_skills"]) == {"C++", "Python"}


@pytest.mark.parametrize(
    ("title", "degrees"),
    [
        ("PhD Machine Learning Software Engineer Intern", []),
        ("Software Engineer Intern - MS/PhD", []),
        ("MBA Software Strategy Intern", []),
        ("Software Engineer Intern", ["PhD"]),
        ("Software Engineer Intern", ["Master's", "PhD"]),
    ],
)
def test_other_degree_levels_are_filtered(title: str, degrees: list[str]) -> None:
    assert check(title=title, degrees=degrees).filter_reason.startswith("degree")


@pytest.mark.parametrize(
    ("title", "degrees"),
    [
        ("Software Engineer Intern", []),
        ("Software Engineer Intern - BS/MS", []),
        ("Software Engineer Intern", ["Bachelor's", "Master's"]),
        ("Software Engineer Intern (Undergraduate)", []),
    ],
)
def test_bachelor_roles_pass(title: str, degrees: list[str]) -> None:
    assert check(title=title, degrees=degrees).filter_reason is None
