"""Several hosts: where create puts students, and templates copied to the host that needs them."""
import json
import os
import shlex

import pytest

from pvelab import networks, nodes, placement, templates, util
from pvelab.util import LabError
from test_ux import fake_create

A = os.uname().nodename.split(".")[0]          # this node (workshops)
B = "b"
IMG = "image=abcdef0123456789"


def cluster(pve):
    (pve / ".members").write_text(json.dumps({"nodename": A, "version": 4, "nodelist": {
        A: {"id": 1, "online": 1, "ip": "10.0.0.123"}, B: {"id": 2, "online": 1, "ip": "10.0.0.229"}}}))


def vm(pve, node, vmid, text):
    d = pve / "nodes" / node / "qemu-server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vmid}.conf").write_text(text)


def tpl(desc, name="debian-13-cloud"):
    return f"#{desc}\nname: {name}\ntemplate: 1\nscsi0: local-lvm:base-9002-disk-0,size=10G\n"


ORIG = tpl(f"built by pvelab from https://x/debian.qcow2 {IMG} built=2026-10-01")


def remote(cmd):
    """The command an argv runs: itself here, or what ssh runs on the other node (with its node's IP)."""
    return (cmd[cmd.index("-T") + 1].removeprefix("root@"), shlex.split(cmd[-1])) if cmd[0] == "ssh" else ("here", list(map(str, cmd)))


# ---- place / fits / node_room / bridge_host

ROOM = {"a": {"mem": 16000, "disk": 100}, "b": {"mem": 8000, "disk": 100}}


def test_place_spreads_to_the_roomier_host_then_alternates():
    where = placement.place(["s1", "s2", "s3", "s4"], 4000, 6, ROOM)
    assert where == {"s1": "a", "s2": "a", "s3": "a", "s4": "b"}   # a: 4,3,2,1 more; b: 2,1
    where = placement.place(["s1", "s2", "s3", "s4"], 4000, 6, {**ROOM, "a": {"mem": 8000, "disk": 100}})
    assert where == {"s1": "a", "s2": "b", "s3": "a", "s4": "b"}   # equal room: alternate, ties by name


def test_place_node_puts_everyone_there_and_fixed_students_stay():
    assert set(placement.place(["s1", "s2"], 4000, 6, ROOM, node="b").values()) == {"b"}
    where = placement.place(["s1", "s2"], 4000, 6, ROOM, node="a", fixed={"s2": "b"})
    assert where == {"s1": "a", "s2": "b"}


def test_place_disk_counts_and_allowed_limits():
    room = {"a": {"mem": 64000, "disk": 5}, "b": {"mem": 8000, "disk": 100}}
    assert placement.place(["s1"], 2000, 6, room) == {"s1": "b"}           # a has memory, not disk
    assert placement.place(["s1", "s2"], 2000, 6, room, allowed=["a"]) == {"s1": "a", "s2": "a"}


def test_place_when_nothing_fits_still_picks_the_best():
    room = {"a": {"mem": -500, "disk": 1}, "b": {"mem": 100, "disk": 1}}
    assert placement.place(["s1"], 4000, 6, room) == {"s1": "b"}          # create's check warns and asks
    assert placement.place(["s1"], 4000, 6, {}) == {"s1": placement.this_node()}   # no numbers: here


def test_fits():
    assert placement.fits({"mem": 10000, "disk": 30}, 2048, 6) == 4
    assert placement.fits({"mem": 10000, "disk": 11}, 2048, 6) == 1
    assert placement.fits({"mem": -2000, "disk": 30}, 2048, 6) == 0


def test_node_room(monkeypatch):
    G, M = 2**30, 2**20
    rows = [{"type": "node", "node": "a", "status": "online", "maxmem": 64 * G, "mem": 30 * G},
            {"type": "node", "node": "b", "status": "online", "maxmem": 16 * G, "mem": 2 * G},
            {"type": "node", "node": "c", "status": "offline"},
            {"type": "storage", "node": "a", "storage": "local-lvm", "maxdisk": 100 * G, "disk": 90 * G},
            {"type": "storage", "node": "a", "storage": "local", "maxdisk": 100 * G, "disk": 0},
            {"type": "storage", "node": "b", "storage": "local-lvm", "maxdisk": 200 * G, "disk": 50 * G},
            {"type": "storage", "node": "c", "storage": "local-lvm", "maxdisk": 200 * G, "disk": 0},
            {"type": "qemu", "node": "a", "vmid": 201, "mem": 2 * G}]
    monkeypatch.setattr(placement, "pvesh_json", lambda path: rows if path == "/cluster/resources" else [])
    assert placement.node_room() == {"a": {"mem": 34 * 1024 - 2048, "disk": 10},
                                     "b": {"mem": 14 * 1024 - 2048, "disk": 150}}
    assert 34 * G // M == 34 * 1024


