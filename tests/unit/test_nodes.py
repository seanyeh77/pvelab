"""Node routing: a command for a VM on another PVE node goes there over ssh; one node changes nothing."""
import json
import os
import shlex
import subprocess

import pytest

from pvelab import guest, inventory, lifecycle, nodes, resources
from pvelab.util import LabError

A = os.uname().nodename.split(".")[0]          # this node
B = "b"


def cluster(pve, online_b=True):
    """A two-node .members (this node and b) like pmxcfs writes it."""
    (pve / ".members").write_text(json.dumps({"nodename": A, "version": 4, "cluster": {"name": "c", "quorate": 1},
                                              "nodelist": {A: {"id": 1, "online": 1, "ip": "10.0.0.123"},
                                                           B: {"id": 2, "online": int(online_b), "ip": "10.0.0.229"}}}))


def vm(pve, node, vmid, text="name: x\n"):
    d = pve / "nodes" / node / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(text)


def test_members_two_nodes(empty_pve):
    cluster(empty_pve)
    assert nodes.members() == {A: {"ip": "10.0.0.123", "online": True, "id": 1},
                               B: {"ip": "10.0.0.229", "online": True, "id": 2}}
    assert nodes.multi() and nodes.node_names() == sorted([A, B])


@pytest.mark.parametrize("text", [None, "not json", json.dumps({"nodename": A, "version": 0})])   # no cluster
def test_members_fall_back_to_this_node(empty_pve, text):
    if text is not None:
        (empty_pve / ".members").write_text(text)
    assert nodes.members() == {A: {"ip": "192.0.2.1", "online": True}} and not nodes.multi()


def test_node_of(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, B, 301)
    assert nodes.node_of(301) == B and nodes.node_of(302) is None


def test_one_host_ignores_a_leftover_node_dir(empty_pve):
    """No cluster: a directory left from an old hostname is not a node; its VMs are not listed or reached."""
    vm(empty_pve, A, 201)
    vm(empty_pve, "oldname", 202)
    assert list(inventory.inventory()) == [201]
    assert nodes.node_of(202) is None and inventory.conf_path(202) is None


def test_members_without_ip(empty_pve):
    """pmxcfs leaves out "ip" for a node it has no address for: the other nodes still count."""
    (empty_pve / ".members").write_text(json.dumps({"nodename": A, "version": 4, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, B: {"id": 2, "online": 0}}}))
    assert nodes.members() == {A: {"ip": "10.0.0.123", "online": True, "id": 1},
                               B: {"ip": None, "online": False, "id": 2}}
    assert nodes.multi()


def test_on_node_local_is_unchanged(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, A, 201)
    assert nodes.qm_argv(201, "start", 201) == ["qm", "start", 201]
    assert nodes.on_node(None, "qm", "list") == ["qm", "list"]


def test_on_node_remote(empty_pve):
    cluster(empty_pve)
    cmd = ["qm", "guest", "exec", "301", "--", "sh", "-c", "echo 'a b' \"c\" $HOME; x"]
    argv = nodes.on_node(B, *cmd)
    assert argv[0] == "ssh" and "HostKeyAlias=b" in argv and "BatchMode=yes" in argv
    assert f"ControlPath={empty_pve}/ssh-%C" in argv and argv[-3:-1] == ["root@10.0.0.229", "--"]
    assert shlex.split(argv[-1]) == cmd       # what the remote shell runs: the same argv


def test_on_node_offline(empty_pve):
    cluster(empty_pve, online_b=False)
    with pytest.raises(LabError, match="host b is offline"):
        nodes.on_node(B, "qm", "list")


