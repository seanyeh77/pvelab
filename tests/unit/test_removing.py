"""pvelab node remove: the checks, the plan, the steps in order - no host commands."""
import pytest

from pvelab import config, removing
from pvelab.util import LabError

MEM = {"workshop1": {"ip": "10.0.0.123", "online": True}, "workshop2": {"ip": None, "online": False}}


def job(**kw):
    j = {"name": "workshop2", "this": "workshop1", "members": MEM, "addr": "10.0.0.229", "copies": {9100: "debian-13-cloud"},
         "others": [], "quorate": False, "addrs": {"workshop1": "10.0.0.123", "workshop2": "10.0.0.229"},
         "backup": "/root/lab/b"}
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


def write_zones(text="vxlan: pvelab\n\tpeers 10.0.0.123, 10.0.0.229\n"):
    with open(config.SDN_ZONES, "w") as f:
        f.write(text)


def test_peer_changes():
    write_zones()
    assert removing.peer_changes(job()) == ({"pvelab": ["10.0.0.123"]}, [])
    write_zones("vxlan: pvelab\n\tpeers 10.0.0.123\n")                     # not a peer: no SDN apply
    assert removing.peer_changes(job()) == ({}, [])


def test_plan_names_each_zone_whose_peers_change(capsys):
    write_zones("vxlan: ALL\n\tpeers 10.0.0.1,10.0.0.2,10.0.0.3\n\nvxlan: SDC\n\tpeers 10.0.0.1,10.0.0.2\n"
                "\tnodes a,b\n\nvxlan: HAND\n\tpeers 10.0.0.9\n")
    mem = {h: {"ip": f"10.0.0.{i}", "online": h != "c"} for i, h in enumerate("abc", 1)}
    removing.show_plan(job(name="c", this="a", members=mem, addr="10.0.0.3",
                           addrs={h: m["ip"] for h, m in mem.items()}), dry=True)
    out = capsys.readouterr().out
    assert "SDN zone ALL: VXLAN peers 10.0.0.1, 10.0.0.2 (without 10.0.0.3)" in out
    assert "SDN zone SDC" not in out and "HAND: peers 10.0.0.9 differ" in out and "reload" in out


def test_survey_counts_the_host_gone_from_members(monkeypatch):
    monkeypatch.setattr(removing, "members", lambda: MEM)
    monkeypatch.setattr(removing, "quorate", lambda: True)
    monkeypatch.setattr(removing, "guest_confs", lambda name: [])
    monkeypatch.setattr(removing.zones, "addresses", lambda: {"workshop1": "10.0.0.123"})
    monkeypatch.setattr(removing, "ring_addr", lambda name: "10.0.0.229")
    assert removing.survey("workshop2")["addrs"] == {"workshop1": "10.0.0.123", "workshop2": "10.0.0.229"}


def test_without_host():
    text = "workshop1,10.0.0.123 ssh-ed25519 A\nworkshop2 ssh-ed25519 B\n10.0.0.229 ssh-rsa C\n|1|hash ssh-rsa D\n"
    assert removing.without_host(text, ["workshop2", "10.0.0.229"]) == "workshop1,10.0.0.123 ssh-ed25519 A\n|1|hash ssh-rsa D\n"


def test_plan_names_every_step(capsys):
    write_zones()
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
    monkeypatch.setattr(removing.sdn, "sync_peers", lambda before: calls.append(("peers", before)) or True)
    monkeypatch.setattr(removing.sdn, "apply", lambda: calls.append("apply"))


def test_remove_steps_in_order(monkeypatch, capsys):
    calls = []
    write_zones()
    fake(monkeypatch, job(), calls)
    removing.node_remove("workshop2")
    assert calls == ["asked", "backup", ("delnode", "workshop2"), "keys", "sdn lock", ("peers", job()["addrs"]),
                     "apply", "sdn unlock"]
    assert "workshop2 is out of the cluster; hosts: workshop1" in capsys.readouterr().out


def test_remove_without_any_peer_change_skips_the_apply(monkeypatch):
    calls = []
    write_zones("vxlan: pvelab\n\tpeers 10.0.0.123\n")
    fake(monkeypatch, job(), calls)
    removing.node_remove("workshop2")
    assert "apply" not in calls and ("delnode", "workshop2") in calls


def test_dry_run_and_problems_change_nothing(monkeypatch, capsys):
    calls = []
    write_zones()
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