@pytest.mark.parametrize("net,sdn,expect", [("dhcp", True, A), ("30", False, A), ("95", True, None)])
def test_bridge_host(monkeypatch, net, sdn, expect):
    monkeypatch.setattr(placement, "is_sdn", lambda n: sdn)
    assert placement.bridge_host(net) == expect


# ---- create on several hosts

def multi_create(monkeypatch, empty_pve, sdn=True, room=None):
    """fake_create with a two-node cluster: clones record (name, template, orig), templates_on (tpl, node)."""
    made, checked, copies = [], [], []
    create = fake_create(monkeypatch, made, checked)
    cluster(empty_pve)
    monkeypatch.setattr(placement, "is_sdn", lambda n: sdn)
    monkeypatch.setattr(create, "bridge", lambda n: f"lab{n}" if sdn else f"vmbr{n}")
    monkeypatch.setattr(networks, "bridge", lambda n: f"lab{n}" if sdn else f"vmbr{n}")   # vnet_up's message
    monkeypatch.setattr(create, "is_sdn", lambda n: sdn)
    monkeypatch.setattr(create, "ensure_gw_mac", lambda n: None)
    monkeypatch.setattr(networks.sdn, "wait_vnet", lambda node, n: checked.append(("vnet up", node)) or True)
    monkeypatch.setattr(create, "node_room", lambda: room or {A: {"mem": 10000, "disk": 100}, B: {"mem": 8192, "disk": 100}})
    monkeypatch.setattr(create, "check_resources", lambda n, mem, node=None, *a: checked.append((n, mem, node)) or [])
    monkeypatch.setattr(create, "template_on", lambda t, h, asked=False, **kw: copies.append((t, h)) or (t if h == A else t + 100))
    monkeypatch.setattr(create, "ensure_snippet", lambda node=None: checked.append(("snippet", node)))
    monkeypatch.setattr(create, "copy_needed", lambda t, h, inv=None, gb=None: None)
    monkeypatch.setattr(create, "host_numbers", lambda hosts: {h: (None, (None, None)) for h in hosts})

    def clone(vmid, name, tpl, net, ip, rows, pw, cores, mem, disk, orig=None, board=None):
        rows[vmid] = {"name": name}
        made.append((name, tpl, orig))
    monkeypatch.setattr(create, "clone_vm", clone)
    return create, made, checked, copies


def test_create_spreads_students_and_keeps_each_on_one_host(monkeypatch, empty_pve, capsys):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    create.cmd_create(["2x2", "debian13", "--net", "95", "--memory", "2G"])
    assert made == [("net95-student01-1", 9002, 9002), ("net95-student01-2", 9002, 9002),
                    ("net95-student02-1", 9102, 9002), ("net95-student02-2", 9102, 9002)]    # b: its copy
    assert sorted(checked, key=str) == sorted([(2, 4096, A), (2, 4096, B), ("snippet", B),
                                               ("vnet up", B)], key=str)
    assert sorted(copies) == sorted([(9002, A), (9002, B)])
    assert f"place them: {', '.join(sorted([f'{A} 1 student', f'{B} 1 student']))}" in capsys.readouterr().out


def test_create_node_puts_everyone_there(monkeypatch, empty_pve):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    create.cmd_create(["3", "debian13", "--net", "95", "--node", B])
    assert [m[1] for m in made] == [9102] * 3 and copies == [(9002, B)]


def test_create_existing_student_stays_on_its_host(monkeypatch, empty_pve):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    monkeypatch.setattr(create, "inventory", lambda: {301: {"name": "net95-student01-1", "_node": B}})
    create.cmd_create(["1x2", "debian13", "--net", "95", "--node", A])
    assert made == [("net95-student01-2", 9102, 9002)]


def test_create_vmbr_net_refuses_another_host(monkeypatch, empty_pve):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve, sdn=False)
    with pytest.raises(LabError, match=f"net30 is a bridge vmbr30 on {A} only \\(not an SDN vnet\\)"):
        create.cmd_create(["2", "--net", "30", "--node", B])
    assert made == [] and checked == []


@pytest.mark.parametrize("argv", [["2", "debian13", "--net", "30"], ["2", "debian13"]])      # a vmbrN net, and dhcp (vmbr1)
def test_create_pinned_net_auto_uses_only_its_host(monkeypatch, empty_pve, argv):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve, sdn=False,
                                                 room={A: {"mem": 100, "disk": 1}, B: {"mem": 99999, "disk": 999}})
    monkeypatch.setattr(create, "ensure_dhcp_bridge", lambda: None)
    create.cmd_create(argv)
    assert {m[1] for m in made} == {9002} and {c[1] for c in copies} == {A}


