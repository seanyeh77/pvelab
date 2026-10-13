"""pvelab upgrade"""

import hashlib
import io
import os
import zipfile

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
        print(f">> {me} is already up to date ({hashlib.sha256(old).hexdigest()[:12]})")
        return
    tmp = f"{me}.new"
    with open(tmp, "wb") as f:
        f.write(new)
    os.chmod(tmp, os.stat(me).st_mode & 0o7777)
    os.replace(tmp, me)
    print(f">> {me} updated: {hashlib.sha256(old).hexdigest()[:12]} -> {hashlib.sha256(new).hexdigest()[:12]}")
