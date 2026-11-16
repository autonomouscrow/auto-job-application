"""The truth profile: the only place facts about the applicant live."""

from jobtool.profile.loader import ProfileError, load_profile
from jobtool.profile.models import Fact, Profile

__all__ = ["Fact", "Profile", "ProfileError", "load_profile"]
