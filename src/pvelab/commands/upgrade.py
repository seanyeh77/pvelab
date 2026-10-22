"""pvelab upgrade"""

import hashlib
import io
import os
import re
import zipfile

from .. import VERSION
from ..config import UPGRADE_URL
from ..util import die, elevate, http_open, package_root


def build_problem(data):
    """Why data is not a pvelab build (the single-file zip app), or None if it is one."""
    if not data.startswith(b"#!/usr/bin/env python3"):
        return f"the download is not pvelab (an HTML or error page? check that {UPGRADE_URL} is the raw file)"
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return "the download is not a pvelab build (not a zip app) - not installed"
    if "__main__.py" not in names or "pvelab/cli.py" not in names:
        return "the download is a zip app but not pvelab - not installed"
    return None


def cmd_upgrade():
    """Download pvelab again from UPGRADE_URL and replace this file (sudo only if needed)."""
    me = os.path.realpath(package_root())
    if not zipfile.is_zipfile(me):
        die(f"this pvelab runs from {me}, not from an installed single file - update it with "
            "git pull (or pip), or install the single file with install.sh")
    if not os.access(me, os.W_OK) or not os.access(os.path.dirname(me), os.W_OK):
        if os.geteuid() != 0:
            elevate()
        die(f"cannot write {me}")
    print(f">> downloading {UPGRADE_URL}")
    try:
        new = http_open(UPGRADE_URL, timeout=60).read()
    except OSError as e:
        die(f"download failed: {e}")
    problem = build_problem(new)
    if problem:
        die(problem)
    old = open(me, "rb").read()
    if new == old:
        print(f">> {me} is already up to date ({VERSION})")
        return
    tmp = f"{me}.new"
    with open(tmp, "wb") as f:
        f.write(new)
    os.chmod(tmp, os.stat(me).st_mode & 0o7777)
    os.replace(tmp, me)
    print(f">> {me} updated: {VERSION} -> {installed_version(io.BytesIO(new)) or hashlib.sha256(new).hexdigest()[:12]}")
    for line in copies_warnings():
        print(f">> {line}")


def installed_version(path):
    """VERSION inside a pvelab single file (a path or file object), or None."""
    try:
        with zipfile.ZipFile(path) as z:
            m = re.search(r'VERSION = "([^"]+)"', z.read("pvelab/_version.py").decode())
            return m.group(1) if m else None
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def other_copies():
    """Other installed pvelab files that differ from the one running (sudo pvelab and pvelab can
    otherwise run different versions without anyone noticing)."""
    me = os.path.realpath(package_root())
    places = ["/usr/local/bin/pvelab"]
    user = os.environ.get("SUDO_USER")
    if user:
        places.append(os.path.expanduser(f"~{user}/.local/bin/pvelab"))
    elif os.geteuid():
        places.append(os.path.expanduser("~/.local/bin/pvelab"))
    out = []
    for p in places:
        v = installed_version(p) if os.path.isfile(p) and os.path.realpath(p) != me else VERSION
        if v != VERSION:
            out.append((p, v or "an old version"))
    return out


def copies_warnings():
    return [f"another pvelab is installed: {p} ({v}); this one is {VERSION} - keep one "
            "('pvelab' and 'sudo pvelab' may run different ones)" for p, v in other_copies()]


def cmd_version():
    print(f"pvelab {VERSION}  ({package_root()})")
    for line in copies_warnings():
        print(line)
