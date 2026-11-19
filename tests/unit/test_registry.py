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


def test_ctime_of_reads_the_meta_line():
    assert registry.ctime_of({"meta": "creation-qemu=11.0.3,ctime=1790432982"}) == 1790432982
    assert registry.ctime_of({}) is None


def test_lookup_matches_vmid_and_ctime(reg):
    conf = {"meta": "creation-qemu=11.0.3,ctime=100"}
    registry.update({216: {"owner": "user01"}}, {216: conf})
    assert registry.lookup(216, conf)["owner"] == "user01"
    assert registry.lookup(216, {"meta": "creation-qemu=11.0.3,ctime=200"}) is None   # VMID reused


def test_update_merges_and_none_removes(reg):
    conf = {"meta": "ctime=1"}
    registry.update({5: {"owner": "a", "password": "p"}}, {5: conf})
    registry.update({5: {"owner": None, "role": "shared"}}, {5: conf})
    assert registry.lookup(5, conf) == {"ctime": 1, "password": "p", "role": "shared"}


def test_update_none_forgets_the_vm(reg):
    conf = {"meta": "ctime=1"}
    registry.update({5: {"owner": "a"}}, {5: conf})
    registry.forget([5])
    assert registry.lookup(5, conf) is None


def test_update_prunes_records_of_gone_or_reused_vms(reg):
    a, b = {"meta": "ctime=1"}, {"meta": "ctime=2"}
    registry.update({1: {"owner": "a"}, 2: {"owner": "b"}}, {1: a, 2: b})
    registry.update({1: {"owner": "c"}}, {1: a, 2: {"meta": "ctime=9"}})       # 2 reused
    assert set(registry.load()["vms"]) == {"1"}


def test_bad_json_stops_without_overwriting(reg):
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("{not json")
    with pytest.raises(LabError, match="vms.json"):
        registry.load()
    with pytest.raises(LabError):
        registry.update({1: {"owner": "a"}}, {1: {"meta": "ctime=1"}})
    assert reg.read_text() == "{not json"


def test_file_is_0600_and_lock_released(reg):
    registry.update({1: {"owner": "a"}}, {1: {"meta": "ctime=1"}})
    assert oct(reg.stat().st_mode & 0o777) == "0o600"
    assert not os.path.exists(registry.REGISTRY_LOCK)


def test_locked_waits_then_fails(reg, monkeypatch):
    os.makedirs(registry.REGISTRY_LOCK)
    monkeypatch.setattr(registry, "LOCK_WAIT", 0.2)
    with pytest.raises(LabError, match="locked by another pvelab run"):
        registry.update({1: {"owner": "a"}}, {1: {"meta": "ctime=1"}})
    assert os.path.isdir(registry.REGISTRY_LOCK)             # someone else's: left alone


def test_templates_are_kept_apart(reg):
    conf = {"meta": "ctime=3"}
    registry.update({9002: {"kind": "built"}}, {9002: conf}, kind="templates")
    assert registry.lookup(9002, conf) is None
    assert registry.lookup(9002, conf, kind="templates")["kind"] == "built"


def test_record_wins_over_the_description(reg, conf_dir):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 owner=old\nmeta: ctime=7\nname: x\ntags: pvelab")
    registry.update({216: {"net": "3", "owner": "new"}}, {216: {"meta": "ctime=7"}})
    c = read_conf(216)
    assert (vm_value(c, "owner"), vm_value(c, "net"), is_managed(c)) == ("new", "3", True)
    assert c["_vmid"] == 216


def test_record_without_the_key_reads_empty(reg, conf_dir):
    write_conf(conf_dir, 216, "#managed-by=lab.sh net=2 prefix=old\nmeta: ctime=7\nname: x\ntags: pvelab")
    registry.update({216: {"net": "3"}}, {216: {"meta": "ctime=7"}})
    assert vm_value(read_conf(216), "prefix") == ""


