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
        FakeChange.log, FakeChange.last = [], self

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
    monkeypatch.setattr(readdress, "set_net_tag", lambda i, drop=(): FakeChange.log.append(("tag", i)))
    monkeypatch.setattr("pvelab.owners.read_conf", lambda i: {"description": "managed-by=lab.sh net=5 owner=a"})
    saved = []
    monkeypatch.setattr(readdress, "cred_rows", lambda: {i: dict(r) for i, r in ROWS.items()})
    monkeypatch.setattr(readdress, "cred_save", lambda rows: FakeChange.log.append(("creds", sorted(
        r["name"] for r in rows.values()))))
    return readdress.net_rename, saved


def test_net_rename_keeps_vm_names_by_default(net_rename):
    net_rename[0]("old", "new")
    log = FakeChange.log
    assert ("warnings", []) in log and not any(e[0] == "set_vms" and e[2] for e in log)
    assert sorted(e[1] for e in log if e[0] == "tag") == [201, 202, 203]       # every VM's net tag follows


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


def test_rollback_puts_the_net_tag_back(monkeypatch):
    from pvelab.commands import snapshots
    from pvelab import lifecycle as readdress        # where set_net_tag reads and runs
    ran = []
    monkeypatch.setattr(snapshots, "targets", lambda a: [201])
    monkeypatch.setattr(snapshots, "vm_names", lambda ids: {201: "net5-user01"})
    monkeypatch.setattr(snapshots, "has_snapshot", lambda i, s: True)
    monkeypatch.setattr(snapshots, "confirm_yes", lambda msg: None)
    monkeypatch.setattr(snapshots, "qm_argv", lambda i, *a: ["qm", *a])
    monkeypatch.setattr(snapshots, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(readdress, "read_conf", lambda i: {"tags": "lab-student", "_node": "pve1",   # the snapshot's
                                                          "description": "managed-by=lab.sh net=5 owner=user01"})
    monkeypatch.setattr(readdress, "on_node", lambda node, *a: a)
    monkeypatch.setattr(readdress, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net5")
    snapshots.cmd_rollback(["net5-user01"])
    assert ran[0][1] == "rollback" and ran[1] == ("qm", "set", 201, "--tags", "lab-student;pvelab;net-net5")


# --- final review fixes ---

def _managed(name, net, extra="", user="student"):
    return {"name": name, "description": f"managed-by=lab.sh net={net}{extra}", "tags": "pvelab", "ciuser": user}


def test_create_shared_refuses_a_second_shared_vm(monkeypatch):
    import importlib
    from pvelab.util import LabError
    create, _ = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {210: _managed("server", "95", " role=shared")})
    with pytest.raises(LabError, match="server is net95's shared VM"):
        create.cmd_create(["--shared", "debian13", "--net", "95"])


def test_net_defaults_ignore_an_accounts_net(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "net2")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    inv = {201: _managed("net2-s1101001", "2", " owner=s1101001", "s1101001"),
           202: _managed("net2-s1101002", "2", " owner=s1101002", "s1101002")}
    assert create.net_defaults("2", inv) == ("user", "user")


def test_net_defaults_after_a_net_rename(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "web")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    inv = {201: _managed("net2-student01", "2", " owner=student01")}
    assert create.net_defaults("2", inv)[0] == "student"


def test_migrate_records_an_owner_from_a_chosen_name(monkeypatch):
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net2")
    todo, by_name = migrate.plan({201: {**_managed("alice01", "2"), "tags": "lab-student"},     # --accounts, 1 each
                                  202: {**_managed("alice02-1", "2", " student=alice02"), "tags": "lab-student"}})
    assert todo[201][0].endswith("owner=alice01") and by_name == ["alice01"]
    assert todo[202][0].endswith("net=2 owner=alice02")


def test_account_keys_keep_their_own_numbers(monkeypatch):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    create.cmd_create(["2x2", "debian13", "--net", "95", "--accounts", "pc-01-1:Pass-00001,pc-01-2:Pass-00002"])
    assert [g[3] for g in got] == ["pc-01-1", "pc-01-1", "pc-01-2", "pc-01-2"]
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: "net95")
    assert owners.owner_of({"description": "managed-by=lab.sh net=95 student=pc-01-1"}) == "pc-01-1"


def test_net_tag_is_a_valid_pve_tag(monkeypatch):
    from pvelab import lifecycle
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: "Prod (A)")
    assert lifecycle.net_tags("7") == "pvelab;net-prod--a-"


