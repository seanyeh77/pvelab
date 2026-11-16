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
    assert create.net_defaults("2", inv) == ("team", "user")         # set by hand: it wins, with the default login


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
    monkeypatch.setattr(readdress, "inventory", lambda: {i: {"description": "managed-by=lab.sh net=5 owner=a",
                                                             "tags": "pvelab"} for i in ROWS})
    monkeypatch.setattr(readdress, "rename_on_host", lambda n, old, new: FakeChange.log.append(("host", new)))
    monkeypatch.setattr(readdress, "set_net_tag", lambda i, drop=(): FakeChange.log.append(("tag", i)))
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
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2", "3", "5", "95"})
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net2")
    todo, by_name, _ = migrate.plan({201: {**_managed("alice01", "2"), "tags": "lab-student"},     # --accounts, 1 each
                                  202: {**_managed("alice02-1", "2", " student=alice02"), "tags": "lab-student"}})
    assert todo[201][0].endswith("owner=alice01") and by_name == [201]
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
    from pvelab import readdress
    monkeypatch.setattr(readdress, "inventory", lambda: {i: {"name": "x", "tags": "pvelab", "description":
                                                             "managed-by=lab.sh net=5" + ("" if i == 202 else " owner=a")}
                                                         for i in ROWS})
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
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None, changed=None: None)
    monkeypatch.setattr(owner, "inventory", lambda: inv or confs)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr(owner.nets, "all_nets", lambda: {"2": ("net2", "10.10.2.0/24")})
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
    from pvelab import readdress
    monkeypatch.setattr(readdress, "inventory", lambda: {i: {"tags": "pvelab", "description": "managed-by=lab.sh net=5"}
                                                         for i in ROWS})
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
    rows = {1: {"name": "net2-user01-1", "net": "2"}, 2: {"name": "net2-user01-2", "net": "2"}}
    assert owners.owner_of(owners.confs_of(rows)[1]) == "user01"
    assert owners.by_owner(rows) == {"user01": [1, 2]}


def test_net_defaults_read_the_recorded_prefix(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "web")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    inv = {1: _managed("lab-a", "2", " owner=team1 prefix=team", "dev")}         # renamed, owner renamed: prefix stays
    assert create.net_defaults("2", inv) == ("team", "dev")


def test_new_vm_records_its_prefix(monkeypatch):
    a = clone_args(monkeypatch, owner="user01", prefix="user")
    assert a[a.index("--description") + 1].endswith("owner=user01 prefix=user")


# --- /code-review round 4 ---

def test_a_legacy_student_token_on_a_note_line_still_counts(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: "net2")
    conf = {"name": "web01", "description": "managed-by=lab.sh net=2\nmy note student=net2-student03"}
    assert owners.owner_of(conf) == "student03"


def test_a_template_built_token_is_read_anywhere():
    from pvelab.inventory import desc_value
    assert desc_value({"description": "my note\nsaved by pvelab from x built=2026-10-01"}, "built") == "2026-10-01"


def test_net_rename_is_not_blocked_by_a_vm_pvelab_no_longer_manages(net_rename, monkeypatch):
    from pvelab import readdress
    monkeypatch.setattr(readdress, "inventory", lambda: {
        201: {**_managed("old-student01", "5", " owner=student01")},
        202: {"name": "web", "description": "managed-by=lab.sh net=5", "tags": ""},     # tag taken off by hand
        203: {**_managed("old-shared", "5", " role=shared")}})
    net_rename[0]("old", "new")


def test_migrate_skips_vms_on_offline_hosts(monkeypatch, capsys):
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2", "3", "5", "95"})
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net2")
    inv = {201: {**_managed("net2-student01", "2"), "tags": "lab-student", "_node": "pve2"}}
    monkeypatch.setattr(migrate, "inventory", lambda: inv)
    monkeypatch.setattr(migrate, "offline_nodes", lambda: ["pve2"])
    monkeypatch.setattr(migrate, "run", lambda *a, **k: pytest.fail("ran qm on an offline host"))
    migrate.ensure(False)
    assert capsys.readouterr().out == ""


