"""The registry: pvelab's facts about its VMs in one cluster-wide file, matched by VMID + ctime."""
import os

import pytest

from pvelab import blocks, credentials, inventory, legacy, owners, registry
from pvelab.blocks import blocks_load
from pvelab.credentials import cred_rows
from pvelab.inventory import is_managed, read_conf, vm_value
from pvelab.util import LabError


@pytest.fixture
def reg():
    return __import__("pathlib").Path(registry.REGISTRY)


@pytest.fixture
def conf_dir():
    d = __import__("pathlib").Path(inventory.CONF_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    f = tmp_path / "credentials.tsv"
    monkeypatch.setattr(credentials, "CRED", str(f))
    return f


@pytest.fixture
def blocks_file(tmp_path, monkeypatch):
    f = tmp_path / "blocks.json"
    monkeypatch.setattr(blocks, "BLOCKS_FILE", str(f))
    return f


def write_conf(conf_dir, vmid, text):
    (conf_dir / f"{vmid}.conf").write_text(text + "\n")


def test_uuid_of_reads_the_smbios1_line():
    assert registry.uuid_of({"smbios1": "uuid=2F1A-77,manufacturer=x"}) == "2f1a-77"
    assert registry.uuid_of({"meta": "creation-qemu=11.0.3,ctime=1790432982"}) is None


def test_lookup_matches_vmid_and_ctime(reg):
    conf = {"smbios1": "uuid=100"}
    registry.update({216: {"owner": "user01"}}, {216: conf})
    assert registry.lookup(216, conf)["owner"] == "user01"
    assert registry.lookup(216, {"smbios1": "uuid=200"}) is None   # VMID reused


def test_update_merges_and_none_removes(reg):
    conf = {"smbios1": "uuid=1"}
    registry.update({5: {"owner": "a", "password": "p"}}, {5: conf})
    registry.update({5: {"owner": None, "role": "shared"}}, {5: conf})
    assert registry.lookup(5, conf) == {"uuid": "1", "password": "p", "role": "shared"}


def test_update_none_forgets_the_vm(reg):
    conf = {"smbios1": "uuid=1"}
    registry.update({5: {"owner": "a"}}, {5: conf})
    registry.forget([5])
    assert registry.lookup(5, conf) is None


def test_update_prunes_records_of_gone_or_reused_vms(reg):
    a, b = {"smbios1": "uuid=1"}, {"smbios1": "uuid=2"}
    registry.update({1: {"owner": "a"}, 2: {"owner": "b"}}, {1: a, 2: b})
    registry.update({1: {"owner": "c"}}, {1: a, 2: {"smbios1": "uuid=9"}})       # 2 reused
    assert set(registry.load()["vms"]) == {"1"}


def test_bad_json_stops_without_overwriting(reg):
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("{not json")
    with pytest.raises(LabError, match="vms.json"):
        registry.load()
    with pytest.raises(LabError):
        registry.update({1: {"owner": "a"}}, {1: {"smbios1": "uuid=1"}})
    assert reg.read_text() == "{not json"


def test_file_is_0600_and_lock_released(reg):
    registry.update({1: {"owner": "a"}}, {1: {"smbios1": "uuid=1"}})
    assert oct(reg.stat().st_mode & 0o777) == "0o600"
    assert not os.path.exists(registry.REGISTRY_LOCK)


def test_locked_waits_then_fails(reg, monkeypatch):
    os.makedirs(registry.REGISTRY_LOCK)
    monkeypatch.setattr(registry, "LOCK_WAIT", 0.2)
    with pytest.raises(LabError, match="locked by another pvelab run"):
        registry.update({1: {"owner": "a"}}, {1: {"smbios1": "uuid=1"}})
    assert os.path.isdir(registry.REGISTRY_LOCK)             # someone else's: left alone


def test_templates_are_kept_apart(reg):
    conf = {"smbios1": "uuid=3"}
    registry.update({9002: {"kind": "built"}}, {9002: conf}, kind="templates")
    assert registry.lookup(9002, conf) is None
    assert registry.lookup(9002, conf, kind="templates")["kind"] == "built"


def test_record_wins_over_the_description(reg, conf_dir):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 owner=old\nsmbios1: uuid=7\nname: x\ntags: pvelab")
    registry.update({216: {"net": "3", "owner": "new"}}, {216: {"smbios1": "uuid=7"}})
    c = read_conf(216)
    assert (vm_value(c, "owner"), vm_value(c, "net"), is_managed(c)) == ("new", "3", True)
    assert c["_vmid"] == 216


def test_record_without_the_key_reads_empty(reg, conf_dir):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 prefix=old\nsmbios1: uuid=7\nname: x\ntags: pvelab")
    registry.update({216: {"net": "3"}}, {216: {"smbios1": "uuid=7"}})
    assert vm_value(read_conf(216), "prefix") == ""


def test_clean_vm_with_a_record_is_managed(reg, conf_dir):
    write_conf(conf_dir, 217, "smbios1: uuid=8\nname: y")
    registry.update({217: {"net": "2", "owner": "u"}}, {217: {"smbios1": "uuid=8"}})
    assert is_managed(read_conf(217)) and owners.owner_of(read_conf(217)) == "u"


def test_reused_vmid_is_not_managed(reg, conf_dir):
    registry.update({218: {"net": "2"}}, {218: {"smbios1": "uuid=1"}})
    write_conf(conf_dir, 218, "smbios1: uuid=2\nname: someone-elses")
    assert not is_managed(read_conf(218))


def test_cred_rows_prefer_the_record(reg, conf_dir, cred_file):
    cred_file.write_text("216\tx\tstudent\toldpw\t2\t10.10.2.11\n219\tz\tuser\tpw9\t2\t10.10.2.14\n")
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "newpw", "net": "2", "ip": "10.10.2.11"}},
                    {216: {"smbios1": "uuid=7"}})
    rows = cred_rows()
    assert rows[216]["password"] == "newpw" and rows[219]["password"] == "pw9"
    assert rows[216]["user"] == "user"


