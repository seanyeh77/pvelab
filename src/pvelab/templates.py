"""Building templates from cloud images (download, checksum, qm create)."""

import hashlib
import os
import re

from .catalogue import TEMPLATES
from .config import DHCP_BRIDGE, GUEST_USER, IMG_DIR, PW_MINLEN, SNIPPET, STORAGE, TPL_DISK
from .inventory import inventory, read_conf, vm_exists
from .networks import ensure_dhcp_bridge
from .util import die, file_hash, http_open, LabError, run, warn


def catalogue_find(arg):
    a = str(arg).lower()
    return next((t for t in TEMPLATES if a in (t.name.lower(), str(t.vmid), *t.aliases)), None)


def node_templates(inv=None):
    """{vmid: name} of templates on this node. inv: an inventory() the caller already read."""
    return {i: c.get("name", "") for i, c in (inv or inventory()).items() if c.get("template") == "1"}


def vendor_data():
    """Vendor-data for every clone (PVE's own user-data still applies on top):
    - qemu-guest-agent, and SSH password login (cloud images disable it)
    - only GUEST_USER may log in over ssh: accounts students make in class (adduser bob) with
      weak passwords are what botnets guess; they still work locally (su - bob)
    - pam_pwquality refuses short/dictionary passwords, for root (sudo passwd bob) too"""
    return ("#cloud-config\nssh_pwauth: true\npackage_update: true\n"
            "packages: [qemu-guest-agent, libpam-pwquality]\n"
            "write_files:\n"
            "  - path: /etc/ssh/sshd_config.d/10-pvelab.conf\n"
            f"    content: \"AllowUsers {GUEST_USER}\\n\"\n"
            "  - path: /etc/security/pwquality.conf.d/pvelab.conf\n"
            f"    content: \"minlen = {PW_MINLEN}\\nenforce_for_root\\n\"\n"
            "runcmd:\n  - [sh, -c, \"systemctl start qemu-guest-agent || true\"]\n"
            "  - [sh, -c, \"systemctl reload ssh || systemctl reload sshd || true\"]\n")


def ensure_snippet():
    """Write vendor_data() to local:snippets (templates point their --cicustom vendor= at it)."""
    cfg = open("/etc/pve/storage.cfg").read()
    m = re.search(r"^dir: local\n((?:[ \t]+.*\n?)*)", cfg, re.M)
    content = re.search(r"^\s+content\s+(\S+)", m.group(1), re.M) if m else None
    cur = content.group(1).split(",") if content else []
    if "snippets" not in cur:
        run("pvesm", "set", "local", "--content", ",".join(cur + ["snippets"]))
    os.makedirs("/var/lib/vz/snippets", exist_ok=True)
    path = f"/var/lib/vz/snippets/{SNIPPET}"
    try:
        if open(path).read() == vendor_data():
            return
    except OSError:
        pass
    with open(path, "w") as f:
        f.write(vendor_data())
    os.chmod(path, 0o644)


def fetch_image(t):
    """Download t.image into IMG_DIR (reused if the checksum still matches); return its path."""
    algo = "sha512" if "512" in t.sums else "sha256"
    try:
        sums = http_open(f"{t.base}/{t.sums}").read().decode()
    except OSError as e:
        die(f"cannot fetch {t.base}/{t.sums}: {e}")
    want = next((p[0].lower() for p in (l.split() for l in sums.splitlines())
                 if len(p) == 2 and p[1].lstrip("*") == t.image), None)
    if not want:
        die(f"{t.image} not listed in {t.base}/{t.sums}")
    os.makedirs(IMG_DIR, exist_ok=True)
    path = f"{IMG_DIR}/{t.image}"
    if os.path.exists(path) and file_hash(path, algo) == want:
        print(f">> {t.image}: cached copy is current")
        return path
    print(f">> {t.name}: downloading {t.base}/{t.image}")
    h, part = hashlib.new(algo), path + ".part"
    try:
        with http_open(f"{t.base}/{t.image}") as r, open(part, "wb") as f:
            total, done, shown = int(r.headers.get("Content-Length") or 0), 0, -10
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total and done * 100 // total >= shown + 10:
                    shown = done * 100 // total
                    print(f"   {shown:3d}% of {total >> 20} MB")
    except OSError as e:
        die(f"download failed: {e}")
    if h.hexdigest() != want:
        os.remove(part)
        die(f"{t.image}: checksum mismatch")
    os.replace(part, path)
    os.chmod(path, 0o644)
    return path


def build_template(t):
    if vm_exists(t.vmid):
        print(f">> {t.vmid} ({t.name}) already exists, skipping (qm destroy {t.vmid} to rebuild)")
        return
    ensure_dhcp_bridge()
    ensure_snippet()
    img = fetch_image(t)
    kvm, cpu = ("1", "host") if os.path.exists("/dev/kvm") else ("0", "x86-64-v2-AES")
    if kvm == "0":
        warn("/dev/kvm missing: nested virtualization is off, template uses --kvm 0 (slow)")
    print(f">> {t.vmid} ({t.name}): creating template")
    try:
        run("qm", "create", t.vmid, "--name", t.name, "--ostype", "l26",
            "--description", f"built by pvelab from {t.base}/{t.image}",
            "--memory", 2048, "--cores", 2, "--cpu", cpu, "--kvm", kvm,
            "--net0", f"virtio,bridge={DHCP_BRIDGE}",
            "--scsihw", "virtio-scsi-single",
            "--scsi0", f"{STORAGE}:0,import-from={img},discard=on,ssd=1",
            "--scsi1", f"{STORAGE}:cloudinit",
            "--boot", "order=scsi0", "--serial0", "socket", "--vga", "serial0",
            "--agent", "enabled=1", "--ipconfig0", "ip=dhcp",
            "--cicustom", f"vendor=local:snippets/{SNIPPET}", capture=False)
        run("qm", "disk", "resize", t.vmid, "scsi0", TPL_DISK)
        run("qm", "template", t.vmid)
    except LabError:
        c = read_conf(t.vmid)             # our half-built VM: remove it so a retry starts clean
        if c is not None and c.get("template") != "1" and c.get("name") == t.name:
            run("qm", "destroy", t.vmid, "--purge", "1", check=False)
        raise
    print(f">> {t.vmid} ({t.name}) template ready")


def check_template(vmid):
    c = read_conf(vmid)
    if c is None:
        die(f"template {vmid} does not exist on this node")
    if c.get("template") != "1":
        die(f"VM {vmid} is not a template")


def resolve_template(arg):
    """VMID, template name or alias -> VMID; builds a catalogue template if missing."""
    t = catalogue_find(arg)
    if t:
        if not vm_exists(t.vmid):
            print(f">> template {t.name} ({t.vmid}) is not on this node yet - building it")
            build_template(t)
        vmid = t.vmid
    elif str(arg).isdigit():
        vmid = int(arg)
    else:
        a = str(arg).lower()
        vmid = next((i for i, n in node_templates().items() if n.lower() == a), None)
        if vmid is None:
            die(f"unknown template '{arg}' (see: pvelab templates)")
    check_template(vmid)
    return vmid


def template_label(vmid, on_node):
    """Name of template vmid; on_node: node_templates()."""
    t = catalogue_find(vmid)
    return t.name if t else on_node.get(vmid, "?")