def test_owner_warns_about_a_slot_name_and_different_passwords(monkeypatch, capsys):
    import importlib
    tm = importlib.import_module("test_migrate")
    from pvelab.commands import owner
    rows = {203: {"name": "net2-user03", "net": "2", "user": "user", "password": "a"},
            204: {"name": "net2-user04", "net": "2", "user": "user", "password": "b"}}
    inv = {203: {**_managed("net2-user03", "2", " owner=user04"), "_node": "pve1"},
           204: {**_managed("net2-user04", "2", " owner=user04"), "_node": "pve1"}}
    monkeypatch.setattr(owner, "targets", lambda a: [203])
    monkeypatch.setattr(owner, "read_conf", lambda i: _managed("net2-user03", "2", " owner=user03"))
    monkeypatch.setattr(owner, "run", lambda *a, **k: None)
    monkeypatch.setattr(owner, "on_node", lambda node, *a: a)
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None, changed=None: None)
    monkeypatch.setattr(owner, "inventory", lambda: inv)
    monkeypatch.setattr(owner, "cred_rows", lambda: rows)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr(owner.nets, "all_nets", lambda: {"2": ("net2", "10.10.2.0/24")})
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    owner.cmd_owner(["net2-user03", "user04"])
    err = capsys.readouterr().err
    assert "pvelab passwd net2-user03" in err and "pvelab rename net2-user03 NEW" in err
    assert tm                                 # (imported for its vm() helper's module setup)


def test_create_plan_shows_no_login_line_for_the_nets_own_login(monkeypatch):
    import importlib
    create, _ = importlib.import_module("test_accounts").fake(monkeypatch)
    monkeypatch.setattr(create, "inventory", lambda: {201: _managed("net95-student01", "95", " owner=student01")})
    monkeypatch.setattr(create, "GUEST_USER", "user")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    lines = []
    monkeypatch.setattr(create.Plan, "do", lambda self, s, *a, **k: lines.append(s))
    create.cmd_create(["2", "debian13", "--net", "95"])
    assert lines and not any(line.startswith("log in as") for line in lines), lines


# --- /code-review round 5 ---

def test_net_rename_tags_only_vms_pvelab_manages(net_rename, monkeypatch):
    from pvelab import readdress
    monkeypatch.setattr(readdress, "inventory", lambda: {
        201: _managed("old-student01", "5", " owner=student01"),
        202: {"name": "web", "description": "managed-by=lab.sh net=5 owner=web", "tags": "theirs"},   # released
        203: _managed("old-shared", "5", " role=shared")})
    net_rename[0]("old", "new")
    assert sorted(e[1] for e in FakeChange.log if e[0] == "tag") == [201, 203]


def test_net_defaults_of_an_older_net_with_a_numbered_login_or_one_odd_vm(monkeypatch):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: "net2")
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    inv = {i: _managed(f"net2-student{i:02d}", "2", f" owner=student{i:02d}", "s1101001") for i in (1, 2, 3)}
    assert create.net_defaults("2", inv) == ("student", "s1101001")
    inv[9] = _managed("web01", "2", " owner=web01", "s1101001")
    assert create.net_defaults("2", inv)[0] == "student"


def test_rename_refuses_the_shared_name_for_an_owners_vm(monkeypatch):
    from pvelab import readdress
    from pvelab.util import LabError
    rows = {201: {"name": "net2-user03", "net": "2", "user": "u", "password": "p", "ip": "-"}}
    monkeypatch.setattr(readdress, "resolve", lambda v: 201)
    monkeypatch.setattr(readdress, "cred_rows", lambda: rows)
    monkeypatch.setattr(readdress, "inventory", lambda: {})
    monkeypatch.setattr(readdress, "read_conf", lambda i: _managed("net2-user03", "2", " owner=user03"))
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    with pytest.raises(LabError, match="net2-shared is the name of net2's shared VM"):
        readdress.vm_rename("net2-user03", "net2-shared")


def test_owner_key_shared_or_another_nets_is_refused(monkeypatch):
    from pvelab.helptext import UsageError
    owner, ran = owner_cmd(monkeypatch, {204: _managed("net2-user04", "2", " owner=user04")})
    monkeypatch.setattr(owner.nets, "all_nets", lambda: {"2": ("net2", ""), "3": ("net3", "")})
    for key in ("net2-shared", "net3-alice"):
        with pytest.raises(UsageError):
            owner.cmd_owner(["net2-user04", key])
    assert ran == []