def test_create_stops_when_the_new_sdn_net_fell_back_to_a_bridge(monkeypatch, empty_pve):
    """Placed across hosts as an SDN net, but make_sdn_net failed and ensure_bridge made vmbr95 here only."""
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    monkeypatch.setattr(create, "ensure_bridge", lambda n: monkeypatch.setattr(placement, "is_sdn", lambda n: False))
    with pytest.raises(LabError, match=f"net95 is now a bridge lab95 on {A} only"):
        create.cmd_create(["2", "debian13", "--net", "95", "--node", B])
    assert made == []


def test_create_stops_when_the_vnet_is_not_up_on_a_host(monkeypatch, empty_pve):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    monkeypatch.setattr(networks.sdn, "wait_vnet", lambda node, n: False)
    with pytest.raises(LabError, match=f"lab95 is not up on {B}"):
        create.cmd_create(["2", "debian13", "--net", "95", "--node", B])
    assert made == []


def test_create_node_must_be_a_member(monkeypatch, empty_pve):
    create, made, checked, copies = multi_create(monkeypatch, empty_pve)
    with pytest.raises(LabError, match="--node bb: no such host"):
        create.cmd_create(["2", "--net", "95", "--node", "bb"])


def test_create_one_host_unchanged(monkeypatch):
    made, checked = [], []
    create = fake_create(monkeypatch, made, checked)
    monkeypatch.setattr(create, "node_room", lambda: pytest.fail("placement with one host"))
    create.cmd_create(["2", "debian13", "--net", "95"])
    assert checked == [(2, 4096)] and [m[1] for m in made] == [9002, 9002]


# ---- template copies

def copying(monkeypatch, empty_pve):
    """templates' host side faked; returns the list of commands run (where, argv)."""
    calls = []
    cluster(empty_pve)

    def run(*cmd, **k):
        calls.append(remote(cmd))
        return "  base-9002-disk-0 10.00 20.00\n  vm-9002-cloudinit 0.00 4.00\n" if "lvs" in calls[-1][1] else ""
    monkeypatch.setattr(templates, "run", run)
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {} for v in ids})
    return calls


def test_template_on_original_present(monkeypatch, empty_pve):
    copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    monkeypatch.setattr(templates, "copy_template", lambda o, n: pytest.fail("copied"))
    assert templates.template_on(9002, A) == 9002


def test_template_on_reuses_a_current_copy(monkeypatch, empty_pve):
    copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    vm(empty_pve, B, 9100, tpl(f"copy of 9002 debian-13-cloud {IMG}"))
    monkeypatch.setattr(templates, "copy_template", lambda o, n: pytest.fail("copied"))
    assert templates.template_on(9002, B) == 9100


def test_template_on_replaces_a_stale_copy(monkeypatch, empty_pve):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    vm(empty_pve, B, 9100, tpl("copy of 9002 debian-13-cloud image=0000000000000000"))
    monkeypatch.setattr(templates, "copy_template", lambda o, n, asked=False, **kw: calls.append(("copy", o, n)) or 9101)
    assert templates.template_on(9002, B) == 9101
    assert calls == [("10.0.0.229", ["qm", "destroy", "9100", "--purge", "1"]), ("copy", 9002, B)]


def test_template_on_keeps_a_stale_copy_clones_use(monkeypatch, empty_pve, capsys):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    vm(empty_pve, B, 9100, tpl("copy of 9002 debian-13-cloud image=0000000000000000"))
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {301: "net95-student01"} for v in ids})
    monkeypatch.setattr(templates, "copy_template", lambda o, n, asked=False, **kw: calls.append(("copy", o, n)) or 9101)
    assert templates.template_on(9002, B) == 9101 and calls == [("copy", 9002, B)]
    assert "left in place" in capsys.readouterr().err
    vm(empty_pve, B, 9101, tpl(f"copy of 9002 debian-13-cloud {IMG}"))     # next time: the new copy
    assert templates.template_on(9002, B) == 9101


