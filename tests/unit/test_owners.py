"""A VM's group (owner) and whether it is a net's shared VM: from its PVE description, names only as a fallback."""
import pytest
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


ROWS = {201: {"name": "old-student01", "net": "5", "user": "s", "password": "p", "ip": "-"},
        202: {"name": "web", "net": "5", "user": "s", "password": "p", "ip": "-"},
        203: {"name": "old-shared", "net": "5", "user": "s", "password": "p", "ip": "-"}}


class FakeChange:
    """readdress.Change without hosts: records what the rename asks of it."""
    log = []

    def __init__(self, n, what, vms=None):
        from pvelab import readdress
        self.rows = {i: dict(r) for i, r in ROWS.items() if vms is None or i in vms}
        self.plan, self.node, self.vms = readdress.Plan(), dict.fromkeys(self.rows, "pve1"), set(self.rows)
        FakeChange.log = []

    def warnings(self, vms=None):
        self.vms = set(self.rows) if vms is None else set(vms)
        FakeChange.log.append(("warnings", sorted(self.vms)))

    def start(self, dry):
        return True

    def set_vms(self, key, values):
        FakeChange.log.append(("set_vms", key, dict(values)))

    def finish(self, done, rows):
        FakeChange.log.append(("finish", sorted(done)))


@pytest.fixture
def net_rename(monkeypatch):
    from pvelab import readdress
    monkeypatch.setattr(readdress, "Change", FakeChange)
    monkeypatch.setattr(readdress, "local_net", lambda v, doing: "5")
    monkeypatch.setattr(readdress.nets, "net_name", lambda n: "old")
    monkeypatch.setattr(readdress.nets, "name_problem", lambda new, n: None)
    monkeypatch.setattr(readdress, "inventory", lambda: {})
    monkeypatch.setattr(readdress, "rename_on_host", lambda n, old, new: FakeChange.log.append(("host", new)))
    monkeypatch.setattr(readdress, "set_net_tag", lambda i, node, n: FakeChange.log.append(("tag", i)))
    saved = []
    monkeypatch.setattr(readdress, "cred_rows", lambda: {i: dict(r) for i, r in ROWS.items()})
    monkeypatch.setattr(readdress, "cred_save", lambda rows: FakeChange.log.append(("creds", sorted(
        r["name"] for r in rows.values()))))
    return readdress.net_rename, saved


def test_net_rename_keeps_vm_names_by_default(net_rename):
    net_rename[0]("old", "new")
    log = FakeChange.log
    assert ("warnings", []) in log and not any(e[0] == "set_vms" and e[2] for e in log)
    assert [e[1] for e in log if e[0] == "tag"] == [201, 202, 203]       # every VM's net tag follows


def test_net_rename_rename_vms_renames_old_prefix_only(net_rename):
    net_rename[0]("old", "new", rename_vms=True)
    sets = [e for e in FakeChange.log if e[0] == "set_vms"]
    assert sets == [("set_vms", "name", {201: "new-student01", 203: "new-shared"})]
    kinds = [e[0] for e in FakeChange.log]
    assert kinds.index("set_vms") < kinds.index("creds")                # config first, credentials after


def test_rename_the_shared_vm_keeps_it_shared(monkeypatch):
    from pvelab import readdress
    rows = {203: {"name": "old-shared", "net": "5", "user": "s", "password": "p", "ip": "-"}}
    ran = []
    monkeypatch.setattr(readdress, "Change", FakeChange)
    monkeypatch.setattr(readdress, "resolve", lambda v: 203)
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "cred_save", lambda r: None)
    monkeypatch.setattr(readdress, "inventory", lambda: {})
    monkeypatch.setattr(readdress, "read_conf", lambda i: {"name": "old-shared",
                                                          "description": "managed-by=lab.sh net=5 role=shared"})
    monkeypatch.setattr(readdress, "run", lambda *a, **k: ran.append(a))
    readdress.vm_rename("old-shared", "old-files")
    assert ("set_vms", "name", {203: "old-files"}) in FakeChange.log and ran == []     # role=shared stays