def test_clean_vm_with_a_record_is_managed(reg, conf_dir):
    write_conf(conf_dir, 217, "meta: ctime=8\nname: y")
    registry.update({217: {"net": "2", "owner": "u"}}, {217: {"meta": "ctime=8"}})
    assert is_managed(read_conf(217)) and owners.owner_of(read_conf(217)) == "u"


def test_reused_vmid_is_not_managed(reg, conf_dir):
    registry.update({218: {"net": "2"}}, {218: {"meta": "ctime=1"}})
    write_conf(conf_dir, 218, "meta: ctime=2\nname: someone-elses")
    assert not is_managed(read_conf(218))


def test_cred_rows_prefer_the_record(reg, conf_dir, cred_file):
    cred_file.write_text("216\tx\tstudent\toldpw\t2\t10.10.2.11\n219\tz\tuser\tpw9\t2\t10.10.2.14\n")
    write_conf(conf_dir, 216, "meta: ctime=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "newpw", "net": "2", "ip": "10.10.2.11"}},
                    {216: {"meta": "ctime=7"}})
    rows = cred_rows()
    assert rows[216]["password"] == "newpw" and rows[219]["password"] == "pw9"
    assert rows[216]["user"] == "user"


def test_cred_rows_skip_a_record_of_a_reused_vmid(reg, conf_dir, cred_file):
    cred_file.write_text("")
    write_conf(conf_dir, 216, "meta: ctime=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "pw"}}, {216: {"meta": "ctime=7"}})
    write_conf(conf_dir, 216, "meta: ctime=8\nname: other")
    assert 216 not in cred_rows()


def test_cred_rows_defaults_of_a_record(reg, conf_dir, cred_file):
    write_conf(conf_dir, 216, "meta: ctime=7\nname: x")
    registry.update({216: {"name": "x", "user": "user", "password": "pw"}}, {216: {"meta": "ctime=7"}})
    assert cred_rows()[216] == {"name": "x", "user": "user", "password": "pw", "net": "dhcp", "ip": "-"}


def test_blocks_load_prefers_the_record(reg, conf_dir, blocks_file):
    blocks_file.write_text('{"216": ["ssh"], "219": ["internet"]}')
    write_conf(conf_dir, 216, "meta: ctime=7\nname: x")
    registry.update({216: {"blocks": ["internet"]}}, {216: {"meta": "ctime=7"}})
    assert blocks_load() == {216: {"internet"}, 219: {"internet"}}


def test_blocks_load_record_without_blocks_has_none(reg, conf_dir, blocks_file):
    blocks_file.write_text('{"216": ["ssh"]}')
    write_conf(conf_dir, 216, "meta: ctime=7\nname: x")
    registry.update({216: {"net": "2"}}, {216: {"meta": "ctime=7"}})
    assert blocks_load() == {}


def test_legacy_record_from_the_old_places(reg, conf_dir):
    write_conf(conf_dir, 220, "#managed-by=lab.sh net=2 template=9002 prefix=net2-student\n"
                              "meta: ctime=9\nname: net2-student03\ntags: pvelab")
    row = {"name": "net2-student03", "user": "student", "password": "pw", "net": "2", "ip": "10.10.2.13"}
    rec = legacy.legacy_record(220, read_conf(220), row, {220: {"ssh"}})
    assert rec == {"net": "2", "template": "9002", "prefix": "net2-student", "owner": "student03",
                   "name": "net2-student03", "user": "student", "password": "pw", "ip": "10.10.2.13",
                   "blocks": ["ssh"]}


def test_legacy_record_of_a_shared_vm(reg, conf_dir):
    write_conf(conf_dir, 221, "#managed-by=lab.sh net=2 role=shared\nmeta: ctime=9\nname: net2-shared\n"
                              "tags: pvelab")
    rec = legacy.legacy_record(221, read_conf(221), None, {})
    assert rec == {"net": "2", "role": "shared"}