def test_cred_rows_skip_a_record_of_a_reused_vmid(reg, conf_dir, cred_file):
    cred_file.write_text("")
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "pw"}}, {216: {"smbios1": "uuid=7"}})
    write_conf(conf_dir, 216, "smbios1: uuid=8\nname: other")
    assert 216 not in cred_rows()


def test_cred_rows_defaults_of_a_record(reg, conf_dir, cred_file):
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "pw"}}, {216: {"smbios1": "uuid=7"}})
    assert cred_rows()[216] == {"name": "x", "user": "user", "password": "pw", "net": "dhcp", "ip": "-"}


def test_blocks_load_prefers_the_record(reg, conf_dir, blocks_file):
    blocks_file.write_text('{"216": ["ssh"], "219": ["internet"]}')
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"blocks": ["internet"]}}, {216: {"smbios1": "uuid=7"}})
    assert blocks_load() == {216: {"internet"}, 219: {"internet"}}


def test_blocks_load_record_without_blocks_has_none(reg, conf_dir, blocks_file):
    blocks_file.write_text('{"216": ["ssh"]}')
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"net": "2"}}, {216: {"smbios1": "uuid=7"}})
    assert blocks_load() == {}


def test_legacy_record_from_the_old_places(reg, conf_dir):
    write_conf(conf_dir, 220, "#managed-by=lab.sh net=2 template=9002 prefix=net2-student\n"
                              "smbios1: uuid=9\nname: net2-student03\ntags: pvelab")
    row = {"name": "net2-student03", "user": "student", "password": "pw", "net": "2", "ip": "10.10.2.13"}
    rec = legacy.legacy_record(220, read_conf(220), row, {220: {"ssh"}})
    assert rec == {"net": "2", "template": "9002", "prefix": "net2-student", "owner": "student03",
                   "name": "net2-student03", "user": "student", "password": "pw", "ip": "10.10.2.13",
                   "blocks": ["ssh"]}


def test_legacy_record_of_a_shared_vm(reg, conf_dir):
    write_conf(conf_dir, 221, "#managed-by=lab.sh net=2 role=shared\nsmbios1: uuid=9\nname: net2-shared\n"
                              "tags: pvelab")
    rec = legacy.legacy_record(221, read_conf(221), None, {})
    assert rec == {"net": "2", "role": "shared"}


# Task 3: writers write the registry only

def run_clone_vm(monkeypatch, conf_dir, desc="", **kw):
    """clone_vm with a fake qm (qm clone writes the VM's config, with the template's description desc);
    the argv of every command run."""
    from pvelab import lifecycle
    calls = []

    def run(*a, **k):
        calls.append([str(x) for x in a])
        if a[:2] == ("qm", "clone"):
            write_conf(conf_dir, 301, (f"#{desc}\n" if desc else "") + "smbios1: uuid=500\n"
                       "name: net95-user01")
        return ""
    monkeypatch.setattr(lifecycle, "run", run)
    monkeypatch.setattr(lifecycle, "on_node", lambda node, *a: a)
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: f"net{n}")
    for f, v in (("node_of", "pve1"), ("host_dns", "1.1.1.1"), ("ssh_keys", ""), ("ip_config", "ip=dhcp"),
                 ("bridge", "lab95")):
        monkeypatch.setattr(lifecycle, f, lambda *a, v=v: v)
    rows = {}
    lifecycle.clone_vm(301, "net95-user01", 9000, "95", "10.0.0.5", rows, "pw", 1, 512, 0, **kw)
    return calls