# --- /code-review round 6 ---

def nd(monkeypatch, name="net2"):
    from pvelab.commands import create
    monkeypatch.setattr(create.nets, "net_name", lambda n: name)
    monkeypatch.delenv("LAB_PREFIX", raising=False)
    monkeypatch.delenv("LAB_USER", raising=False)
    monkeypatch.setattr(create, "PREFIX", "user")
    monkeypatch.setattr(create, "GUEST_USER", "user")
    return create


def test_net_defaults_ignore_accounts_named_like_slots(monkeypatch):
    create = nd(monkeypatch)
    inv = {i: _managed(f"net2-team{i:02d}", "2", f" owner=team{i:02d}", f"s110100{i}") for i in (1, 2, 3)}
    assert create.net_defaults("2", inv) == ("user", "user")


def test_net_defaults_keep_an_old_dhcp_set(monkeypatch):
    create = nd(monkeypatch)
    inv = {i: _managed(f"student{i:02d}", "dhcp", "", "student") for i in (1, 2)}
    assert create.net_defaults("dhcp", inv) == ("student", "student")


def test_lab_prefix_alone_takes_the_default_login(monkeypatch):
    create = nd(monkeypatch)
    monkeypatch.setenv("LAB_PREFIX", "team")
    monkeypatch.setattr(create, "PREFIX", "team")
    inv = {1: _managed("net2-student01", "2", " owner=student01 prefix=student", "student")}
    assert create.net_defaults("2", inv) == ("team", "user")


def test_accounts_that_are_one_owner_or_a_bad_key_are_refused(monkeypatch):
    import importlib
    from pvelab.util import LabError
    create, _ = importlib.import_module("test_accounts").fake(monkeypatch)
    with pytest.raises(LabError, match=r"owner\(s\) twice"):
        create.cmd_create(["debian13", "--net", "95", "--accounts", _csv(monkeypatch, "alice01,s1,\nnet95-alice01,s2,")])
    monkeypatch.setattr(create.nets, "pvelab_names", lambda nets=None: ["net95", "net3"])
    with pytest.raises(LabError, match="net3 is another network"):
        create.cmd_create(["debian13", "--net", "95", "--accounts", _csv(monkeypatch, "net3-bob,s3,")])


def _csv(monkeypatch, body):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("name,user,password\n" + body + "\n")
    f.close()
    return f.name


# --- /code-review round 7 ---

def test_net_defaults_count_logins_per_owner(monkeypatch):
    create = nd(monkeypatch)
    accts = {1: _managed("net2-alice01-1", "2", " owner=alice01", "s1101002"),
             2: _managed("net2-alice01-2", "2", " owner=alice01", "s1101002"),
             3: _managed("net2-bob02-1", "2", " owner=bob02", "s1101003"),
             4: _managed("net2-bob02-2", "2", " owner=bob02", "s1101003")}
    assert create.net_defaults("2", accts) == ("user", "user")
    one = {1: _managed("net2-student01-1", "2", " owner=student01", "root"),
           2: _managed("net2-student01-2", "2", " owner=student01", "student")}
    assert create.net_defaults("2", one)[0] == "student"


def test_with_tokens_removes_a_token_from_every_line():
    from pvelab.owners import with_tokens
    assert with_tokens("managed-by=lab.sh net=2\nstudent=net2-a", owner="a", student=None) == \
        "managed-by=lab.sh net=2 owner=a"


def test_key_problem_knows_only_pvelabs_nets():
    from pvelab.owners import key_problem
    assert key_problem("dev-team", ["net2"]) is None
    assert key_problem("net2-x", ["net2"])


def test_removing_a_token_keeps_the_notes_blank_lines():
    from pvelab.owners import with_tokens
    desc = "managed-by=lab.sh net=2 student=a\nnote one\n\nnote two\nstudent=a"
    assert with_tokens(desc, owner="a", student=None) == "managed-by=lab.sh net=2 owner=a\nnote one\n\nnote two"


# --- /code-review round 8 ---

