"""Building templates from cloud images (download, checksum, qm create)."""

import glob
import hashlib
import os
import re
import time

from .catalogue import TEMPLATES
from .config import (DHCP_BRIDGE, GUEST_USER, IMG_DIR, PW_MINLEN, READY_TIMEOUT,
                     SAVED_ID_START, SNIPPET, STORAGE, TPL_DISK)
from .guest import guest_json
from .inventory import desc_value, inventory, read_conf, vm_exists, vm_status
from .lifecycle import first_boot_status
from .ui import some
from .util import die, file_hash, http_open, LabError, run, warn, write_file


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
    write_file(f"/var/lib/vz/snippets/{SNIPPET}", vendor_data())


def latest_sum(t):
    """The checksum of the distribution's newest t.image (from its published checksum file)."""
    try:
        sums = http_open(f"{t.base}/{t.sums}").read().decode()
    except OSError as e:
        die(f"cannot fetch {t.base}/{t.sums}: {e}")
    want = next((p[0].lower() for p in (l.split() for l in sums.splitlines())
                 if len(p) == 2 and p[1].lstrip("*") == t.image), None)
    if not want:
        die(f"{t.image} not listed in {t.base}/{t.sums}")
    return want


def fetch_image(t, want):
    """Download t.image with checksum want into IMG_DIR (a cached copy that matches is reused);
    return its path."""
    algo = "sha512" if "512" in t.sums else "sha256"
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


def image_tag(want):
    """How a template's description records the image it was built from."""
    return f"image={want[:16]}"


def template_state(t, conf):
    """Catalogue template t at its VMID, given that VMID's config on this node (or None):
    "ours" (pvelab's template), "foreign" (some other VM), "elsewhere" (another node) or "missing"."""
    if conf is None:
        return "elsewhere" if vm_exists(t.vmid) else "missing"
    return "ours" if conf.get("template") == "1" and conf.get("name") == t.name else "foreign"


def template_status(t, inv):
    """What `templates` and the menu say about t. inv: inventory()."""
    conf = inv.get(t.vmid)
    state = template_state(t, conf)
    if state == "ours":
        day = desc_value(conf, "built")
        return "on this node" + (f", built {day}" if day else "")
    return {"foreign": "VMID in use by something else!", "elsewhere": "VMID in use on another node!",
            "missing": f"not built (pvelab pull {t.short})"}[state]


def linked_clones(vmid):
    """{vmid: name} of the guests whose disks still need template vmid's disks. On ZFS, Ceph and
    directory (qcow2) storage a linked clone's volume names its base (base-9003-disk-0/vm-201-...),
    and PVE won't delete that base. On LVM-thin clones are independent of it and don't name it."""
    base = re.compile(rf"base-{vmid}-disk-\d+[^,\s]*/")
    out = {}         # not inventory(): clones on any node count, and disks only an old snapshot still uses
    for f in glob.glob("/etc/pve/nodes/*/qemu-server/*.conf"):
        text = open(f).read()
        if base.search(text):
            m = re.search(r"^name: (\S+)", text, re.M)
            out[int(os.path.basename(f)[:-5])] = m.group(1) if m else "?"
    return dict(sorted(out.items()))


