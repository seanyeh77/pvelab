"""SDN: never apply other people's pending changes; hold PVE's SDN lock while pvelab changes SDN."""
import pytest

from pvelab import sdn
from pvelab.util import LabError


def test_pending_lists_only_foreign_items():
    listings = {
        "zones": [{"zone": "pvelab", "state": "changed"}],
        "vnets": [{"vnet": "lab33", "zone": "pvelab", "state": "new"},
                  {"vnet": "web1", "zone": "office", "state": "new"}],
        "subnets": [{"subnet": "pvelab-10.10.33.0-24", "vnet": "lab33", "state": "new"},
                    {"subnet": "office-192.168.9.0-24", "vnet": "web1", "state": "new"}],
        "controllers": [{"controller": "evpn1"}],
    }
    assert sdn.pending(listings) == ["vnet web1 (new)", "subnet office-192.168.9.0-24 (new)"]


@pytest.fixture
def calls(monkeypatch):
    rec = []

    def fake_run(*cmd, **kw):
        rec.append(cmd)
        return "tok1\n" if cmd[:3] == ("pvesh", "create", "/cluster/sdn/lock") else ""
    monkeypatch.setattr(sdn, "run", fake_run)
    monkeypatch.setattr(sdn, "pvesh_json", lambda *a, **k: [])
    monkeypatch.setattr(sdn, "node_ips", lambda: ["10.0.0.1"])
    monkeypatch.setattr(sdn, "zone_mtu", lambda: 1450)
    monkeypatch.setattr(sdn, "foreign_pending", lambda: [])
    return rec


def test_session_refuses_with_the_list(calls, monkeypatch):
    monkeypatch.setattr(sdn, "foreign_pending", lambda: ["vnet web1 (new)"])
    with pytest.raises(LabError) as e:
        with sdn.session():
            pytest.fail("entered")
    assert "vnet web1 (new)" in str(e.value) and "Apply or roll them back" in str(e.value)
    assert not any("/cluster/sdn/lock" in c for c in calls)


def test_session_passes_the_token_and_releases_on_error(calls):
    with pytest.raises(LabError):
        with sdn.session():
            sdn.add_vnet("33")
            raise LabError("boom")
    creates = [c for c in calls if c[:2] == ("pvesh", "create") and "/cluster/sdn/lock" not in c]
    assert creates and all(c[-2:] == ("--lock-token", "tok1") for c in creates)
    assert calls[-1] == ("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", "tok1")


def test_apply_releases_the_lock(calls, monkeypatch):
    monkeypatch.setattr(sdn, "make_rules_add_once", lambda: None)
    with sdn.session():
        sdn.apply()
    assert ("pvesh", "set", "/cluster/sdn", "--lock-token", "tok1", "--release-lock", "1") in calls
    assert not any(c[:2] == ("pvesh", "delete") and "/cluster/sdn/lock" in c for c in calls)


@pytest.fixture
def destroy_env(monkeypatch):
    from contextlib import contextmanager
    from pvelab.commands import destroy as d
    log = []

    @contextmanager
    def session():
        log.append("open")
        yield
        log.append("close")
    monkeypatch.setattr(d, "targets", lambda a: ["101"])
    monkeypatch.setattr(d, "cred_rows", lambda: {"101": {"net": "33"}})
    monkeypatch.setattr(d, "inventory", lambda: {})
    monkeypatch.setattr(d, "vm_names", lambda ids, inv: {"101": "vm"})
    monkeypatch.setattr(d, "net_of", lambda i, rows: "33")
    monkeypatch.setattr(d, "emptied_nets", lambda n, r: ["33"])
    monkeypatch.setattr(d, "is_sdn", lambda n: True)
    monkeypatch.setattr(d, "YES", False)
    monkeypatch.setattr(d.sdn, "refuse_foreign", lambda: None)
    monkeypatch.setattr(d.sdn, "session", session)
    monkeypatch.setattr(d.sdn, "apply", lambda: log.append("apply"))
    monkeypatch.setattr(d, "confirm_yes", lambda m: None)
    monkeypatch.setattr(d, "each", lambda *a, **k: None)
    monkeypatch.setattr(d, "remove_vm", lambda i: None)
    monkeypatch.setattr(d, "refresh_net", lambda n: None)
    monkeypatch.setattr(d, "remove_bridge", lambda n: log.append("remove") or True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    return d, log


def test_destroy_answers_before_the_sdn_lock(destroy_env, monkeypatch):
    d, log = destroy_env
    monkeypatch.setattr("builtins.input", lambda p="": log.append("ask") or "y")
    d.cmd_destroy(object())
    assert log == ["ask", "open", "remove", "apply", "close"]


def test_destroy_declined_never_takes_the_lock(destroy_env, monkeypatch):
    d, log = destroy_env
    monkeypatch.setattr("builtins.input", lambda p="": log.append("ask") or "n")
    d.cmd_destroy(object())
    assert log == ["ask"]