def test_power_start_on_node_b(empty_pve, monkeypatch):
    from pvelab.commands import power
    cluster(empty_pve)
    vm(empty_pve, B, 301)
    calls = []
    monkeypatch.setattr(power, "targets", lambda a: [301])
    monkeypatch.setattr(power, "vm_statuses", lambda ids: {301: "stopped"})
    monkeypatch.setattr(power, "vm_names", lambda ids: {301: "net95-student01"})
    monkeypatch.setattr(lifecycle, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(lifecycle, "agent_up", lambda i, node=None: True)
    power.cmd_power("start", ["net95-student01"])
    (cmd,) = calls
    assert cmd[0] == "ssh" and "root@10.0.0.229" in cmd and cmd[-1] == "qm start 301"


def test_guest_json_routes_by_vmid_and_passes_stdin(empty_pve, monkeypatch):
    cluster(empty_pve)
    vm(empty_pve, B, 301)
    vm(empty_pve, A, 201)
    seen = []

    def fake(argv, input=None, **k):
        seen.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, b'{"pid": 7}', b"")
    monkeypatch.setattr(guest.subprocess, "run", fake)
    assert guest.guest_json("exec", 301, "--pass-stdin", 1, "--", "sh", "-c", "cat > x", data=b"hi") == {"pid": 7}
    assert guest.guest_json("cmd", 201, "ping") == {"pid": 7}
    (remote, data), (local, _) = seen
    assert remote[0] == "ssh" and shlex.split(remote[-1])[:4] == ["qm", "guest", "exec", "301"] and data == b"hi"
    assert local == ["qm", "guest", "cmd", "201", "ping"]


QM_LIST = "      VMID NAME     STATUS     MEM(MB)    BOOTDISK(GB) PID\n       {} x  {}  2048  10.00  0\n"


def test_vm_statuses_one_call_per_node(empty_pve, monkeypatch):
    cluster(empty_pve)
    vm(empty_pve, A, 201)
    vm(empty_pve, B, 301)
    calls = []

    def run(*cmd, **k):
        calls.append(cmd)
        return QM_LIST.format(301, "stopped") if cmd[0] == "ssh" else QM_LIST.format(201, "running")
    monkeypatch.setattr(inventory, "run", run)
    assert inventory.vm_statuses([201, 301]) == {201: "running", 301: "stopped"}
    assert sorted(c[0] for c in calls) == ["qm", "ssh"]
    calls.clear()
    assert inventory.vm_statuses([201]) == {201: "running"}
    assert calls == [("qm", "list")]          # one node: exactly today's call


def test_inventory_marks_each_vms_node(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, A, 201, "name: s1\n#managed\n")
    vm(empty_pve, B, 301, "name: s2\n")
    inv = inventory.inventory()
    assert {i: c["_node"] for i, c in inv.items()} == {201: A, 301: B}
    assert inv[201]["name"] == "s1" and inv[201]["description"] == "managed"
    assert inventory.read_conf(301)["_node"] == B           # read_conf finds a VM on another node too


def test_a_catalogue_template_on_another_node_is_elsewhere():
    from pvelab import templates
    t = templates.catalogue_find("ubuntu24")
    conf = {"template": "1", "name": t.name, "description": ""}
    assert templates.template_state(t, conf) == "ours"                       # no _node: this node
    assert templates.template_state(t, {**conf, "_node": B}) == "elsewhere"


@pytest.mark.parametrize("node", [A, B])
def test_clone_hands_over_the_keys(empty_pve, monkeypatch, node):
    """On this node and on b: qm set reads the keys from its stdin."""
    from pvelab import lifecycle
    cluster(empty_pve)
    vm(empty_pve, node, 9004)
    calls = []

    def run(*cmd, data=None, **k):
        calls.append((shlex.split(cmd[-1]) if cmd[0] == "ssh" else list(cmd), data))
        if "clone" in calls[-1][0]:
            vm(empty_pve, node, 301)         # qm clone made the VM on the template's node
        return ""
    monkeypatch.setattr(lifecycle, "run", run)
    monkeypatch.setattr(lifecycle, "ssh_keys", lambda: "ssh-ed25519 AAAA x\n")
    monkeypatch.setattr(lifecycle, "cred_save", lambda rows: None)
    monkeypatch.setattr(lifecycle, "host_dns", lambda: "10.10.95.1")
    monkeypatch.setattr(lifecycle, "bridge", lambda n: "lab95")
    lifecycle.clone_vm(301, "net95-student01", 9004, "95", "10.10.95.11", {}, "pw", 2, 2048, None)
    assert [c[0][:2] for c in calls] == [["qm", "clone"], ["qm", "set"], ["qm", "snapshot"], ["qm", "start"]]
    qm_set, data = calls[1]
    assert qm_set[qm_set.index("--sshkeys") + 1] == "/dev/stdin" and data == "ssh-ed25519 AAAA x\n"


