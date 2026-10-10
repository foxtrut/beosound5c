"""No module or package beside a runnable script may share a stdlib name.

A script run directly — ``python3 services/sources/news.py``, the way the
systemd units start every service — gets its own directory as
``sys.path[0]``. A module or package in that directory named like a
standard-library module is then imported *instead of* the stdlib one, by
every library the script uses, not just by the script itself.

``services/sources/calendar/`` did exactly that: aiohttp's ``import
calendar`` picked up the calendar source's package, and every single-file
source in ``services/sources/`` (cd, news, weather, dr_news) crashed at
import on the device. The rest of the suite never noticed, because it
imports ``sources.news`` as a package with ``services/`` on the path, which
never puts ``services/sources`` first. Hence a check on the tree itself
rather than on imports.

If this fails, rename the module or package (e.g. ``<name>_source``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[3] / "services"
STDLIB = frozenset(sys.stdlib_module_names)
MAIN_GUARD = re.compile(r"""__name__\s*==\s*['"]__main__['"]""")


def _script_dirs() -> set[Path]:
    """Directories holding a script that can be run directly."""
    return {
        path.parent
        for path in SERVICES_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
        and MAIN_GUARD.search(path.read_text(encoding="utf-8", errors="ignore"))
    }


def _importable_name(entry: Path) -> str | None:
    """The top-level name ``entry`` would take when its directory is on sys.path.

    A directory without ``__init__.py`` is a namespace package, which loses to
    any regular module further down sys.path, so it cannot shadow the stdlib.
    """
    if entry.suffix == ".py":
        return entry.stem
    if entry.is_dir() and (entry / "__init__.py").exists():
        return entry.name
    return None


def test_script_dirs_are_found():
    """Guard the guard: the scan must see the directories systemd runs from."""
    dirs = _script_dirs()
    assert SERVICES_DIR in dirs
    assert SERVICES_DIR / "sources" in dirs


def test_no_stdlib_names_beside_runnable_scripts():
    clashes = sorted(
        str(entry.relative_to(SERVICES_DIR.parent))
        for directory in _script_dirs()
        for entry in directory.iterdir()
        if _importable_name(entry) in STDLIB
    )
    assert not clashes, (
        "These shadow a standard-library module for every script run from "
        "the same directory — rename them:\n  " + "\n  ".join(clashes)
    )
