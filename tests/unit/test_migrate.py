"""pvelab migrate: VMs made before the registry get their record, then their configs lose pvelab's line and
tags (qm set only, no reboot); --dry-run only lists; the old files go once no VM needs them."""
import pytest

from pvelab import blocks, credentials, migrate, registry
from pvelab.util import LabError


def vm(name, net="2", extra="", tags="lab-student", ctime=None, node="pve1", desc=None):
    conf = {"name": name, "description": desc if desc is not None else f"managed-by=lab.sh template=9000 net={net}{extra}",
            "tags": tags, "_node": node}
    if ctime:
        conf["smbios1"] = f"uuid={ctime}"
    return conf


@pytest.fixture(autouse=True)
def names(monkeypatch):
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2", "3", "5", "95", "97"})
    monkeypatch.setattr(migrate.nets, "all_nets", lambda: {"2": ("net2", "")})
    monkeypatch.setattr(migrate, "net_arg", lambda v: v[3:] if v.startswith("net") else v)
    monkeypatch.setattr(migrate, "offline_nodes", lambda: [])
    monkeypatch.setattr(migrate, "on_node", lambda node, *a: list(a))
    monkeypatch.setattr(migrate, "confirm_yes", lambda msg: None)
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: f"net{n}")


@pytest.fixture(autouse=True)
def old_files(tmp_path, monkeypatch):
    cred, blk = tmp_path / "credentials.tsv", tmp_path / "blocks.json"
    monkeypatch.setattr(credentials, "CRED", str(cred))
    monkeypatch.setattr(blocks, "BLOCKS_FILE", str(blk))
    return cred, blk


def here(inv):
    """inv's VMs given a row in this host's credentials.tsv (as the pvelab that made them wrote); inv back."""
    with open(credentials.CRED, "a") as f:
        for v, c in sorted(inv.items()):
            f.write(f"{v}\t{c['name']}\tstudent\tpw{v}\t{c.get('net', '2')}\t-\n")
    return inv


def fake(monkeypatch, inv):
    """inventory() is inv; qm set and registry.update recorded in one list, in order."""
    done = []
    monkeypatch.setattr(migrate, "inventory", lambda: inv)
    monkeypatch.setattr(migrate, "run", lambda *a, **k: done.append(("qm", list(a))))
    real = registry.update
    monkeypatch.setattr(migrate.registry, "update", lambda ch, confs, kind="vms": done.append(("registry", ch))
                        or real(ch, confs, kind))
    return done


def test_plan_gives_the_record_and_the_cleaned_config():
    items = migrate.plan(here({201: vm("net2-student03-2", tags="lab-student;pvelab;net-net2;prod",
                                       desc="web server\nmanaged-by=lab.sh template=9000 net=2")}))
    [(vmid, rec, desc, tags)] = items
    assert vmid == 201 and rec["owner"] == "student03" and rec["net"] == "2" and rec["template"] == "9000"
    assert (desc, tags) == ("web server", "prod")


def test_plan_takes_the_legacy_student_token():
    [(_, rec, _, _)] = migrate.plan(here({203: vm("alice-db", extra=" student=net2-alice")}))
    assert rec["owner"] == "alice"


def test_plan_marks_the_old_shared_vm():
    [(_, rec, _, _)] = migrate.plan(here({210: vm("net2-shared")}))
    assert rec["role"] == "shared" and "owner" not in rec


def test_plan_takes_login_and_blocks_from_the_old_files(old_files):
    cred, blk = old_files
    cred.write_text("201\tnet2-u1\tstudent\tpw1\t2\t10.10.2.11\n")
    blk.write_text('{"201": ["ssh"]}')
    [(_, rec, _, _)] = migrate.plan({201: vm("net2-u1", ctime=5)})
    assert (rec["user"], rec["password"], rec["ip"], rec["blocks"]) == ("student", "pw1", "10.10.2.11", ["ssh"])