def build_template(t, want=None):
    """Build template t from the newest cloud image; if it is there already, rebuild it only when a
    newer image is out and nothing still needs the old one's disks (linked clones on ZFS, Ceph,
    qcow2). VMs cloned on LVM-thin keep working: their disks don't need the template's.
    want: the newest image's checksum, if the caller has it already (latest_sum)."""
    conf = read_conf(t.vmid)
    state = template_state(t, conf)
    if state == "elsewhere":
        die(f"VMID {t.vmid} ({t.name}) is used on another node")
    if state == "foreign":
        die(f"VMID {t.vmid} is '{conf.get('name', '?')}', not pvelab's {t.name} template - left alone")
    want = want or latest_sum(t)
    if state == "ours":
        if image_tag(want) in conf["description"]:
            print(f">> {t.vmid} ({t.name}) is up to date")
            return
        clones = linked_clones(t.vmid)
        if clones:
            warn(f"{t.name}: a newer image is out, but {len(clones)} VM(s) are linked clones that still need "
                 f"this template's disks ({some(clones.values(), 5, ' ')}) - not updated; it can be once "
                 "they are destroyed")
            return
    ensure_snippet()
    img = fetch_image(t, want)
    if state == "ours":
        print(f">> {t.vmid} ({t.name}): a newer image is out - replacing the template "
              "(VMs made from it stay as they are)")
        run("qm", "destroy", t.vmid, "--purge", "1")
    kvm, cpu = ("1", "host") if os.path.exists("/dev/kvm") else ("0", "x86-64-v2-AES")
    if kvm == "0":
        warn("/dev/kvm missing: nested virtualization is off, template uses --kvm 0 (slow)")
    print(f">> {t.vmid} ({t.name}): creating template")
    try:
        run("qm", "create", t.vmid, "--name", t.name, "--ostype", "l26",
            "--description", f"built by pvelab from {t.base}/{t.image} {image_tag(want)} "
                             f"built={time.strftime('%Y-%m-%d')}",
            "--memory", 2048, "--cores", 2, "--cpu", cpu, "--kvm", kvm,
            "--net0", f"virtio,bridge={DHCP_BRIDGE}",   # need not exist: templates never boot, create
                                                       # sets (and makes) each clone's bridge
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
        if template_state(t, read_conf(t.vmid)) == "missing":
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


SAVED = "saved by pvelab from"             # description of templates made with save-template

# Run in the copy before it becomes a template: what makes a machine itself goes, so that every
# clone boots as a new one (cloud-init sets its name, password, keys and IP again).
FORGET = """set -e
cloud-init clean --logs --seed
truncate -s 0 /etc/machine-id
rm -f /etc/ssh/ssh_host_*
rm -f /root/.bash_history /home/*/.bash_history
apt-get clean >/dev/null 2>&1 || true
journalctl --rotate >/dev/null 2>&1 && journalctl --vacuum-time=1s >/dev/null 2>&1 || true
fstrim -a >/dev/null 2>&1 || true
"""


def is_saved(conf):
    return bool(conf) and conf.get("template") == "1" and conf.get("description", "").startswith(SAVED)


def check_template_name(name, inv):
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name):
        die(f"template name '{name}': letters, digits and - only (like docker-lab)")
    if catalogue_find(name):
        die(f"'{name}' is a catalogue template's name - pick another")
    if any(c.get("name", "").lower() == name.lower() for c in inv.values()):
        die(f"a VM or template named '{name}' exists already")


def wait_for(what, test, limit):
    t0 = time.time()
    while not test():
        if time.time() - t0 > limit:
            die(f"{what}: still not ready after {limit // 60} min")
        time.sleep(5)


def save_template(src, name):
    """A new template NAME with VM src's disks as they are now; src itself is not changed."""
    conf = read_conf(src)
    vmid = next(i for i in range(SAVED_ID_START, SAVED_ID_START + 900) if not vm_exists(i))
    bridge = re.search(r"bridge=([^,]+)", conf.get("net0", "")).group(1)
    print(f">> copying {conf['name']} (VMID {src}) to {vmid} '{name}' - a full copy, a few minutes")
    run("qm", "clone", src, vmid, "--name", name, "--full", "1", capture=False)
    try:
        run("qm", "set", vmid, "--delete", "tags,cipassword", "--onboot", "0", "--ipconfig0", "ip=dhcp",
            "--net0", f"virtio,bridge={bridge},link_down=1",       # no network: src's IP is in use
            "--description", f"{SAVED} {conf['name']} (VMID {src}) built={time.strftime('%Y-%m-%d')}")
        print(f">> {name}: booting the copy once (no network) to make it forget who it was")
        run("qm", "start", vmid)
        wait_for("its boot", lambda: not (first_boot_status(vmid) or "running").startswith(
            ("running", "not started")), READY_TIMEOUT)
        r = guest_json("exec", vmid, "--timeout", 300, "--", "sh", "-c", FORGET, timeout=330)
        if not r or r.get("exitcode") != 0:
            die(f"{name}: clean-up in the copy failed: {(r or {}).get('err-data', 'no answer')}".strip())
        run("qm", "shutdown", vmid, "--timeout", 120, check=False)
        if vm_status(vmid) != "stopped":
            run("qm", "stop", vmid)
        run("qm", "set", vmid, "--net0", f"virtio,bridge={DHCP_BRIDGE}")   # create sets each clone's
        run("qm", "template", vmid)
    except LabError:
        c = read_conf(vmid)               # our half-made copy: remove it so a retry starts clean
        if c is not None and c.get("template") != "1" and c.get("name") == name:
            run("qm", "stop", vmid, check=False)
            run("qm", "destroy", vmid, "--purge", "1", check=False)
        raise
    print(f">> template '{name}' ({vmid}) ready - use it: pvelab create 20 {name} --net N")


def delete_template(name):
    inv = inventory()
    vmid = next((i for i, c in inv.items() if is_saved(c) and c.get("name", "").lower() == name.lower()), None)
    if vmid is None:
        die(f"no template '{name}' made by save-template (see: pvelab templates)")
    clones = linked_clones(vmid)
    if clones:
        die(f"{len(clones)} VM(s) are linked clones that still need its disks: {some(clones.values(), 5, ' ')}")
    run("qm", "destroy", vmid, "--purge", "1")
    print(f">> template '{name}' ({vmid}) deleted - VMs made from it stay as they are")