def test_copy_template(monkeypatch, empty_pve):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    vm(empty_pve, B, 9100, "name: someone\n")           # 9100 is used: the copy gets 9101
    piped, space = [], []

    def pipe(first, second, logfile=None):
        piped.append((remote(first), remote(second)))
        vm(empty_pve, B, 9101, ORIG)                     # qmrestore: a template again, the original's config
    monkeypatch.setattr(templates, "pipe", pipe)
    monkeypatch.setattr(templates, "check_copy_space", lambda gb, node=None: space.append((gb, node)) or [])
    monkeypatch.setattr(templates, "confirm_yes", lambda msg: None)
    assert templates.copy_template(9002, B) == 9101
    assert piped == [(("here", ["vzdump", "9002", "--stdout", "--mode", "stop", "--compress", "0"]),
                      ("10.0.0.229", ["qmrestore", "-", "9101", "--storage", "local-lvm"]))]
    assert space == [(2.0, B)]                            # 20% of 10 GB, checked on b
    assert calls[-1] == ("10.0.0.229", ["qm", "set", "9101", "--name", "debian-13-cloud", "--description",
                                        f"copy of 9002 debian-13-cloud {IMG}"])
    assert not any(c[1][:2] == ["qm", "template"] for c in calls)     # qmrestore made it a template


def test_copy_template_failure_removes_the_half_copy(monkeypatch, empty_pve):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)

    def pipe(first, second, logfile=None):
        vm(empty_pve, B, 9100, "lock: create\n")
        raise KeyboardInterrupt
    monkeypatch.setattr(templates, "pipe", pipe)
    monkeypatch.setattr(templates, "check_copy_space", lambda gb, node=None: None)
    monkeypatch.setattr(templates, "confirm_yes", lambda msg: None)
    with pytest.raises(KeyboardInterrupt):
        templates.copy_template(9002, B)
    assert [c[1][:2] for c in calls[-2:]] == [["qm", "unlock"], ["qm", "destroy"]] and calls[-1][0] == "10.0.0.229"


def test_copy_template_without_yes_copies_nothing(monkeypatch, empty_pve):
    copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9002, ORIG)
    monkeypatch.setattr(util, "YES", False)
    monkeypatch.setattr(templates, "check_copy_space", lambda gb, node=None: None)
    monkeypatch.setattr(templates, "pipe", lambda *a: pytest.fail("copied"))
    with pytest.raises(LabError, match="confirmation needed"):
        templates.copy_template(9002, B)


def test_pipe_runs_both_and_fails_on_either():
    util.pipe(["printf", "x"], ["cat"])
    with pytest.raises(LabError, match="command failed"):
        util.pipe(["printf", "x"], ["false"])


def test_resolve_template_maps_a_copy_to_its_original(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-01", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-01", "docker-lab"))
    assert templates.resolve_template(9101) == 9100
    assert templates.resolve_template("docker-lab") == 9100
    assert templates.node_templates() == {9100: "docker-lab"}         # copies are not listed as templates


def test_templates_by_node(empty_pve):
    cluster(empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-01", "docker-lab"))
    assert templates.templates_by_node() == {"docker-lab": {A: "current", B: "old"}}


def test_delete_template_deletes_unused_copies_and_lists_the_kept(monkeypatch, empty_pve, capsys):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-01", "docker-lab"))
    vm(empty_pve, B, 9102, tpl("copy of 9100 docker-lab built=2026-10-02", "docker-lab"))
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {301: "a", 302: "b"} if v == 9101 else {} for v in ids})
    templates.delete_template("docker-lab")
    assert sorted((w, c[1]) for w, c in calls) == sorted([("here", "destroy"), ("10.0.0.229", "destroy")])
    assert ["9100", "9102"] == sorted(c[2] for _, c in calls)
    assert f"kept copy 9101 on {B}: linked clones still use it (a, b)" in capsys.readouterr().out


def test_delete_template_one_host_unchanged(monkeypatch, empty_pve):
    copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {301: "a"} for v in ids})
    with pytest.raises(LabError, match="1 VM\\(s\\) are linked clones that still need its disks: a"):
        templates.delete_template("docker-lab")


# ---- resources and `templates` on several hosts

def test_storage_and_memory_of_another_node(monkeypatch, empty_pve):
    from pvelab import resources
    cluster(empty_pve)
    G = 2**30
    answers = {"/nodes/b/storage/local-lvm/status": {"total": 100 * G, "used": 85 * G, "avail": 15 * G},
               "/nodes/b/status": {"memory": {"total": 16 * G, "used": 10 * G, "free": 1 * G, "available": 6 * G}}}
    monkeypatch.setattr(resources, "pvesh_json", lambda path: answers.get(path, []))
    assert resources.storage(B) == (85, 15) and resources.host_mem_mb(B) == (16384, 6144)
    with pytest.raises(LabError, match=f"local-lvm on {B}: the copy needs ~20.0 GB, only 15 GB free"):
        resources.check_copy_space(20, B)
    short = resources.check_resources(4, 8192, B, *resources.host_numbers([B])[B])
    assert f"memory on {B}: 4 new VM(s) need 8192 MB, only 6144 MB" in short[0]
    monkeypatch.setattr(resources, "pvesh_json", lambda path: [])            # the API call failed
    assert resources.storage(B) == (None, None) and resources.host_mem_mb(B) == (0, 0)


