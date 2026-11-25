"""The ssh keys a new VM's user gets: every host's own root keys and ADMIN_KEYS, not root's authorized_keys."""
import pathlib

import pytest

from pvelab import credentials
from pvelab.util import LabError

A = "ssh-ed25519 AAAAhere root@here"
B = "ssh-ed25519 AAAAother root@other"
ADMIN = "ssh-ed25519 AAAAadmin alice@laptop"
OLD = "ssh-rsa AAAAold root@pve9"


@pytest.fixture(autouse=True)
def keys(tmp_path, monkeypatch):
    """This host "here" with key A and an authorized_keys of old and personal keys; the other host "other"."""
    ssh = tmp_path / "ssh"
    ssh.mkdir()
    (ssh / "id_ed25519.pub").write_text(A + "\n")
    (ssh / "authorized_keys").write_text(f"{OLD}\n{ADMIN}\n")
    monkeypatch.setattr(credentials, "ROOT_SSH_DIR", str(ssh))
    monkeypatch.setattr(credentials, "NODE_KEYS_DIR", str(tmp_path / "pve" / "node-keys"))
    monkeypatch.setattr(credentials, "ADMIN_KEYS", str(tmp_path / "pve" / "admin_keys"))
    monkeypatch.setattr(credentials, "this_node", lambda: "here")
    monkeypatch.setattr(credentials, "is_local", lambda n: n == "here")
    monkeypatch.setattr(credentials, "node_names", lambda: ["here", "other"])
    monkeypatch.setattr(credentials, "online_nodes", lambda: ["here", "other"])
    fetched = []
    monkeypatch.setattr(credentials, "run_on", lambda node, *cmd, **k: fetched.append(node) or B + "\n")
    credentials._ssh_keys.cache_clear()
    yield tmp_path, fetched
    credentials._ssh_keys.cache_clear()


def got():
    credentials._ssh_keys.cache_clear()
    return credentials.ssh_keys().splitlines()


def test_every_hosts_root_key_and_the_admins_not_authorized_keys(keys, capsys):
    tmp, fetched = keys
    pathlib.Path(credentials.ADMIN_KEYS).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(credentials.ADMIN_KEYS).write_text(f"# who may log in\n{ADMIN}\n\n")
    assert got() == sorted([A, B, ADMIN]) and OLD not in got()
    assert "admin_keys" not in capsys.readouterr().err


def test_both_keys_are_kept_and_an_online_hosts_read_again_each_run(keys):
    tmp, fetched = keys
    got()
    assert pathlib.Path(credentials.NODE_KEYS_DIR, "here.pub").read_text() == A + "\n"
    assert pathlib.Path(credentials.NODE_KEYS_DIR, "other.pub").read_text() == B + "\n"
    got()
    assert fetched == ["other", "other"]             # online: its key as it is now, not the kept one


def test_a_reinstalled_host_replaces_its_kept_key(keys, monkeypatch):
    d = pathlib.Path(credentials.NODE_KEYS_DIR)
    d.mkdir(parents=True)
    (d / "other.pub").write_text("ssh-rsa AAAAstale root@other\n")
    assert B in got() and "ssh-rsa AAAAstale root@other" not in got()
    assert (d / "other.pub").read_text() == B + "\n"


def test_an_offline_host_has_its_kept_key(keys, monkeypatch):
    tmp, fetched = keys
    got()
    monkeypatch.setattr(credentials, "online_nodes", lambda: ["here"])
    assert B in got() and fetched == ["other"]


def test_a_host_that_cannot_be_read_now_has_its_kept_key(keys, monkeypatch, capsys):
    got()
    monkeypatch.setattr(credentials, "run_on", lambda node, *cmd, **k: (_ for _ in ()).throw(LabError("timed out")))
    assert B in got()
    assert "other's root key not read (timed out): the one kept in" in capsys.readouterr().err


def test_admin_keys_with_options_are_kept_and_other_lines_said(keys, capsys):
    pathlib.Path(credentials.ADMIN_KEYS).parent.mkdir(parents=True, exist_ok=True)
    opt = 'from="10.0.0.0/8",no-pty ssh-ed25519 AAAAopt bob@desk'
    pathlib.Path(credentials.ADMIN_KEYS).write_text(f"# admins\n{opt}\nnot a key\n")
    assert opt in got()
    assert "admin_keys line 3 is no ssh public key - left out" in capsys.readouterr().err


def test_a_key_that_cannot_be_kept_is_said_not_fatal(keys, monkeypatch, capsys):
    monkeypatch.setattr(credentials.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("read-only")))
    assert got() == sorted([A, B])
    assert "not written (read-only): the other hosts read this key over ssh instead" in capsys.readouterr().err


def test_a_new_key_of_this_host_replaces_the_kept_one(keys):
    got()
    new = "ssh-ed25519 AAAAnew root@here"
    pathlib.Path(credentials.ROOT_SSH_DIR, "id_ed25519.pub").write_text(new + "\n")
    assert new in got() and A not in got()
    assert pathlib.Path(credentials.NODE_KEYS_DIR, "here.pub").read_text() == new + "\n"


def test_no_admin_keys_is_said(capsys):
    assert got() == sorted([A, B])
    assert "only pvelab ssh / run reach the new VMs" in capsys.readouterr().err


def test_an_offline_host_without_a_kept_key_is_said(monkeypatch, capsys, keys):
    tmp, fetched = keys
    monkeypatch.setattr(credentials, "online_nodes", lambda: ["here"])
    assert got() == [A] and fetched == []
    assert "other is offline and pvelab has no root key of it" in capsys.readouterr().err


def test_a_host_that_cannot_be_read_is_said(monkeypatch, capsys):
    monkeypatch.setattr(credentials, "run_on", lambda node, *cmd, **k: (_ for _ in ()).throw(LabError("ssh failed")))
    assert got() == [A]
    assert "other's root key not read (ssh failed): pvelab ssh / run from other will not reach" in capsys.readouterr().err


def test_no_root_key_here_stops(keys):
    pathlib.Path(credentials.ROOT_SSH_DIR, "id_ed25519.pub").unlink()
    with pytest.raises(LabError, match=r"no SSH public key of root on this host .*ssh-keygen"):
        got()


def test_read_once_a_run(keys):
    tmp, fetched = keys
    credentials.ssh_keys()
    pathlib.Path(credentials.NODE_KEYS_DIR, "other.pub").unlink()
    credentials.ssh_keys()
    assert fetched == ["other"]
