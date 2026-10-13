#!/usr/bin/env python3
"""Build dist/pvelab: the pvelab package as one executable file (a Python zip app).

install.sh and `pvelab upgrade` download this one file. The zip is reproducible (sorted
entries, fixed timestamps), so the same source always gives the same bytes and
`pvelab upgrade` only replaces the file when something really changed.
"""
import os
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "src" / "pvelab"
OUT = ROOT / "dist" / "pvelab"
SHEBANG = b"#!/usr/bin/env python3\n"
MAIN = "from pvelab.cli import entry\n\nentry()\n"


def add(z, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def main():
    files = sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)
    for p in files:                                   # fail here, not on a PVE host
        compile(p.read_bytes(), str(p.relative_to(ROOT)), "exec")
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(SHEBANG)
        with zipfile.ZipFile(f, "w") as z:
            add(z, "__main__.py", MAIN)
            for p in files:
                add(z, p.relative_to(PKG.parent).as_posix(), p.read_bytes())
    os.chmod(tmp, 0o755)
    os.replace(tmp, OUT)
    print(f"built {OUT.relative_to(ROOT)} ({len(files)} modules, {OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    sys.exit(main())
