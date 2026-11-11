"""pvelab node remove: the checks, the plan, the steps in order - no host commands."""
import pytest

from pvelab import removing
from pvelab.util import LabError

MEM = {"workshop1": {"ip": "10.0.0.123", "online": True}, "workshop2": {"ip": None, "online": False}}


def job(**kw):
    j = {"name": "workshop2", "this": "workshop1", "members": MEM, "addr": "10.0.0.229", "copies": {9100: "debian-13-cloud"},
         "others": [], "quorate": False, "peers": "10.0.0.123,10.0.0.229", "backup": "/root/lab/b"}
    return {**j, **kw}


def test_problems():
    assert removing.problems(job()) == []
    assert "not a host" in removing.problems(job(name="pve9"))[0]
    assert "this host" in removing.problems(job(name="workshop1"))[0]
    online = {**MEM, "workshop2": {"ip": "10.0.0.229", "online": True}}
    assert "power it off first" in removing.problems(job(members=online))[0]
    assert "201 (net2-student01)" in removing.problems(job(others=["201 (net2-student01)"]))[0]
    alone = {"workshop1": {"ip": None, "online": False}, "workshop2": MEM["workshop2"]}
    assert "no other host is online" in removing.problems(job(members=alone))


def test_peers_after():
    assert removing.peers_after(job()) == "10.0.0.123"
    assert removing.peers_after(job(peers="10.0.0.123")) is None           # not a peer: no SDN apply
    assert removing.peers_after(job(peers="")) is None and removing.peers_after(job(addr=None)) is None


def test_without_host():
    text = "workshop1,10.0.0.123 ssh-ed25519 A\nworkshop2 ssh-ed25519 B\n10.0.0.229 ssh-rsa C\n|1|hash ssh-rsa D\n"
    assert removing.without_host(text, ["workshop2", "10.0.0.229"]) == "workshop1,10.0.0.123 ssh-ed25519 A\n|1|hash ssh-rsa D\n"


def test_plan_names_every_step(capsys):
    removing.show_plan(job(), dry=True)
    out = capsys.readouterr().out
    for s in ("back up config.db", "pvecm expected 1 first", "pvecm delnode workshop2",
              "/etc/pve/nodes/workshop2 with its template copies debian-13-cloud (9100)",
              "peers 10.0.0.123 (without 10.0.0.229)", "reload", "Reinstall it"):
        assert s in out, s


def fake(monkeypatch, j, calls):
    monkeypatch.setattr(removing, "survey", lambda name: j)
    monkeypatch.setattr(removing, "back_up", lambda j: calls.append("backup"))
    monkeypatch.setattr(removing, "delnode", lambda name: calls.append(("delnode", name)))
    monkeypatch.setattr(removing, "node_dir", lambda name: "/nonexistent/" + name)
    monkeypatch.setattr(removing, "forget_keys", lambda j: calls.append("keys"))
    monkeypatch.setattr(removing, "members", lambda: {"workshop1": MEM["workshop1"]})
    monkeypatch.setattr(removing.Plan, "ask", lambda self: calls.append("asked"))

    class Session:
        def __enter__(self):
            calls.append("sdn lock")

        def __exit__(self, *a):
            calls.append("sdn unlock")
    monkeypatch.setattr(removing.sdn, "session", Session)
    monkeypatch.setattr(removing.sdn, "ensure_zone", lambda: calls.append("peers"))
    monkeypatch.setattr(removing.sdn, "apply", lambda: calls.append("apply"))
    monkeypatch.setattr(removing.sdn, "zone", lambda: {"peers": "10.0.0.123"})


def test_remove_steps_in_order(monkeypatch, capsys):
    calls = []
    fake(monkeypatch, job(), calls)
    removing.node_remove("workshop2")
    assert calls == ["asked", "backup", ("delnode", "workshop2"), "keys", "sdn lock", "peers", "apply", "sdn unlock"]
    assert "workshop2 is out of the cluster; hosts: workshop1" in capsys.readouterr().out


def test_remove_without_sdn_peer_skips_the_apply(monkeypatch):
    calls = []
    fake(monkeypatch, job(peers=""), calls)
    removing.node_remove("workshop2")
    assert "apply" not in calls and ("delnode", "workshop2") in calls


def test_dry_run_and_problems_change_nothing(monkeypatch, capsys):
    calls = []
    fake(monkeypatch, job(), calls)
    removing.node_remove("workshop2", dry=True)
    assert calls == [] and "dry run - nothing was changed" in capsys.readouterr().out
    fake(monkeypatch, job(others=["201 (x)"]), calls)
    with pytest.raises(LabError, match="nothing was changed"):
        removing.node_remove("workshop2")
    assert calls == []


def test_cli_routes_node_remove(monkeypatch):
    from pvelab import cli
    from pvelab.commands import node
    got = []
    monkeypatch.setattr(node, "node_remove", lambda name, dry=False: got.append((name, dry)))
    node.cmd_node_remove(["remove", "workshop2"], dry=True)
    assert got == [("workshop2", True)] and cli.command("node", ["remove", "workshop2"]) == "node remove"
    with pytest.raises(LabError):
        node.cmd_node_remove(["remove"])
