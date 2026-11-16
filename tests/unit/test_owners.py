"""A VM's group (owner) and whether it is a net's shared VM: from its PVE description, names only as a fallback."""
import os
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


def test_is_managed_accepts_old_and_new_tag():
    from pvelab.inventory import is_managed
    d = "managed-by=lab.sh net=2"
    assert is_managed({"tags": "pvelab;net-net2", "description": d})
    assert is_managed({"tags": "lab-student", "description": d})
    assert not is_managed({"tags": "other", "description": d})


def clone_args(monkeypatch, **kw):
    from pvelab import lifecycle
    seen = []
    monkeypatch.setattr(lifecycle, "run", lambda *a, **k: seen.append([str(x) for x in a]) or "")
    monkeypatch.setattr(lifecycle, "on_node", lambda node, *a: a, raising=False)
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(lifecycle, "cred_save", lambda rows: None, raising=False)
    for f, v in (("node_of", "pve1"), ("host_dns", "1.1.1.1"), ("ssh_keys", b""), ("ip_config", "ip=dhcp")):
        monkeypatch.setattr(lifecycle, f, lambda *a, v=v: v)
    try:
        lifecycle.clone_vm(301, "net95-user01", 9000, "95", "10.0.0.5", {}, "pw", 1, 512, 0, **kw)
    except Exception:
        pass
    return next(a for a in seen if "--tags" in a)


def test_new_vm_gets_owner_role_and_net_tag(monkeypatch):
    a = clone_args(monkeypatch, owner="user01")
    assert a[a.index("--tags") + 1] == "pvelab;net-net95"
    assert a[a.index("--description") + 1].endswith("net=95 owner=user01")


def test_shared_vm_gets_role_shared(monkeypatch):
    a = clone_args(monkeypatch, shared=True)
    assert a[a.index("--description") + 1].endswith("net=95 role=shared")


def test_more_vms_keep_the_nets_old_prefix_and_login(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: f"net{n}")
    inv = {201: {"name": "net2-student01", "description": "managed-by=lab.sh net=2", "tags": "lab-student",
                 "ciuser": "student"},
           202: {"name": "net2-student02-1", "description": "managed-by=lab.sh net=2", "tags": "lab-student",
                 "ciuser": "student"}}
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    assert create.net_defaults("2", inv) == ("student", "student")
    assert create.net_defaults("3", inv) == ("user", "user")
    monkeypatch.setenv("LAB_PREFIX", "team")
    monkeypatch.setattr(create, "PREFIX", "team")
    assert create.net_defaults("2", inv) == ("team", "student")      # set by hand: it wins


def test_plain_defaults():
    import subprocess, sys
    env = {k: v for k, v in os.environ.items() if k not in ("LAB_PREFIX", "LAB_USER", "LAB_TAG")}
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    out = subprocess.run([sys.executable, "-c", "from pvelab import config as c; print(c.PREFIX, c.GUEST_USER, c.TAG)"],
                         env=env, capture_output=True, text=True).stdout.split()
    assert out == ["user", "user", "pvelab"]
