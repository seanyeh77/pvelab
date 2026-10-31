#!/usr/bin/env python3
"""Build dist/pvelab: the pvelab package as one executable file (a Python zip app).

install.sh and `pvelab upgrade` download this one file. The zip is reproducible (sorted
entries, fixed timestamps), so the same source always gives the same bytes and
`pvelab upgrade` only replaces the file when something really changed.
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "src" / "pvelab"
SCRIPTS = ROOT / "scripts"                        # the script library (pvelab scripts)
OUT = ROOT / "dist" / "pvelab"
SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = "from pvelab.cli import entry\n\nentry()\n"


def add(z, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def version(files):
    """<date of the last commit>+<hash of the sources and library scripts>: the same files give the
    same version, and a changed script alone gives a new one (so pvelab upgrade delivers it)."""
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(ROOT).as_posix().encode() + b"\0" + p.read_bytes())
    r = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cd", "--date=format:%Y.%m.%d"],
                       capture_output=True, text=True)
    return f"{r.stdout.strip() or 'unknown'}+{h.hexdigest()[:8]}"


def main():
    files = sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts and p.name != "_version.py")
    for p in files:                                   # fail here, not on a PVE host
        compile(p.read_bytes(), str(p.relative_to(ROOT)), "exec")
    libs = sorted(SCRIPTS.glob("*.sh"))
    ver = version(files + libs)
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(SHEBANG)
        with zipfile.ZipFile(f, "w") as z:
            add(z, "__main__.py", MAIN)
            for p in files:
                add(z, p.relative_to(PKG.parent).as_posix(), p.read_bytes())
            for p in libs:
                add(z, f"pvelab/library/{p.name}", p.read_bytes())
            add(z, "pvelab/_version.py", f'VERSION = "{ver}"\n')
    os.chmod(tmp, 0o755)
    os.replace(tmp, OUT)
    print(f"built {OUT.relative_to(ROOT)} {ver} ({len(files)} modules, {len(libs)} scripts, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