def test_clone_writes_no_description_or_tags(monkeypatch, reg, conf_dir, cred_file):
    calls = run_clone_vm(monkeypatch, conf_dir, owner="user01", prefix="user")
    sets = [a for a in calls if a[:2] == ["qm", "set"]]
    assert sets and not any("--tags" in a or "--description" in a for a in sets)
    rec = registry.load()["vms"]["301"]
    assert rec == {"uuid": "500", "name": "net95-user01", "net": "95", "ip": "10.0.0.5", "user": "student",
                   "password": "pw", "template": "9000", "owner": "user01", "prefix": "user"}
    assert is_managed(read_conf(301)) and not cred_file.exists()


def test_clone_clears_the_description_copied_from_its_template(monkeypatch, reg, conf_dir, cred_file):
    calls = run_clone_vm(monkeypatch, conf_dir, desc="built by pvelab from debian/13 image=ab12 built=2026-11-01",
                         owner="user01")
    sets = [a for a in calls if a[:2] == ["qm", "set"]]
    assert any(a[a.index("--delete") + 1] == "description" for a in sets if "--delete" in a)


def test_clone_of_a_shared_vm_records_its_role(monkeypatch, reg, conf_dir, cred_file):
    run_clone_vm(monkeypatch, conf_dir, shared=True)
    rec = registry.load()["vms"]["301"]
    assert rec["role"] == "shared" and "owner" not in rec


def test_first_write_of_an_unmigrated_vm_keeps_its_legacy_facts(reg, conf_dir, cred_file, blocks_file):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 owner=u1\nsmbios1: uuid=7\nname: net2-u1\ntags: pvelab;net-net2")
    cred_file.write_text("216\tnet2-u1\tstudent\tpw\t2\t10.10.2.11\n")
    blocks.blocks_save({216: {"ssh"}})
    rec = registry.lookup(216, read_conf(216))
    assert (rec["owner"], rec["password"], rec["blocks"]) == ("u1", "pw", ["ssh"])
    assert not blocks_file.exists()                          # the old file is not written


def test_cred_save_records_only_the_rows_changed(reg, conf_dir, cred_file):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 owner=u1\nsmbios1: uuid=7\nname: net2-u1\ntags: pvelab")
    write_conf(conf_dir, 217, "#managed-by=lab.sh net=2 owner=u2\nsmbios1: uuid=8\nname: net2-u2\ntags: pvelab")
    old = "216\tnet2-u1\tstudent\tpw\t2\t10.10.2.11\n217\tnet2-u2\tstudent\tpw2\t2\t10.10.2.12\n"
    cred_file.write_text(old)
    rows = cred_rows()
    rows[216]["password"] = "new"
    credentials.cred_save(rows)
    assert set(registry.load()["vms"]) == {"216"}            # 217 stays as it was, not migrated
    assert cred_rows()[216]["password"] == "new" and cred_rows()[217]["password"] == "pw2"
    assert registry.load()["vms"]["216"]["owner"] == "u1"
    assert cred_file.read_text() == old


def test_unblock_of_a_recorded_vm_drops_its_blocks(reg, conf_dir, blocks_file):
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"net": "2", "blocks": ["ssh", "internet"]}}, {216: {"smbios1": "uuid=7"}})
    blocks.blocks_save({216: set()})
    assert "blocks" not in registry.load()["vms"]["216"] and blocks_load() == {}


def test_strip_legacy_keeps_admin_notes_and_tags():
    d, t = legacy.strip_legacy("web server\nmanaged-by=lab.sh net=2 owner=a", "pvelab;net-net2;prod;lab-student")
    assert (d, t) == ("web server", "prod")
    assert legacy.strip_legacy("managed-by=lab.sh net=2\nnote\nstudent=net2-student03", "pvelab") == ("note", "")
    assert legacy.strip_legacy("", "") == ("", "")


def test_remove_vm_forgets_the_record(monkeypatch, reg, conf_dir, cred_file, blocks_file):
    from pvelab import lifecycle
    write_conf(conf_dir, 216, "smbios1: uuid=7\nname: x")
    registry.update({216: {"net": "2", "owner": "u"}}, {216: {"smbios1": "uuid=7"}})
    cred_file.write_text("216\tx\tstudent\tpw\t2\t10.10.2.11\n219\tz\tuser\tpw9\t2\t10.10.2.14\n")
    blocks_file.write_text('{"216": ["ssh"]}')

    def run(*a, **k):
        if "destroy" in a:
            os.remove(conf_dir / "216.conf")
        return ""
    monkeypatch.setattr(lifecycle, "run", run)
    monkeypatch.setattr(lifecycle, "vm_status", lambda v: "stopped")
    lifecycle.remove_vm(216)
    assert "216" not in registry.load()["vms"]
    assert "216\t" not in cred_file.read_text() and "219\t" in cred_file.read_text()
    assert "216" not in blocks_file.read_text()