def test_notes_that_mention_a_token_are_left_alone():
    from pvelab.owners import legacy_student, with_tokens
    desc = "managed-by=lab.sh net=2\nsee student=net2-bob for details\nstudent=x"
    assert legacy_student({"description": "managed-by=lab.sh net=2\nsee student=net2-bob for details"}) == ""
    assert legacy_student({"description": desc}) == "x"               # appended at the end: an older rename's
    assert with_tokens(desc, owner="a", student=None) == \
        "managed-by=lab.sh net=2 owner=a\nsee student=net2-bob for details"


def test_an_owner_without_a_login_is_not_an_account(monkeypatch):
    create = nd(monkeypatch)
    inv = {1: _managed("net2-student01", "2", " owner=student01", "s1"),
           2: _managed("net2-student02", "2", " owner=student02", "s2"),
           3: _managed("net2-student03", "2", " owner=student03", "")}
    assert create.net_defaults("2", inv) == ("user", "user")             # two with their own login: accounts
    inv = {1: _managed("net2-student01", "2", " owner=student01", "student"),
           3: _managed("net2-student03", "2", " owner=student03", "")}
    assert create.net_defaults("2", inv)[0] == "student"


def test_a_token_before_a_trailing_newline_is_read_and_removed_alike():
    from pvelab.owners import legacy_student, with_tokens
    desc = "managed-by=lab.sh net=2\nnotes student=bob\n"
    assert legacy_student({"description": desc}) == "bob"
    assert with_tokens(desc, owner="bob", student=None) == "managed-by=lab.sh net=2 owner=bob\nnotes"


# --- /code-review round 9 ---

def test_removing_an_appended_token_keeps_blank_lines_before_it():
    from pvelab.owners import with_tokens
    assert with_tokens("managed-by=lab.sh net=2\nline one\n\n\nstudent=x", owner="x", student=None) == \
        "managed-by=lab.sh net=2 owner=x\nline one\n\n"


def test_by_owner_keeps_an_owners_vms_whose_configs_disagree_on_the_net(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    confs = {201: {"name": "a", "description": "managed-by=lab.sh net=2 owner=alice"},
             210: {"name": "b", "description": "managed-by=lab.sh net=3 owner=alice"}}
    assert owners.by_owner({201: "a", 210: "b"}, confs) == {"alice": [201, 210]}


def test_migrate_leaves_vms_of_a_net_not_known_here(monkeypatch):
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: f"net{n}")
    todo, _, _ = migrate.plan({201: {**_managed("web-student01", "5"), "tags": "lab-student"},
                            202: {**_managed("net2-student01", "2"), "tags": "lab-student"}})
    assert list(todo) == [202]


def test_recovery_commands_do_not_record_owners_first(monkeypatch):
    from pvelab import cli
    assert {"node remove", "node add", "node rename", "node upgrade", "pull"} <= cli.NO_MIGRATE


def test_a_failed_recording_does_not_stop_the_command(monkeypatch, capsys):
    from pvelab import cli, migrate
    from pvelab.util import LabError
    monkeypatch.setattr(migrate, "inventory", lambda: (_ for _ in ()).throw(LabError("no quorum")))
    cli.record_owners("start", False)
    assert "owners not recorded" in capsys.readouterr().err


def test_move_names_the_shared_vm_not_its_group(monkeypatch):
    from pvelab.commands import move
    monkeypatch.setattr(move, "net_name", lambda n: f"net{n}")
    assert move.group_label(("2", "+shared")) == "net2's shared VM" and move.group_label(("2", "alice")) == "alice"


def test_create_warns_when_an_owner_gets_a_second_password(monkeypatch, capsys):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    conf = {**_managed("net95-student01-1", "95", " owner=student01"), "_node": "workshop1"}
    monkeypatch.setattr(create, "inventory", lambda: {201: conf})
    monkeypatch.setattr(create, "per_owner_on", lambda n, confs=None: 2)
    monkeypatch.setattr(create, "cred_rows", lambda: {201: {"name": "net95-student01-1", "net": "95", "user": "student",
                                                            "password": "Old-pass-1", "ip": "10.10.95.11"}})
    monkeypatch.setattr(create, "wait_first_boot", lambda ids, board: {i: "done" for i in ids})
    create.cmd_create(["1x2", "debian13", "--net", "95", "--password", "New-pass-22"])
    assert "student01: its VMs have two passwords now" in capsys.readouterr().err


def test_no_migrate_names_real_commands():
    from pvelab import cli
    assert cli.NO_MIGRATE <= set(cli.COMMANDS)