# --- /code-review round 1 ---

def test_create_shared_again_is_skipped_not_refused(monkeypatch, capsys):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {210: _managed("net95-shared", "95", " role=shared")})
    create.cmd_create(["--shared", "debian13", "--net", "95"])
    assert got == []


def test_a_vm_with_an_owner_is_not_shared_by_its_name(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners, "shared_name", lambda n: "net2-shared")
    assert not owners.is_shared_vm({"name": "net2-shared", "description": "managed-by=lab.sh net=2 owner=alice"})


def test_net_defaults_ignore_account_holders_named_like_defaults(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "x")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    inv = {201: _managed("alice01", "2", " owner=alice01", "s1101002"),
           202: _managed("bob02", "2", " owner=bob02", "s1101003")}
    assert create.net_defaults("2", inv) == ("user", "user")
    inv = {201: _managed("x-student01", "2", " owner=student01"), 202: _managed("x-student02", "2", " owner=student02")}
    assert create.net_defaults("2", inv) == ("student", "student")


def test_net_rename_waits_for_owners_to_be_recorded(net_rename, monkeypatch):
    from pvelab.util import LabError
    monkeypatch.setattr("pvelab.owners.read_conf", lambda i: {"name": "x", "description": "managed-by=lab.sh net=5"
                                                              + ("" if i == 202 else " owner=a")})
    with pytest.raises(LabError, match="web: its owner is not recorded"):
        net_rename[0]("old", "new")


def test_new_tags_keeps_an_admins_own_net_tags(monkeypatch):
    from pvelab import lifecycle
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: "prod")
    assert lifecycle.new_tags("net-admin;net-test;pvelab", "5", drop=("old",)) == "net-admin;net-test;pvelab;net-prod"
    assert lifecycle.new_tags("net-old;pvelab", "5", drop=("old",)) == "pvelab;net-prod"


def test_shared_ip_reads_only_the_configs_given(monkeypatch):
    from pvelab import isolation, owners
    monkeypatch.setattr(owners, "read_conf", lambda i: pytest.fail(f"read {i}"))
    rows = {201: {"name": "a", "net": "5", "ip": "1"}, 202: {"name": "b", "net": "5", "ip": "2"}}
    confs = {202: {"name": "b", "description": "managed-by=lab.sh net=5 role=shared"}}
    assert isolation.shared_ip("5", rows, confs) == "2"


# --- /code-review round 2 ---

def test_a_deleted_vms_row_does_not_split_its_owner(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: "net2")
    confs = {101: {"name": "a-1", "description": "managed-by=lab.sh net=2 owner=alice01"},
             102: {"name": "a-2", "description": "managed-by=lab.sh net=2 owner=alice01"}}
    monkeypatch.setattr(owners, "read_conf", lambda i: confs.get(i))
    assert owners.by_owner({101: "a-1", 102: "a-2", 103: "net2-alice01-3"})["alice01"] == [101, 102]


def test_rollback_takes_off_a_net_tag_from_before_a_rename(monkeypatch):
    from pvelab.commands import snapshots
    from pvelab import lifecycle
    ran, tags = [], iter(["pvelab;net-cs102", "pvelab;net-cs101"])        # before / after the rollback
    monkeypatch.setattr(snapshots, "targets", lambda a: [201])
    monkeypatch.setattr(snapshots, "vm_names", lambda ids: {201: "x"})
    monkeypatch.setattr(snapshots, "has_snapshot", lambda i, s: True)
    monkeypatch.setattr(snapshots, "confirm_yes", lambda msg: None)
    monkeypatch.setattr(snapshots, "qm_argv", lambda i, *a: ["qm", *a])
    monkeypatch.setattr(snapshots, "run", lambda *a, **k: ran.append(a))
    conf = {"_node": "pve1", "description": "managed-by=lab.sh net=5 owner=a"}
    monkeypatch.setattr(snapshots, "read_conf", lambda i: {**conf, "tags": next(tags)})
    monkeypatch.setattr(lifecycle, "read_conf", lambda i: {**conf, "tags": "pvelab;net-cs101"})
    monkeypatch.setattr(lifecycle, "on_node", lambda node, *a: a)
    monkeypatch.setattr(lifecycle, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(lifecycle.nets, "net_name", lambda n: "cs102")
    snapshots.cmd_rollback(["x"])
    assert ran[-1] == ("qm", "set", 201, "--tags", "pvelab;net-cs102")


def owner_cmd(monkeypatch, confs, inv=None):
    from pvelab.commands import owner
    ran = []
    monkeypatch.setattr(owner, "targets", lambda a: sorted(confs))
    monkeypatch.setattr(owner, "read_conf", lambda i: confs[i])
    monkeypatch.setattr(owner, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(owner, "on_node", lambda node, *a: a)
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets: None)
    monkeypatch.setattr(owner, "inventory", lambda: inv or confs)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: "net2")
    return owner, ran