def test_owner_writes_the_record_not_the_description(monkeypatch, reg, conf_dir, cred_file):
    from pvelab.commands import owner
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 owner=u1\nsmbios1: uuid=7\nname: net2-u1\ntags: pvelab")
    cred_file.write_text("216\tnet2-u1\tstudent\tpw\t2\t10.10.2.11\n")
    ran = []
    monkeypatch.setattr(owner, "targets", lambda a: [216])
    monkeypatch.setattr(owner.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(owner.nets, "all_nets", lambda: {})
    monkeypatch.setattr(owner.nets, "known_nets", lambda found: {"2"})
    monkeypatch.setattr(owner.nets, "known_here", lambda n, known: True)
    monkeypatch.setattr(owner.nets, "pvelab_names", lambda found: set())
    monkeypatch.setattr(owner, "run", lambda *a, **k: ran.append(a), raising=False)
    monkeypatch.setattr(owner, "refresh_isolation", lambda *a, **k: None)
    monkeypatch.setattr(owner, "warn_layout", lambda *a, **k: None)
    owner.cmd_owner(["net2-u1", "alice"])
    assert ran == []
    rec = registry.lookup(216, read_conf(216))
    assert (rec["owner"], rec["password"]) == ("alice", "pw")


def test_rollback_strips_what_the_snapshot_brought_back(monkeypatch, reg, conf_dir):
    from pvelab.commands import snapshots
    write_conf(conf_dir, 216, "#admin note\n#managed-by=lab.sh net=2 owner=a\nsmbios1: uuid=7\nname: x\n"
                              "tags: pvelab;net-net2;prod\n[before]\nname: x")
    write_conf(conf_dir, 217, "#managed-by=lab.sh net=2 owner=b\nsmbios1: uuid=8\nname: y\ntags: pvelab\n"
                              "[before]\nname: y")
    registry.update({216: {"net": "2", "owner": "a"}}, {216: {"smbios1": "uuid=7"}})
    ran = []
    monkeypatch.setattr(snapshots, "run", lambda *a, **k: ran.append([str(x) for x in a]) or "")
    monkeypatch.setattr(snapshots, "qm_argv", lambda vmid, *a: ["qm", *a])
    monkeypatch.setattr(snapshots, "confirm_yes", lambda q: None)
    monkeypatch.setattr(snapshots, "targets", lambda a: [216, 217])
    snapshots.cmd_rollback(["216", "217", "--to", "before"])
    sets = [a for a in ran if a[:2] == ["qm", "set"]]
    assert sets == [["qm", "set", "216", "--description", "admin note", "--tags", "prod"]]   # 217: not migrated


def test_a_clone_of_the_same_template_is_another_vm(reg):
    """qm clone copies meta (ctime) from the template but gives each clone its own smbios1 uuid."""
    old = {"meta": "creation-qemu=11.0.3,ctime=100", "smbios1": "uuid=aaaa-1111"}
    new = {"meta": "creation-qemu=11.0.3,ctime=100", "smbios1": "uuid=bbbb-2222"}   # VMID reused
    registry.update({216: {"owner": "u1", "blocks": ["ssh"]}}, {216: old})
    assert registry.lookup(216, old)["owner"] == "u1"
    assert registry.lookup(216, new) is None
    registry.update({216: {"owner": "u2"}}, {216: new})
    assert registry.lookup(216, new) == {"uuid": "bbbb-2222", "owner": "u2"}       # nothing of the old one


def test_first_write_of_a_vm_made_on_another_host_stops(reg, conf_dir, cred_file, blocks_file, monkeypatch):
    """Its login and blocks are in the other host's credentials.tsv: a record without them would hide them."""
    write_conf(conf_dir, 230, "#managed-by=lab.sh net=2 owner=u1\nsmbios1: uuid=230\nname: net2-u1\ntags: pvelab")
    cred_file.write_text("")
    with pytest.raises(LabError, match="pvelab migrate on that host"):
        legacy.write({230: {"owner": "alice"}})
    assert "230" not in registry.load()["vms"]
    monkeypatch.setattr(legacy, "FORCE", True)                     # LAB_FORCE=1: no host has it any more
    legacy.write({230: {"owner": "alice"}})
    assert registry.load()["vms"]["230"]["owner"] == "alice"