def test_plan_leaves_vms_whose_login_is_on_another_host():
    """A VM made by pvelab on another host: its row and blocks are in that host's files, not here; a record
    written here would hide them for good."""
    inv = here({202: vm("net2-b")})
    inv |= {201: vm("net5-alice-1", net="5", extra=" owner=alice"), 203: vm("web", net="dhcp", desc="managed-by=lab.sh")}
    assert [i[0] for i in migrate.plan(inv)] == [202]


def test_vms_whose_login_is_not_here_are_listed(monkeypatch, capsys, old_files):
    done = fake(monkeypatch, {201: vm("net5-alice-1", net="5", extra=" owner=alice", ctime=1)})
    migrate.cmd_migrate([])
    out = capsys.readouterr().out
    assert done == [] and "left, its login not in this host's credentials.tsv: net5-alice-1" in out
    assert "pvelab owner VM KEY" in out


def test_plan_leaves_clean_recorded_and_unmanaged_vms():
    new = {"name": "net95-user01", "description": "", "tags": "", "_node": "pve1", "_rec": {"owner": "user01"}}
    mine = {"name": "mine", "description": "x", "tags": "", "_node": "pve1"}
    assert migrate.plan({301: new, 206: mine}) == []


def test_plan_cleans_a_recorded_vm_that_still_has_pvelabs_line():
    """A VM rolled back to a snapshot from before: its record stays as it is, its config is cleaned."""
    rolled = dict(vm("net2-u1", extra=" owner=x", tags="pvelab;net-net2"), _rec={"owner": "u1", "net": "2"})
    [(vmid, rec, desc, tags)] = migrate.plan({216: rolled})
    assert rec == {"owner": "u1", "net": "2"} and (desc, tags) == ("", "")


def test_plan_net_limits_it_to_that_net():
    inv = here({201: vm("net2-a"), 202: vm("net97-b", net="97")})
    assert [i[0] for i in migrate.plan(inv, net="97")] == [202]


def test_plan_leaves_vms_of_a_net_not_known_here(monkeypatch):
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    assert [i[0] for i in migrate.plan(here({201: vm("web-student01", net="5"), 202: vm("net2-student01")}))] == [202]


def test_dry_run_lists_and_changes_nothing(monkeypatch, capsys, old_files):
    old_files[0].write_text("204\tweb01\tstudent\tpw\t2\t10.10.2.14\n")
    done = fake(monkeypatch, here({201: vm("net2-student01", ctime=1, tags="lab-student;prod",
                                           desc="notes\nmanaged-by=lab.sh net=2"),
                                   204: vm("web01", ctime=2)}))
    migrate.cmd_migrate([], dry=True)
    out = capsys.readouterr().out
    assert done == [] and not __import__("os").path.exists(registry.REGISTRY)
    assert "net2-student01: owner student01" in out and "pvelab's line removed, notes kept" in out
    assert "tags: lab-student removed, prod kept" in out
    assert "web01: owner web01, login student" in out and "name alone: web01" in out
    assert "dry run" in out and old_files[0].exists()


def test_real_run_writes_the_registry_before_any_qm_set(monkeypatch, capsys, old_files):
    done = fake(monkeypatch, here({201: vm("net2-student01", ctime=1), 202: vm("net2-student02", ctime=2)}))
    migrate.cmd_migrate([])
    assert done[0][0] == "registry" and set(done[0][1]) == {201, 202}
    assert sorted(a[2] for kind, a in done[1:] if kind == "qm") == [201, 202]
    assert registry.load()["vms"]["201"]["owner"] == "student01"


def test_empty_notes_and_tags_are_deleted(monkeypatch, old_files):
    done = fake(monkeypatch, here({201: vm("net2-student01", ctime=1, tags="pvelab;net-net2")}))
    migrate.cmd_migrate([])
    [qm] = [a for kind, a in done if kind == "qm"]
    assert qm == ["qm", "set", 201, "--delete", "description,tags"]