# --- /code-review round 10 ---

def test_failed_recordings_give_one_warning(monkeypatch, capsys):
    from pvelab import migrate
    from pvelab.util import LabError
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net2")
    monkeypatch.setattr(migrate, "offline_nodes", lambda: [])
    inv = {i: {**_managed(f"net2-student0{i}", "2"), "tags": "lab-student", "_node": "pve1"} for i in (1, 2, 3)}
    monkeypatch.setattr(migrate, "inventory", lambda: inv)
    monkeypatch.setattr(migrate, "on_node", lambda node, *a: a)
    monkeypatch.setattr(migrate, "run", lambda *a, **k: (_ for _ in ()).throw(LabError("read-only")))
    migrate.ensure(False)
    assert capsys.readouterr().err.count("WARNING") == 1


def test_owner_check_names_vms_recorded_elsewhere(monkeypatch, capsys):
    from pvelab.commands import owner
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr(migrate, "offline_nodes", lambda: ["pve2"])
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(owner, "inventory", lambda: {
        1: {**_managed("web-a01", "5"), "tags": "lab-student", "_node": "pve1"},
        2: {**_managed("net2-a01", "2"), "tags": "lab-student", "_node": "pve2"}})
    owner.cmd_owner_check(["--check"])
    out = capsys.readouterr().out
    assert "not recorded from here" in out and "web-a01" in out and "net2-a01" in out


def test_owner_refuses_a_vm_of_a_net_not_known_here(monkeypatch):
    from pvelab.util import LabError
    owner, ran = owner_cmd(monkeypatch, {204: _managed("x", "7", " owner=x")})
    monkeypatch.setattr(owner.nets, "all_nets", lambda: {"2": ("net2", "10.10.2.0/24")})

    with pytest.raises(LabError, match="not known on this host"):
        owner.cmd_owner(["x", "classa-alice"])
    assert ran == []


def test_groups_take_the_net_from_the_row(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    rows = {201: {"name": "a", "net": "2"}, 210: {"name": "b", "net": "2"}}
    confs = {201: {"name": "a", "description": "managed-by=lab.sh net=2 owner=alice"},
             210: {"name": "b", "description": "managed-by=lab.sh net=3 owner=alice"}}
    assert owners.groups_of(rows, confs) == {("2", "alice"): [201, 210]}


def test_move_names_whose_shared_vm(monkeypatch):
    from pvelab.commands import move
    monkeypatch.setattr(move, "net_name", lambda n: f"net{n}")
    assert move.group_label(("3", "+shared")) == "net3's shared VM"


# --- /code-review round 11 ---

def test_two_passwords_warned_for_a_vm_made_whose_setup_failed(monkeypatch, capsys):
    import importlib
    create, got = importlib.import_module("test_accounts").fake(monkeypatch)
    conf = {**_managed("net95-student01-1", "95", " owner=student01"), "_node": "workshop1"}
    monkeypatch.setattr(create, "inventory", lambda: {201: conf})
    monkeypatch.setattr(create, "per_owner_on", lambda n, confs=None: 2)
    monkeypatch.setattr(create, "cred_rows", lambda: {201: {"name": "net95-student01-1", "net": "95", "user": "student",
                                                            "password": "Old-pass-1", "ip": "10.10.95.11"}})
    create.cmd_create(["1x2", "debian13", "--net", "95", "--password", "New-pass-22"])     # first boot "fails"
    assert "student01: its VMs have two passwords now" in capsys.readouterr().err


def test_check_tells_offline_from_unknown_and_lists_tags_only(monkeypatch, capsys):
    from pvelab.commands import owner
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr(migrate, "offline_nodes", lambda: ["pve2"])
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(owner, "inventory", lambda: {
        1: {**_managed("web-a01", "5", " owner=a01"), "tags": "lab-student", "_node": "pve1"},
        2: {**_managed("net2-a02", "2"), "tags": "lab-student", "_node": "pve2"}})
    owner.cmd_owner_check(["--check"])
    out = capsys.readouterr().out
    assert "host offline" in out and "net2-a02" in out and "its net not known on this host" in out
    assert "web-a01" in out


def test_a_failed_recording_of_a_vm_without_a_name_still_warns(monkeypatch, capsys):
    from pvelab import migrate
    from pvelab.util import LabError
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: "net2")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: "net2")
    monkeypatch.setattr(migrate, "offline_nodes", lambda: [])
    conf = {"description": "managed-by=lab.sh net=2", "tags": "lab-student", "_node": "pve1"}
    monkeypatch.setattr(migrate, "inventory", lambda: {7: conf})
    monkeypatch.setattr(migrate, "on_node", lambda node, *a: a)
    monkeypatch.setattr(migrate, "run", lambda *a, **k: (_ for _ in ()).throw(LabError("x")))
    migrate.ensure(False)
    assert "7" in capsys.readouterr().err


