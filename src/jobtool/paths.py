"""Where personal data lives.

Everything personal (profile, config, API key, database, generated documents)
is kept in one folder outside the repository, called the home folder. It is
found, in order, from:

1. an explicit ``--home`` option,
2. the ``JOBTOOL_HOME`` environment variable,
3. a ``private`` folder next to the repository checkout.

No absolute path is ever written into the code or the repository.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "JOBTOOL_HOME"


class HomeNotFoundError(RuntimeError):
    """Raised when no home folder can be located."""


def repo_root() -> Path:
    """The repository checkout this package is running from."""
    return Path(__file__).resolve().parents[2]


def default_home() -> Path:
    """The conventional location: a ``private`` folder beside the repository."""
    return repo_root().parent / "private"


def find_home(explicit: Path | None = None, *, must_exist: bool = True) -> Path:
    """Locate the home folder."""
    if explicit is not None:
        home = Path(explicit)
    elif os.environ.get(ENV_VAR):
        home = Path(os.environ[ENV_VAR])
    else:
        home = default_home()
    home = home.expanduser()
    if must_exist and not home.is_dir():
        raise HomeNotFoundError(
            f"Home folder not found: {home}. Set {ENV_VAR} or pass --home. "
            "To try the tool with made-up data, use --home examples/home."
        )
    return home


def profile_dir(home: Path) -> Path:
    return home / "profile"


def config_path(home: Path) -> Path:
    return home / "config.yaml"