def test_templates_hosts_column(monkeypatch, empty_pve, capsys):
    from pvelab.commands import templates as cmd
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-01", "docker-lab"))
    cmd.cmd_templates([])
    out = capsys.readouterr().out
    assert "HOSTS" not in out and "Other templates on this node" in out     # one host: as before
    cluster(empty_pve)
    cmd.cmd_templates([])
    out = capsys.readouterr().out.splitlines()
    assert out[1].split()[:4] == ["VMID", "NAME", "ALIASES", "HOSTS"]
    assert f"{', '.join(sorted([A, f'{B} (old)']))}" in next(l for l in out if "docker-lab" in l)


def test_resize_checks_the_memory_of_the_vms_host(monkeypatch, empty_pve, capsys):
    from pvelab import resources
    from pvelab.commands import resize
    cluster(empty_pve)
    vm(empty_pve, B, 301, "name: net95-student01\nmemory: 2048\n")
    monkeypatch.setattr(resize, "targets", lambda a: [301])
    monkeypatch.setattr(resize, "host_mem_mb", lambda node=None: (16384, 3000) if node == B else pytest.fail("here"))
    monkeypatch.setattr(resources, "host_mem_mb", lambda node=None: (16384, 3000) if node == B else pytest.fail("here"))
    monkeypatch.setattr(resize, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    with pytest.raises(LabError, match="not enough memory on a host"):
        resize.cmd_resize(["net95-student01", "--memory", "4G"])
    assert f"memory on {B}: these VMs need 2048 MB, only 3000 MB" in capsys.readouterr().err


def test_delete_template_keeps_the_original_its_clones_use_and_deletes_copies(monkeypatch, empty_pve, capsys):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-02", "docker-lab"))
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {201: "a"} if v == 9100 else {} for v in ids})
    templates.delete_template("docker-lab")
    assert [(w, c[1], c[2]) for w, c in calls] == [("10.0.0.229", "destroy", "9101")]
    out = capsys.readouterr().out
    assert f"kept template 9100 on {A}" in out and f"copy 9101 on {B} deleted" in out and "' (9100) deleted" not in out


def test_delete_template_nothing_deleted_with_copies(monkeypatch, empty_pve):
    calls = copying(monkeypatch, empty_pve)
    vm(empty_pve, A, 9100, tpl("saved by pvelab from x (VMID 201) built=2026-10-02", "docker-lab"))
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-02", "docker-lab"))
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {201: "a"} for v in ids})
    with pytest.raises(LabError, match="nothing deleted:\n  kept template 9100"):
        templates.delete_template("docker-lab")
    assert calls == []


def test_free_template_id_skips_the_source_of_a_kept_copy(empty_pve):
    """The original was deleted, a copy clones use was kept: its source VMID must not name a new template."""
    cluster(empty_pve)
    vm(empty_pve, B, 9101, tpl("copy of 9100 docker-lab built=2026-10-02", "docker-lab"))
    assert templates.free_template_id() == 9102


def test_ensure_snippet_on_another_node(monkeypatch, empty_pve, tmp_path):
    cluster(empty_pve)
    cfg = tmp_path / "storage.cfg"
    cfg.write_text("dir: local\n\tpath /var/lib/vz\n\tcontent iso,vztmpl,backup,snippets\n")
    monkeypatch.setattr(templates, "STORAGE_CFG", str(cfg))
    monkeypatch.setattr(templates, "run", lambda *a, **k: pytest.fail("pvesm runs only when snippets is missing"))
    calls = []
    monkeypatch.setattr(nodes, "run_on", lambda node, *cmd, **kw: calls.append((node, cmd, kw.get("data"))) or "")
    assert templates.ensure_snippet(B) is True
    (node, cat, _), (node2, write, data) = calls
    assert node == node2 == B and cat[0] == "cat" and "cat > /var/lib/vz/snippets/qga-vendor.yaml" in write[-1]
    assert data == templates.vendor_data()
    calls.clear()
    monkeypatch.setattr(nodes, "run_on", lambda node, *cmd, **kw: calls.append(cmd) or templates.vendor_data())
    assert templates.ensure_snippet(B) is False and len(calls) == 1     # already current: not written