def test_admin_notes_and_tag_kept(monkeypatch, old_files):
    done = fake(monkeypatch, here({201: vm("net2-student01", ctime=1, tags="pvelab;prod",
                                           desc="web\nmanaged-by=lab.sh net=2")}))
    migrate.cmd_migrate([])
    [qm] = [a for kind, a in done if kind == "qm"]
    assert qm == ["qm", "set", 201, "--description", "web", "--tags", "prod"]


def test_net_leaves_other_nets_alone(monkeypatch, old_files):
    done = fake(monkeypatch, here({201: vm("net2-a", ctime=1), 202: vm("net97-b", net="97", ctime=2)}))
    migrate.cmd_migrate(["--net", "net97"])
    assert [a[2] for kind, a in done if kind == "qm"] == [202]
    assert list(registry.load()["vms"]) == ["202"]


def test_old_files_renamed_only_when_nothing_is_left(monkeypatch, capsys, old_files):
    cred, blk = old_files
    cred.write_text("201\tnet2-a\tstudent\tpw\t2\t10.10.2.11\n")
    blk.write_text("{}")
    fake(monkeypatch, {201: vm("net2-a", ctime=1), 202: vm("net97-b", net="97", ctime=2)})
    migrate.cmd_migrate(["--net", "97"])
    assert cred.exists() and blk.exists()                  # net2's VM still reads them
    fake(monkeypatch, {201: vm("net2-a", ctime=1)})
    migrate.cmd_migrate([])
    assert not cred.exists() and not blk.exists()
    left = sorted(p.name for p in cred.parent.iterdir())
    assert any(n.startswith("credentials.tsv.migrated-") for n in left)
    assert any(n.startswith("blocks.json.migrated-") for n in left)
    assert "credentials.tsv.migrated-" in capsys.readouterr().out


def test_old_files_kept_when_a_qm_set_failed(monkeypatch, capsys, old_files):
    inv = here({201: vm("net2-a", ctime=1)})
    monkeypatch.setattr(migrate, "inventory", lambda: inv)
    monkeypatch.setattr(migrate, "run", lambda *a, **k: (_ for _ in ()).throw(LabError("read-only")))
    migrate.cmd_migrate([])
    err = capsys.readouterr().err
    assert "net2-a" in err and "pvelab migrate" in err and old_files[0].exists()
    assert registry.load()["vms"]["201"]["owner"] == "a"       # the record is written: it wins


def test_vms_on_an_offline_host_are_left(monkeypatch, capsys, old_files):
    monkeypatch.setattr(migrate, "offline_nodes", lambda: ["pve2"])
    done = fake(monkeypatch, {201: vm("net2-a", ctime=1, node="pve2")})
    migrate.cmd_migrate([])
    assert done == [] and "host offline" in capsys.readouterr().out


def test_nothing_to_do(monkeypatch, capsys, old_files):
    fake(monkeypatch, {})
    migrate.cmd_migrate([], dry=True)
    assert "no VM keeps pvelab's facts" in capsys.readouterr().out


def test_hint_counts_vms_not_migrated(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "inventory", lambda: {201: vm("a"), 202: vm("b"),
                                                        203: {"name": "c", "_rec": {}, "description": ""}})
    migrate.hint()
    out = capsys.readouterr().out
    assert "2 VM(s) still keep pvelab's facts" in out and "pvelab migrate --dry-run" in out
    monkeypatch.setattr(migrate, "inventory", lambda: {203: {"name": "c", "_rec": {}, "description": ""}})
    migrate.hint()
    assert capsys.readouterr().out == ""


def test_hint_is_quiet_when_it_cannot_read(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "inventory", lambda: (_ for _ in ()).throw(LabError("no quorum")))
    migrate.hint()
    assert capsys.readouterr().out == "" and capsys.readouterr().err == ""


def test_migrate_takes_no_other_arguments():
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError):
        migrate.cmd_migrate(["web01"])


