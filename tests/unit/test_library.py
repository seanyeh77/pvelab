"""The script library: vendor-data snippet, the loader, pvelab scripts, and the CI header checks."""
import os
import pathlib
import re
import subprocess

import pytest

from pvelab import templates
from pvelab.commands.scripts import cmd_scripts
from pvelab.library import NAME_RE, library
from pvelab.util import LabError

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_ensure_snippet_writes_only_on_change(tmp_path, monkeypatch):
    cfg = tmp_path / "storage.cfg"
    cfg.write_text("dir: local\n\tpath /var/lib/vz\n\tcontent iso,vztmpl\n")
    calls = []
    monkeypatch.setattr(templates, "STORAGE_CFG", str(cfg))
    monkeypatch.setattr(templates, "SNIPPET_DIR", str(tmp_path / "snippets"))
    monkeypatch.setattr(templates, "run", lambda *a, **k: calls.append(a))
    assert templates.ensure_snippet() is True
    assert calls == [("pvesm", "set", "local", "--content", "iso,vztmpl,snippets")]
    snippet = tmp_path / "snippets" / templates.SNIPPET
    assert snippet.read_text() == templates.vendor_data()
    os.utime(snippet, (1, 1))                       # an old mtime: a rewrite would change it
    cfg.write_text("dir: local\n\tpath /var/lib/vz\n\tcontent iso,snippets\n")
    calls.clear()
    assert templates.ensure_snippet() is False
    assert snippet.stat().st_mtime == 1
    assert calls == []                              # snippets already listed: no pvesm


def test_library_from_source():
    lib = library()
    assert {"docker", "harden"} <= set(lib)
    assert lib["harden"][0] == 'Only "student" may ssh in; passwords need 10+ characters'
    assert lib["docker"][0].startswith("Install Docker Engine")
    assert lib["harden"][1].startswith(b"#!")


def test_cmd_scripts_list_and_print(capsys):
    cmd_scripts([])
    out = capsys.readouterr().out
    assert re.search(r"^  docker +Install Docker Engine", out, re.M)
    assert re.search(r"^  harden +Only \"student\" may ssh in", out, re.M)
    assert "use: pvelab create ... --script docker" in out
    cmd_scripts(["harden"])
    assert capsys.readouterr().out == (ROOT / "scripts" / "harden.sh").read_text()
    with pytest.raises(LabError, match="harden"):
        cmd_scripts(["hardn"])


@pytest.mark.parametrize("path", sorted((ROOT / "scripts").glob("*.sh")), ids=lambda p: p.name)
def test_script_headers(path):
    """CI check: what every library script must look like (see the design doc)."""
    data = path.read_bytes()
    assert len(data) <= 1 << 20, "over 1 MiB (the guest agent limit)"
    assert NAME_RE.fullmatch(path.stem), "name must match [a-z][a-z0-9-]*"
    lines = data.decode().splitlines()
    assert lines and lines[0].startswith("#!"), "line 1 must be the #! line"
    m = re.fullmatch(r"# (.+)", lines[1] if len(lines) > 1 else "")
    assert m and m.group(1).strip(), "line 2 must be '# <description>'"
    assert m.group(1).isascii() and re.search(r"[A-Za-z]", m.group(1)), "description: English text"
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            assert line.isascii(), f"line {n}: comments must be in English (ASCII)"
    if re.search(r"\b(bash|sh)$", lines[0].strip()):
        r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_library_folder_not_empty():
    assert list((ROOT / "scripts").glob("*.sh"))
