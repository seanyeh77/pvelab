"""A template's net0: a bridge its host has, so a VM cloned from it by hand (PVE GUI) starts."""
from pvelab import templates


def test_pvelabs_dhcp_bridge_when_the_host_has_it(monkeypatch):
    monkeypatch.setattr(templates, "host_bridges", lambda node=None: ["vmbr0", "vmbr1"])
    assert templates.template_bridge() == "vmbr1"


def test_the_uplink_when_there_is_no_vmbr1(monkeypatch):
    monkeypatch.setattr(templates, "host_bridges", lambda node=None: ["vmbr0", "vmbr5"])
    assert templates.template_bridge() == "vmbr0"


def test_any_bridge_when_there_is_neither(monkeypatch):
    monkeypatch.setattr(templates, "host_bridges", lambda node=None: ["vmbr7", "lab3"])
    assert templates.template_bridge() == "vmbr7"


def test_vmbr1_when_the_host_has_no_bridge_at_all(monkeypatch):
    monkeypatch.setattr(templates, "host_bridges", lambda node=None: [])
    assert templates.template_bridge() == "vmbr1"          # create makes it


def test_a_copy_gets_a_bridge_of_its_new_host(monkeypatch):
    ran = []
    monkeypatch.setattr(templates, "host_bridges", lambda node=None: ["vmbr0"] if node == "pve2" else ["vmbr1"])
    monkeypatch.setattr(templates, "run", lambda *a, **k: ran.append(a) or "")
    monkeypatch.setattr(templates, "on_node", lambda node, *a: (node, *a))
    templates.fit_bridge(9105, "pve2", "virtio=BC:24:11:00:00:01,bridge=vmbr1")
    assert ran == [("pve2", "qm", "set", 9105, "--net0", "virtio=BC:24:11:00:00:01,bridge=vmbr0")]
    ran.clear()
    templates.fit_bridge(9105, "pve2", "virtio,bridge=vmbr0")
    assert ran == []                                         # its bridge is there: left alone