def test_groups_drop_the_rows_net_prefix_when_the_config_disagrees(monkeypatch):
    from pvelab import owners
    monkeypatch.setattr(owners.nets, "net_name", lambda n: f"net{n}")
    rows = {201: {"name": "net2-alice01-1", "net": "2"}, 202: {"name": "net2-alice01-2", "net": "2"}}
    confs = {201: {"name": "net2-alice01-1", "description": "managed-by=lab.sh net=2"},
             202: {"name": "net2-alice01-2", "description": "managed-by=lab.sh net=3"}}
    assert owners.groups_of(rows, confs) == {("2", "alice01"): [201, 202]}


# --- /code-review round 13 ---

def check_out(monkeypatch, capsys, inv, offline=()):
    from pvelab.commands import owner
    from pvelab import migrate
    monkeypatch.setattr(migrate.nets, "known_nets", lambda found=None: {"2"})
    monkeypatch.setattr(migrate, "offline_nodes", lambda: list(offline))
    for m in ("pvelab.owners.nets.net_name", "pvelab.lifecycle.nets.net_name"):
        monkeypatch.setattr(m, lambda n: f"net{n}")
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(owner, "inventory", lambda: inv)
    owner.cmd_owner_check(["--check"])
    return capsys.readouterr().out


def test_check_does_not_list_recorded_vms_of_a_net_known_elsewhere(monkeypatch, capsys):
    out = check_out(monkeypatch, capsys, {1: {**_managed("web-a01", "5", " owner=a01"), "tags": "pvelab;net-web",
                                              "_node": "pve1"}})
    assert "every VM has its owner and tags recorded" in out


def test_check_lists_far_vms_on_an_offline_host_as_waiting(monkeypatch, capsys):
    out = check_out(monkeypatch, capsys, {1: {**_managed("web-a01", "5"), "tags": "lab-student", "_node": "pve2"}},
                    offline=["pve2"])
    assert "its net not known on this host: web-a01" in out and "host offline" not in out


def test_handout_names_a_shared_vm_without_a_row(monkeypatch):
    from pvelab.commands import handout
    from pvelab.util import LabError
    monkeypatch.setattr(handout, "targets", lambda a: [210, 201])
    monkeypatch.setattr(handout, "cred_rows", lambda: {201: {"name": "net2-a", "net": "2", "user": "u",
                                                             "password": "p", "ip": "-"}})
    monkeypatch.setattr(handout, "confs_of", lambda vms, confs=None: {
        210: {"name": "net2-files", "description": "managed-by=lab.sh net=2 role=shared"},
        201: {"name": "net2-a", "description": "managed-by=lab.sh net=2 owner=a"}})
    monkeypatch.setattr(handout, "sorted_nets", lambda on: ["2"])
    monkeypatch.setattr(handout, "is_exposed", lambda n: False)
    monkeypatch.setattr(handout, "forwarded_on", lambda n: set())
    try:
        handout.cmd_handout(["x", "--output", "/dev/null"])
    except KeyError as e:
        pytest.fail(f"KeyError {e}")
    except (LabError, OSError, TypeError):
        pass


# --- /code-review round 14 ---



# --- /code-review round 15 ---

def test_isolate_again_rebuilds_the_table(monkeypatch, capsys):
    from pvelab.commands import access
    done = []
    monkeypatch.setattr(access, "net_switch_args", lambda a: ("5", False, None))
    monkeypatch.setattr(access, "is_isolated", lambda n: True)
    monkeypatch.setattr(access, "set_isolation", lambda n, on=True, **k: done.append((n, on)))
    monkeypatch.setattr(access, "net_name", lambda n: "lab5")
    access.cmd_isolate(["--net", "lab5"])
    assert done == [("5", True)] and "rebuilt" in capsys.readouterr().out


