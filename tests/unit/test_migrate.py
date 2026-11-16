"""Existing managed VMs get owner=/role=shared and the pvelab;net-NAME tags once, without a reboot."""
import pytest

from pvelab import migrate


def vm(name, net="2", extra="", tags="lab-student"):
    return {"name": name, "description": f"managed-by=lab.sh template=9000 net={net}{extra}", "tags": tags,
            "_node": "pve1"}


@pytest.fixture(autouse=True)
def names(monkeypatch):
    monkeypatch.setattr(migrate.nets, "net_name", lambda n: f"net{n}")
    monkeypatch.setattr(migrate.nets, "known", lambda n: True)
    monkeypatch.setattr("pvelab.owners.nets.net_name", lambda n: f"net{n}")
    monkeypatch.setattr("pvelab.lifecycle.nets.net_name", lambda n: f"net{n}")


def test_plan_derives_owner_from_old_names():
    todo, problems = migrate.plan({201: vm("net2-student03-2"), 202: vm("net2-student04")})
    assert todo[201] == ("managed-by=lab.sh template=9000 net=2 owner=student03", "lab-student;pvelab;net-net2")
    assert todo[202][0].endswith("owner=student04") and problems == []


def test_plan_marks_the_old_shared_vm():
    todo, _ = migrate.plan({210: vm("net2-shared")})
    assert todo[210][0].endswith("net=2 role=shared")


def test_plan_rewrites_legacy_student_token():
    todo, _ = migrate.plan({203: vm("alice-db", extra=" student=net2-alice")})
    assert todo[203][0] == "managed-by=lab.sh template=9000 net=2 owner=alice"


def test_plan_reports_a_vm_whose_name_does_not_tell():
    todo, problems = migrate.plan({204: vm("web01")})
    assert problems == ["web01"]
    assert todo[204][0].endswith("net=2 owner=web01")           # its own owner, as pvelab grouped it before


def test_plan_leaves_done_and_unmanaged_vms():
    done = vm("net2-user01", extra=" owner=user01", tags="pvelab;net-net2")
    todo, problems = migrate.plan({205: done, 206: {"name": "mine", "description": "x", "tags": ""}})
    assert todo == {} and problems == []


def test_plan_keeps_other_tags():
    todo, _ = migrate.plan({207: vm("x", extra=" owner=a", tags="pvelab;net-old;web")})
    assert todo[207][1] == "pvelab;net-old;web;net-net2"           # an admin's net-old is theirs


def test_ensure_is_silent_when_done(monkeypatch, capsys):
    monkeypatch.setattr(migrate, "inventory", lambda: {205: vm("net2-user01", extra=" owner=user01",
                                                                tags="pvelab;net-net2")})
    monkeypatch.setattr(migrate, "run", lambda *a, **k: pytest.fail("ran qm"))
    migrate.ensure(False)
    assert capsys.readouterr().out == ""


def test_ensure_sets_description_and_tags(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(migrate, "inventory", lambda: {201: vm("net2-student01"), 204: vm("web01")})
    monkeypatch.setattr(migrate, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(migrate, "on_node", lambda node, *a: a)
    migrate.ensure(True)
    assert ran == [] and "dry run" in capsys.readouterr().out
    migrate.ensure(False)
    assert sorted(a[a.index("set") + 1] for a in ran) == [201, 204]
    out = capsys.readouterr()
    assert "recording owners in 2 VM(s)" in out.out and "name alone: web01" in out.out


def test_owner_command_sets_the_token(monkeypatch):
    from pvelab.commands import owner
    ran = []
    monkeypatch.setattr(owner, "targets", lambda a: [204])
    monkeypatch.setattr(owner, "read_conf", lambda i: vm("web01"))
    monkeypatch.setattr(owner, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(owner, "on_node", lambda node, *a: a)
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None: None)
    owner.cmd_owner(["web01", "alice"])
    assert ran[0][-1].endswith("net=2 owner=alice")


def test_owner_check_changes_nothing(monkeypatch, capsys):
    from pvelab.commands import owner
    monkeypatch.setattr(migrate, "inventory", lambda: {201: vm("net2-student01"), 204: vm("web01")})
    monkeypatch.setattr(owner, "inventory", lambda: {201: vm("net2-student01"), 204: vm("web01")})
    monkeypatch.setattr(migrate, "run", lambda *a, **k: pytest.fail("ran qm"))
    owner.cmd_owner_check(["--check"])          # as cli passes it: the subcommand first
    out = capsys.readouterr().out
    assert "net2-student01" in out and "student01" in out and "web01" in out and "pvelab owner web01" in out


def test_owner_warns_when_an_owner_spans_hosts(monkeypatch, capsys):
    from pvelab.commands import owner
    monkeypatch.setattr(owner, "targets", lambda a: [204])
    monkeypatch.setattr(owner, "read_conf", lambda i: vm("web01"))
    monkeypatch.setattr(owner, "run", lambda *a, **k: None)
    monkeypatch.setattr(owner, "on_node", lambda node, *a: a)
    monkeypatch.setattr(owner, "refresh_isolation", lambda nets, inv=None: None)
    monkeypatch.setattr(owner, "inventory", lambda: {
        204: {**vm("web01", extra=" owner=alice"), "_node": "pve1"},
        205: {**vm("net2-alice-db", extra=" owner=alice"), "_node": "pve2"}})
    owner.cmd_owner(["web01", "alice"])
    assert "alice's VMs on net2 are on 2 hosts" in capsys.readouterr().err
