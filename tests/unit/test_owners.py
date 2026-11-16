"""A VM's group (owner) and whether it is a net's shared VM: from its PVE description, names only as a fallback."""
from pvelab import owners


def test_owner_of_reads_token_then_legacy_then_name(monkeypatch):
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    assert owners.owner_of({"description": "managed-by=lab.sh net=2 owner=user01"}, "x") == "user01"
    assert owners.owner_of({"description": "managed-by=lab.sh net=2 student=net2-student03"}, "net2-db") == "student03"
    assert owners.owner_of({"name": "net2-student03-2", "description": "managed-by=lab.sh net=2"}) == "student03"
    assert owners.owner_from_name("net2-student03-2", "net2") == "student03"
    assert owners.owner_from_name("alice01", "net2") == "alice01"


def test_is_shared_vm_by_role_or_legacy_name(monkeypatch):
    monkeypatch.setattr(owners, "shared_name", lambda n: f"net{n}-shared")
    assert owners.is_shared_vm({"description": "managed-by=lab.sh net=2 role=shared"})
    assert owners.is_shared_vm({"name": "net2-shared", "description": "managed-by=lab.sh net=2"}, "2")
    assert not owners.is_shared_vm({"name": "net2-db", "description": "managed-by=lab.sh net=2 owner=a"}, "2")


def test_by_owner_mixed_old_and_new(monkeypatch):
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(owners, "shared_name", lambda n: f"net{n}-shared")
    confs = {1: {"name": "net2-student03", "description": "managed-by=lab.sh net=2"},
             2: {"name": "x", "description": "managed-by=lab.sh net=2 owner=student03"},
             3: {"name": "net2-shared", "description": "managed-by=lab.sh net=2"}}
    assert owners.by_owner({i: c["name"] for i, c in confs.items()}, confs) == {"student03": [1, 2]}
    assert owners.vms_per_owner({i: c["name"] for i, c in confs.items()}, confs) == 2


def test_with_tokens_sets_replaces_and_removes():
    assert owners.with_tokens("managed-by=lab.sh net=2 student=a\nnote", owner="b", student=None) == \
        "managed-by=lab.sh net=2 owner=b\nnote"
    assert owners.with_tokens("managed-by=lab.sh net=2 owner=a", owner="b", role="shared") == \
        "managed-by=lab.sh net=2 owner=b role=shared"
