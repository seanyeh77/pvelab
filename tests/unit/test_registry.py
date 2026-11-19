"""The registry: pvelab's facts about its VMs in one cluster-wide file, matched by VMID + ctime."""
import os

import pytest

from pvelab import registry
from pvelab.util import LabError


@pytest.fixture
def reg():
    return __import__("pathlib").Path(registry.REGISTRY)


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