def test_owner_check_points_to_migrate():
    from pvelab.commands import owner
    from pvelab.helptext import UsageError
    with pytest.raises(UsageError, match="pvelab migrate --dry-run"):
        owner.cmd_owner(["--check"])


def test_owner_command_writes_the_record(monkeypatch):
    from pvelab.commands import owner
    ran = []
    monkeypatch.setattr(owner, "targets", lambda a: [204])
    monkeypatch.setattr(owner, "read_conf", lambda i: vm("web01"))
    monkeypatch.setattr(owner.legacy, "write", lambda f: ran.append(f))
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None, changed=None: None)
    owner.cmd_owner(["web01", "alice"])
    assert ran == [{204: {"owner": "alice", "role": None}}]           # its record, not its description


def test_owner_warns_when_an_owner_spans_hosts(monkeypatch, capsys):
    from pvelab.commands import owner
    monkeypatch.setattr(owner, "targets", lambda a: [204])
    monkeypatch.setattr(owner, "read_conf", lambda i: vm("web01"))
    monkeypatch.setattr(owner.legacy, "write", lambda f: None)
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None, changed=None: None)
    monkeypatch.setattr(owner, "inventory", lambda: {
        204: {**vm("web01", extra=" owner=alice"), "_node": "pve1"},
        205: {**vm("net2-alice-db", extra=" owner=alice"), "_node": "pve2"}})
    owner.cmd_owner(["web01", "alice"])
    assert "alice's VMs on net2 are on 2 hosts" in capsys.readouterr().err


# ---- pvelab migrate: templates too

def tpl(desc, name="debian-13-cloud", ctime=1):
    return {"template": "1", "name": name, "description": desc, "smbios1": f"uuid={ctime}", "_node": "pve1"}


def test_migrate_moves_a_legacy_templates_facts(monkeypatch, capsys):
    inv = {9002: tpl("built by pvelab from https://x/d.qcow2 image=abcdef0123456789 built=2026-10-01"),
           9100: tpl("saved by pvelab from x (VMID 201) built=2026-10-02\nkeep this", "docker-lab", 2),
           9101: tpl("copy of 9100 docker-lab built=2026-10-02", "docker-lab", 3),
           9200: tpl("an admin's own template", "mine", 4)}
    done = fake(monkeypatch, inv)
    migrate.cmd_migrate([])
    out = capsys.readouterr().out
    assert "template docker-lab (9100): pvelab's text removed, notes kept" in out
    recs = registry.load()["templates"]
    assert recs["9002"] == {"uuid": "1", "kind": "built", "image": "abcdef0123456789", "built": "2026-10-01"}
    assert recs["9100"] == {"uuid": "2", "kind": "saved", "from": "x (VMID 201)", "built": "2026-10-02"}
    assert recs["9101"] == {"uuid": "3", "kind": "copy", "orig": 9100, "stamp": "built=2026-10-02"}
    assert "9200" not in recs
    qm = sorted(a for kind, a in done if kind == "qm")
    assert qm == [["qm", "set", 9002, "--delete", "description"],
                  ["qm", "set", 9100, "--description", "keep this"],
                  ["qm", "set", 9101, "--delete", "description"]]
    assert done[0][0] == "registry"                        # the record before its description goes


def test_migrate_dry_run_and_net_leave_templates(monkeypatch, capsys):
    done = fake(monkeypatch, {9002: tpl("built by pvelab from https://x/d.qcow2 built=2026-10-01")})
    migrate.cmd_migrate([], dry=True)
    assert "template debian-13-cloud (9002)" in capsys.readouterr().out and done == []
    migrate.cmd_migrate(["--net", "2"])
    assert done == [] and "template" not in capsys.readouterr().out


@pytest.mark.parametrize("desc", ["", "an admin's own template"])
def test_migrate_leaves_templates_without_pvelabs_text(monkeypatch, desc):
    assert migrate.template_plan({9200: tpl(desc)}) == []