def test_list_host_column_only_with_two_nodes(empty_pve, monkeypatch, capsys):
    from pvelab.commands import listing
    conf = {"name": "net95-student01", "tags": "lab-student", "description": "managed-by=lab.sh template=9004 net=95",
            "memory": "2048", "cores": "2", "_node": B}
    monkeypatch.setattr(listing, "cred_rows", lambda: {})
    monkeypatch.setattr(listing, "inventory", lambda: {301: conf})
    monkeypatch.setattr(listing, "is_exposed", lambda n: False)
    monkeypatch.setattr(listing, "forwarded_on", lambda nets: set())
    stats = {301: {"status": "running", "node": B, "maxmem": 2**31}}
    monkeypatch.setattr(listing, "live", lambda: (stats, {B: (16384, 4096)}))
    monkeypatch.setattr(listing, "storage", lambda node=None: (50, 100))
    monkeypatch.setattr(listing, "blocks_load", lambda: {})
    monkeypatch.setattr(listing, "vm_ip", lambda i, rows, up: "10.10.95.11")
    monkeypatch.setattr(listing, "is_isolated", lambda n: True)
    monkeypatch.setattr(listing, "host_mem_mb", lambda: (65536, 32768))
    monkeypatch.setattr(resources, "host_mem_mb", lambda node=None: (65536, 32768))
    listing.cmd_list([])
    out = capsys.readouterr().out
    assert "HOST" not in out and "memory: running VMs" in out and "local-lvm: 50% used" in out
    cluster(empty_pve)
    listing.cmd_list([])
    out = capsys.readouterr().out
    head, row = out.splitlines()[:2]
    assert head.split()[4:6] == ["STATUS", "HOST"] and row.split()[4:6] == ["running", B]
    assert f"memory on {A}: running VMs may use 0 GB of the host's 64 GB (32 GB available now)" in out
    assert f"memory on {B}: running VMs may use 2 GB of the host's 16 GB (12 GB available now)" in out
    assert f"local-lvm on {A}: 50% used" in out
    monkeypatch.setattr(listing, "live", lambda: (stats, {}))     # b offline
    listing.cmd_list([])
    assert f"memory on {B}: host offline" in capsys.readouterr().out


def test_snapshot_names_of_a_vm_on_another_node(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, B, 301, "name: x\n\n[clean]\nname: x\n\n[pending]\nmemory: 4096\n")
    assert inventory.conf_path(301).endswith(f"/nodes/{B}/qemu-server/301.conf")
    assert inventory.snapshot_names(301) == ["clean"]


def test_top_other_hosts(empty_pve, monkeypatch, capsys):
    from pvelab.commands import top
    cluster(empty_pve)
    top.other_hosts({B: (16384, 4096)})
    assert capsys.readouterr().out.strip() == f"host {B}: memory 4/16 GB used"
    top.other_hosts({})
    assert f"host {B}: offline" in capsys.readouterr().out


def test_hour_cpu_asks_the_vms_node(empty_pve, monkeypatch):
    from pvelab.commands import top
    cluster(empty_pve)
    vm(empty_pve, B, 301)
    asked = []
    monkeypatch.setattr(top, "pvesh_json", lambda path, *a: asked.append(path) or [{"cpu": 0.5}, {"cpu": 1.0}])
    assert top.hour_cpu(301) == 0.75 and asked == [f"/nodes/{B}/qemu/301/rrddata"]


@pytest.mark.parametrize("argv, locked", [(["node", "list"], False), (["nodes"], False), (["node", "add", "10.0.0.229"], True),
                                          (["nodes", "add", "10.0.0.229"], True)])
def test_node_lock(monkeypatch, tmp_path, argv, locked):
    """node list only looks (no lock); node add changes the cluster (the lock)."""
    from pvelab import cli
    ran, locks = [], []
    monkeypatch.setattr(cli, "pve_check", lambda: None)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "LAB_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CRED", str(tmp_path / "credentials.tsv"))
    monkeypatch.setattr(cli, "ensure_snippet", lambda: None)
    monkeypatch.setattr(cli.fcntl, "flock", lambda f, op: locks.append(op))
    name = cli.command(argv[0], argv[1:])
    monkeypatch.setitem(cli.COMMANDS, name, (lambda args: ran.append(args), cli.COMMANDS[name][1]))
    old = os.umask(0o022)
    try:
        cli.main(argv)
    finally:
        os.umask(old)
    assert ran == [argv[1:]] and bool(locks) == locked
