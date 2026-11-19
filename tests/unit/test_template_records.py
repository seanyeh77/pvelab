"""What pvelab knows of its templates (built, a copy, saved) lives in the registry, not their description:
read from the record first, from the description of a template made before; migrate moves it over (test_migrate)."""
import time

from pvelab import registry, templates

WANT = "abcdef0123456789" + "0" * 48


def building(monkeypatch, conf):
    """build_template with qm faked: conf is what the template's VMID holds (None: nothing), and becomes the
    new template's config (ctime 7) when qm create runs; returns the qm subcommands' argv."""
    t = templates.catalogue_find("ubuntu24")
    state = {"conf": conf}
    calls = []

    def run(*a, **k):
        calls.append(a)
        if a[1] == "create":
            state["conf"] = {"template": "1", "name": t.name, "meta": "ctime=7", "_vmid": t.vmid}
        return ""
    monkeypatch.setattr(templates, "read_conf", lambda v: state["conf"])
    monkeypatch.setattr(templates, "vm_exists", lambda v: state["conf"] is not None)
    monkeypatch.setattr(templates, "linked_clones", lambda *ids: {v: {} for v in ids})
    monkeypatch.setattr(templates, "fetch_image", lambda t, want: "/x.img")
    monkeypatch.setattr(templates, "fit_bridge", lambda *a: None)
    monkeypatch.setattr(templates, "run", run)
    return t, state, calls


def test_a_built_template_has_no_description_and_is_found_up_to_date(monkeypatch, capsys):
    t, state, calls = building(monkeypatch, None)
    templates.build_template(t, WANT)
    [create] = [a for a in calls if a[1] == "create"]
    assert "--description" not in create
    rec = registry.load()["templates"][str(t.vmid)]
    assert rec == {"ctime": 7, "kind": "built", "image": "abcdef0123456789", "built": time.strftime("%Y-%m-%d")}
    calls.clear()
    templates.build_template(t, WANT)                       # pulled again: the record says it is current
    assert calls == [] and "up to date" in capsys.readouterr().out
    assert templates.stamp(state["conf"]) == "image=abcdef0123456789"
    assert templates.template_status(t, {t.vmid: state["conf"]}).startswith("on this node, built ")


def test_a_newer_image_rebuilds_a_recorded_template(monkeypatch):
    t, state, calls = building(monkeypatch, None)
    templates.build_template(t, WANT)
    calls.clear()
    templates.build_template(t, "f" * 64)
    assert [a[1] for a in calls if a[1] in ("destroy", "create")] == ["destroy", "create"]
    assert registry.load()["templates"][str(t.vmid)]["image"] == "f" * 16


def test_the_record_wins_over_the_description():
    conf = {"template": "1", "name": "debian-13-cloud", "meta": "ctime=3", "_vmid": 9100,
            "description": "copy of 9002 debian-13-cloud image=0000000000000000"}
    registry.update({9100: {"kind": "copy", "orig": 9003, "stamp": "image=1111111111111111"}}, {9100: conf},
                    "templates")
    assert templates.source_of(conf) == 9003 and templates.stamp(conf) == "image=1111111111111111"


def test_a_saved_template_is_known_by_its_record():
    conf = {"template": "1", "name": "docker-lab", "meta": "ctime=4", "_vmid": 9100, "description": ""}
    assert not templates.is_saved(conf)
    registry.update({9100: {"kind": "saved", "from": "net30-student01 (VMID 201)", "built": "2026-11-20"}},
                    {9100: conf}, "templates")
    assert templates.is_saved(conf) and templates.stamp(conf) == "built=2026-11-20"
    assert templates.saved_from(conf) == "net30-student01 (VMID 201) built=2026-11-20"


def test_a_template_made_before_is_read_from_its_description():
    saved = {"template": "1", "description": "saved by pvelab from x (VMID 201) built=2026-10-01"}
    assert templates.is_saved(saved) and templates.saved_from(saved) == "x (VMID 201) built=2026-10-01"
    copy = {"template": "1", "description": "copy of 9100 docker-lab built=2026-10-01"}
    assert templates.source_of(copy) == 9100 and templates.stamp(copy) == "built=2026-10-01"


def test_save_template_records_it_and_writes_no_description(monkeypatch):
    from test_ux import saving
    tpl, calls = saving(monkeypatch)
    sets = []
    real = tpl.run
    monkeypatch.setattr(tpl, "run", lambda *a, **k: (sets.append(a) if a[1] == "set" else None) or real(*a, **k))
    tpl.save_template(201, "docker-lab")
    assert not any("--description" in a for a in sets)
    [rec] = registry.load()["templates"].values()
    assert rec["kind"] == "saved" and rec["from"] == "net30-student01 (VMID 201)"
    assert rec["built"] == time.strftime("%Y-%m-%d")