def test_owner_key_written_like_a_vm_name_loses_its_net(monkeypatch):
    owner, ran = owner_cmd(monkeypatch, {102: _managed("net2-alice01-2", "2", " owner=bob")})
    owner.cmd_owner(["net2-alice01-2", "net2-alice01"])
    assert ran[0][-1].endswith("owner=alice01")


def test_owner_refuses_the_shared_vm_and_the_word_shared(monkeypatch):
    from pvelab.helptext import UsageError
    from pvelab.util import LabError
    owner, ran = owner_cmd(monkeypatch, {210: _managed("net2-shared", "2", " role=shared")})
    with pytest.raises(LabError, match="net2-shared is net2's shared VM"):
        owner.cmd_owner(["net2-shared", "web"])
    with pytest.raises(UsageError, match="shared"):
        owner.cmd_owner(["net2-shared", "shared"])
    assert ran == []


def test_owner_warns_when_owners_get_uneven(monkeypatch, capsys):
    inv = {1: _managed("net2-user01", "2", " owner=user01"), 2: _managed("net2-user02", "2", " owner=user02"),
           3: _managed("net2-user03", "2", " owner=user03")}
    owner, _ = owner_cmd(monkeypatch, {2: inv[2]}, {**inv, 2: _managed("net2-user02", "2", " owner=user01")})
    owner.cmd_owner(["net2-user02", "user01"])
    assert "net2: owners have 1 to 2 VMs" in capsys.readouterr().err


def test_the_shared_vm_owner_is_the_shared_group(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: "net2")
    assert owners.owner_of(_managed("net2-shared", "2", " role=shared")) == owners.SHARED_GROUP


# --- /code-review round 3 ---

def test_net_rename_dry_run_previews_with_owners_not_recorded(net_rename, monkeypatch, capsys):
    monkeypatch.setattr("pvelab.owners.read_conf", lambda i: {"description": "managed-by=lab.sh net=5"})
    net_rename[0]("old", "new", dry=True)
    assert any("recorded first" in str(line) for line in FakeChange.last.plan.steps)


def test_net_defaults_ignore_one_account_and_mixed_prefixes(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "net2")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    one = {201: _managed("alice01", "2", " owner=alice01", "s1101001")}
    assert create.net_defaults("2", one) == ("user", "user")
    mixed = {i: _managed(k, "2", f" owner={k}", "user") for i, k in ((1, "alice01"), (2, "bob02"), (3, "carol03"))}
    assert create.net_defaults("2", mixed) == ("user", "user")
    lone = {201: _managed("net2-student01", "2", " owner=student01", "student")}
    assert create.net_defaults("2", lone) == ("student", "student")


def test_wizard_knows_a_renamed_shared_vm(monkeypatch):
    import importlib
    test_ux = importlib.import_module("test_ux")
    from pvelab import wizard
    data = {**test_ux.DATA, "nets": ["cs101"], "shared": ["cs101"]}
    monkeypatch.setattr(wizard.Wizard, "net_key", lambda self, n: "2")
    with pytest.raises(wizard.Back):
        wizard.Wizard(data).shared()


def test_desc_value_reads_the_marker_line_only():
    from pvelab.inventory import desc_value
    conf = {"description": "managed-by=lab.sh template=9 net=2 subnet=x\nrole=db server\nowner=bob"}
    assert desc_value(conf, "role") == "" and desc_value(conf, "owner") == "" and desc_value(conf, "net") == "2"
    notes = {"description": "my notes\nmanaged-by=lab.sh net=3 owner=a"}
    assert desc_value(notes, "owner") == "a"
    from pvelab.owners import with_tokens
    assert with_tokens(notes["description"], owner="b") == "my notes\nmanaged-by=lab.sh net=3 owner=b"


def test_a_gone_vms_owner_loses_its_net_prefix(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr(owners, "read_conf", lambda i: None)
    assert owners.owner_of(None, "net2-user01-1", net="2") == "user01"
    assert owners.by_owner({1: "net2-user01-1", 2: "net2-user01-2"}, net="2") == {"user01": [1, 2]}