def test_moving_only_the_shared_vm_names_it(monkeypatch):
    from pvelab.commands import move
    monkeypatch.setattr(move, "net_name", lambda n: f"net{n}")
    assert move.whose({("5", "+shared"): [1]}) == "net5's shared VM"
    assert move.whose({("5", "a"): [1], ("5", "b"): [2], ("5", "+shared"): [3]}) == "2 owner(s) and net5's shared VM"


# --- /code-review round 16 ---

def test_owner_warns_conditionally_for_an_sdn_net_with_its_gateway_elsewhere(monkeypatch, capsys):
    from pvelab.commands import owner
    monkeypatch.setattr(owner, "is_isolated", lambda n: False)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: "lab5")
    monkeypatch.setattr(owner, "gateway_elsewhere", lambda n: "another host")
    owner.refresh_isolation({"5"}, {}, {"5"})
    assert "if lab5 is isolated" in capsys.readouterr().err


def test_isolate_on_a_non_gateway_host_names_the_gateway(monkeypatch):
    from pvelab.commands import access
    from pvelab.util import LabError
    monkeypatch.setattr(access, "net_arg", lambda v: "5")
    monkeypatch.setattr(access, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(access, "gateway_elsewhere", lambda n: "another host")
    monkeypatch.setattr(access, "net_name", lambda n: "lab5")
    with pytest.raises(LabError, match="gateway is on another host"):
        access.net_switch_args(["--net", "lab5"])


def test_move_counts_an_owner_per_net(monkeypatch):
    from pvelab.commands import move
    assert move.whose({("2", "alice01"): [1], ("3", "alice01"): [2]}) == "2 owner(s)"


# --- /code-review round 17 ---

def test_gateway_elsewhere_needs_the_vnet_in_pve(monkeypatch):
    from pvelab import ifaces
    monkeypatch.setattr(ifaces, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(ifaces.config, "bridge", lambda n: f"lab{n}")
    monkeypatch.setattr(ifaces.config, "sdn_vnet_ids", lambda: {"lab5"})
    assert ifaces.gateway_elsewhere("5") and not ifaces.gateway_elsewhere("99")


def test_isolate_names_the_gateway_host_when_known(monkeypatch):
    from pvelab.commands import access
    from pvelab.util import LabError
    monkeypatch.setattr(access, "net_arg", lambda v: "5")
    monkeypatch.setattr(access, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(access, "gateway_elsewhere", lambda n: "workshop2")
    monkeypatch.setattr(access, "net_name", lambda n: "lab5")
    with pytest.raises(LabError, match="gateway is on workshop2"):
        access.net_switch_args(["--net", "lab5"])


def test_routine_rebuilds_do_not_warn_about_offline_hosts(monkeypatch, capsys, tmp_path):
    from pvelab import isolation
    monkeypatch.setattr(isolation, "multi", lambda: True)
    monkeypatch.setattr(isolation, "is_sdn", lambda n: True)
    monkeypatch.setattr(isolation, "net_vms", lambda n: {})
    monkeypatch.setattr(isolation, "shared_ip", lambda n, confs=None: None)
    monkeypatch.setattr(isolation, "NFT_DIR", str(tmp_path))
    monkeypatch.setattr(isolation, "nft_path", lambda n: str(tmp_path / "t.nft"))
    monkeypatch.setattr(isolation, "write_section", lambda *a: None)
    monkeypatch.setattr(isolation, "iptables", lambda r: ([], []))
    monkeypatch.setattr(isolation, "other_nodes", lambda: [])
    monkeypatch.setattr(isolation, "offline_nodes", lambda: ["pve3"])
    monkeypatch.setattr(isolation.nets, "net_name", lambda n: "web")
    isolation.set_isolation("5")
    assert capsys.readouterr().err == ""
    isolation.set_isolation("5", tell_offline=True)
    err = capsys.readouterr().err
    assert "pve3 is offline" in err and "--net web" in err and "old or none" in err


# --- /code-review round 18 ---

def test_gateway_elsewhere_finds_pvelabs_vnet_without_sdn_loaded_here(monkeypatch):
    from pvelab import ifaces
    monkeypatch.setattr(ifaces, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(ifaces.config, "bridge", lambda n: f"vmbr{n}")          # no SDN loaded on this host
    monkeypatch.setattr(ifaces.config, "sdn_vnet_ids", lambda: {"lab5"})
    monkeypatch.setattr(ifaces.config, "taken_entry", lambda n: None)
    assert ifaces.gateway_elsewhere("5")


def test_gateway_elsewhere_names_a_recorded_host_never_this_one(monkeypatch):
    from pvelab import ifaces
    monkeypatch.setattr(ifaces, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(ifaces, "this_node", lambda: "workshop1")
    monkeypatch.setattr(ifaces.config, "taken_entry", lambda n: ("vnet1", {"net": 9, "host": "workshop2"}))
    assert ifaces.gateway_elsewhere("9") == "workshop2"
    monkeypatch.setattr(ifaces.config, "taken_entry", lambda n: ("vnet1", {"net": 9}))
    assert ifaces.gateway_elsewhere("9") == "another host"


def test_owner_rebuild_tells_offline_hosts(monkeypatch):
    from pvelab.commands import owner
    seen = []
    monkeypatch.setattr(owner, "is_isolated", lambda n: True)
    monkeypatch.setattr(owner, "set_isolation", lambda n, confs=None, tell_offline=False: seen.append(tell_offline))
    owner.refresh_isolation({"5"}, {}, {"5"})
    assert seen == [True]


# --- /code-review round 19 ---

def test_a_take_over_recorded_here_without_its_block_is_not_elsewhere(monkeypatch):
    from pvelab import ifaces
    monkeypatch.setattr(ifaces, "bridge_is_ours", lambda n: False)
    monkeypatch.setattr(ifaces.config, "sdn_vnet_ids", lambda: {"vnet1"})
    monkeypatch.setattr(ifaces.config, "taken_entry", lambda n: ("vnet1", {"net": 5, "host": "workshop1"}))
    monkeypatch.setattr(ifaces, "this_node", lambda: "workshop1")
    assert not ifaces.gateway_elsewhere("5")


def test_one_failed_rebuild_does_not_stop_the_others(monkeypatch, capsys):
    from pvelab.commands import owner
    from pvelab.util import LabError
    done = []
    monkeypatch.setattr(owner, "is_isolated", lambda n: True)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: f"net{n}")

    def rebuild(n, confs=None, tell_offline=False):
        if n == "2":
            raise LabError("nft failed")
        done.append(n)
    monkeypatch.setattr(owner, "set_isolation", rebuild)
    owner.refresh_isolation({"2", "7"}, {}, {"2", "7"})
    assert done == ["7"] and "pvelab isolate --net net2" in capsys.readouterr().err


def test_owner_again_rebuilds_isolation(monkeypatch):
    owner, ran = owner_cmd(monkeypatch, {204: _managed("net2-user04", "2", " owner=user04")})
    seen = []
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv, changed: seen.append(set(nets)))
    owner.cmd_owner(["net2-user04", "user04"])
    assert ran == [] and seen == [{"2"}]


# --- /code-review round 20 ---

def test_rebuild_errors_of_any_kind_leave_the_other_nets(monkeypatch, capsys):
    from pvelab.commands import owner
    done = []
    monkeypatch.setattr(owner, "is_isolated", lambda n: True)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: f"net{n}")

    def rebuild(n, confs=None, tell_offline=False):
        if n == "2":
            raise OSError("disk full")
        done.append(n)
    monkeypatch.setattr(owner, "set_isolation", rebuild)
    owner.refresh_isolation({"2", "7"}, {}, {"2", "7"})
    assert done == ["7"] and "not rebuilt everywhere" in capsys.readouterr().err


def test_gateway_hint_only_for_nets_whose_owners_changed(monkeypatch, capsys):
    from pvelab.commands import owner
    monkeypatch.setattr(owner, "is_isolated", lambda n: False)
    monkeypatch.setattr(owner.nets, "net_name", lambda n: "net5")
    monkeypatch.setattr(owner, "gateway_elsewhere", lambda n: "workshop2")
    owner.refresh_isolation({"5"}, {}, set())
    assert capsys.readouterr().err == ""
