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
    # the lock is held during the check (nothing new can come in), then let go
    assert [c[:3] for c in calls] == [("pvesh", "create", "/cluster/sdn/lock"), ("pvesh", "delete", "/cluster/sdn/lock")]
    assert calls[-1][-1] == "tok1" and sdn._token is None


def test_session_checks_after_taking_the_lock(calls, monkeypatch):
    seen = []
    monkeypatch.setattr(sdn, "foreign_pending", lambda: seen.append(len(lock_creates(calls))) or [])
    with sdn.session():
        pass
    assert seen == [1]


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


def lock_creates(calls):
    return [c for c in calls if c[:3] == ("pvesh", "create", "/cluster/sdn/lock")]


def test_undo_after_apply_takes_the_lock_again(calls, monkeypatch):
    monkeypatch.setattr(sdn, "make_rules_add_once", lambda: None)
    toks = iter(["tok1\n", "tok2\n"])
    rec = calls

    def fake_run(*cmd, **kw):
        rec.append(cmd)
        return next(toks) if cmd[:3] == ("pvesh", "create", "/cluster/sdn/lock") else ""
    monkeypatch.setattr(sdn, "run", fake_run)
    checked = []
    with sdn.session():
        sdn.apply()
        monkeypatch.setattr(sdn, "foreign_pending", lambda: checked.append(1) or [])
        sdn.remove_vnet("33")
        sdn.apply()
    assert len(lock_creates(calls)) == 2 and checked
    deletes = [c for c in calls if c[:2] == ("pvesh", "delete") and "/cluster/sdn/lock" not in c]
    assert deletes and all(c[-2:] == ("--lock-token", "tok2") for c in deletes)
    assert calls[-1] == ("pvesh", "set", "/cluster/sdn", "--lock-token", "tok2", "--release-lock", "1")


def test_undo_after_apply_leaves_foreign_changes_alone(calls, monkeypatch):
    monkeypatch.setattr(sdn, "make_rules_add_once", lambda: None)
    warned = []
    monkeypatch.setattr(sdn, "warn", warned.append)
    with sdn.session():
        sdn.apply()
        monkeypatch.setattr(sdn, "foreign_pending", lambda: ["vnet web1 (new)"])
        n_before = len(calls)
        sdn.remove_vnet("33")
        sdn.apply()
    after = calls[n_before:]
    sets = [c for c in after if c[:3] == ("pvesh", "set", "/cluster/sdn")]
    assert not sets                                     # their change is not pushed
    assert ("pvesh", "delete", "/cluster/sdn/lock", "--lock-token", "tok1") in after      # the re-take let go
    deletes = [c for c in after if c[:2] == ("pvesh", "delete") and "/cluster/sdn/lock" not in c]
    assert deletes and not any("--lock-token" in c for c in deletes)     # pvelab's own removal stays pending
    assert len(warned) == 1 and "vnet web1 (new)" in warned[0]
    assert sdn._token is None and not sdn._held_back


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
    monkeypatch.setattr(d.sdn, "foreign_pending", lambda: [])
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


def test_destroy_keeping_the_bridge_is_not_blocked_by_foreign_sdn_changes(destroy_env, monkeypatch, capsys):
    d, log = destroy_env
    monkeypatch.setattr(d.sdn, "refuse_foreign", lambda: log.append("refused"))
    monkeypatch.setattr(d.sdn, "foreign_pending", lambda: ["vnet web1 (new)"])
    monkeypatch.setattr("builtins.input", lambda p="": log.append("ask") or "y")
    d.cmd_destroy(object())                  # a terminal: the VMs go, the vnet stays, nothing is asked
    assert log == [] and "kept: SDN has changes that are not applied yet (vnet web1 (new))" \
        in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    d.cmd_destroy(object())                  # no terminal, no LAB_YES: the bridge is kept, no check
    assert log == []


def test_destroy_with_lab_yes_refuses_foreign_sdn_changes_first(destroy_env, monkeypatch):
    d, log = destroy_env
    monkeypatch.setattr(d, "YES", True)
    monkeypatch.setattr(d.sdn, "refuse_foreign", lambda: log.append("refused"))
    monkeypatch.setattr(d, "confirm_yes", lambda m: log.append("confirm"))
    d.cmd_destroy(object())
    assert log[:2] == ["refused", "confirm"]


def test_undo_when_the_lock_cannot_be_taken_again_goes_on(calls, monkeypatch):
    monkeypatch.setattr(sdn, "make_rules_add_once", lambda: None)
    warned = []
    monkeypatch.setattr(sdn, "warn", warned.append)
    with sdn.session():
        sdn.apply()
        rec = calls

        def busy(*cmd, **kw):
            rec.append(cmd)
            if cmd[:3] == ("pvesh", "create", "/cluster/sdn/lock"):
                raise LabError("the SDN lock is held")
            return ""
        monkeypatch.setattr(sdn, "run", busy)
        n_before = len(calls)
        sdn.remove_vnet("33")                         # no error: the undo goes on
        sdn.apply()
    after = calls[n_before:]
    assert not [c for c in after if c[:3] == ("pvesh", "set", "/cluster/sdn")]
    assert len(warned) == 1 and "could not be taken again" in warned[0]
    assert sdn._token is None and not sdn._held_back


def test_conflict_reads_vnets_of_any_zone_and_subnets(monkeypatch):
    from pvelab import config
    with open(config.SDN_VNETS, "w") as f:
        f.write("vnet: lab33\n\tzone office\n\tag 9\n")
    with open(config.SDN_SUBNETS, "w") as f:
        f.write("subnet: office-10.10.34.0-24\n\tvnet web1\n")
    assert "vnet lab33 exists" in sdn.conflict("33")
    assert "subnet 10.10.34.0/24" in sdn.conflict("34")
    assert sdn.conflict("34", subnet=False) is None and sdn.conflict("35") is None
