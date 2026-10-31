"""The script library: scripts/*.sh in the repo, packed into the single-file pvelab by build.py."""

import functools
import importlib.resources
import re
from pathlib import Path

NAME_RE = re.compile(r"[a-z][a-z0-9-]*")      # a library script's name: its file name without .sh


def _dir():
    """Where the library is: pvelab/library inside the zip app, else <repo>/scripts (run from
    source, or in tests), else None."""
    packed = importlib.resources.files("pvelab") / "library"
    if packed.is_dir():
        return packed
    repo = Path(__file__).resolve().parents[2] / "scripts"
    return repo if repo.is_dir() else None


@functools.lru_cache(maxsize=None)          # read once per run (a chain of N entries asks N times)
def library():
    """{name: (description, bytes)} of every library script, sorted by name. The description is
    line 2 of the script without its leading '# '. Shared by every caller: don't change it."""
    d = _dir()
    if d is None:
        return {}
    found = {}
    for f in sorted(d.iterdir(), key=lambda f: f.name):
        if not f.name.endswith(".sh"):
            continue
        name = f.name[:-3]
        if not NAME_RE.fullmatch(name):
            continue
        data = f.read_bytes()
        lines = data.decode(errors="replace").splitlines()
        desc = lines[1].removeprefix("#").strip() if len(lines) > 1 else ""
        found[name] = (desc, data)
    return found


def library_lines():
    """'  name   description' rows, names aligned: what pvelab scripts and the wizard show."""
    lib = library()
    width = max((len(n) for n in lib), default=0)
    return [f"  {n:<{width}}   {desc}" for n, (desc, _) in lib.items()]
