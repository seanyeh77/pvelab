"""Building templates from cloud images (download, checksum, qm create)."""

import glob
import hashlib
import os
import re
import time

from .catalogue import TEMPLATES
from .config import (DHCP_BRIDGE, UPLINK, HOST_LABEL, IMG_DIR, MAX_VMID, NODES_DIR, READY_TIMEOUT, SAVED_ID_START,
                     SNIPPET, SNIPPET_DIR, STORAGE, STORAGE_CFG, TPL_DISK)
from .guest import guest_json
from .inventory import inventory, read_conf, vm_exists, vm_status
from .lifecycle import booting, first_boot_state
from .nodes import is_local, node_of, on_node, used_vmids, write_on
from .resources import check_copy_space, conf_disk_gb, confirm
from . import registry, ui
from .ui import some, Spinner
from .util import confirm_yes, die, file_hash, http_open, LabError, number, parallel, pipe, run, wait_for, warn


def catalogue_find(arg):
    a = str(arg).lower()
    return next((t for t in TEMPLATES if a in (t.name.lower(), str(t.vmid), *t.aliases)), None)


# What a template's description said before the registry (pvelab writes none now; described() reads them)
COPY = "copy of"                      # a template's copy on another node: copy of <VMID> <name> <stamp>
SAVED = "saved by pvelab from"        # save-template's: saved by pvelab from <name> (VMID <n>) built=<day>
BUILT = re.compile(r"built by \S+ from ")     # pull's (lab.py's before): built by pvelab from <url> image=<16 hex> built=<day>


def described(desc):
    """The record a template's description held before the registry ({} when it is not pvelab's text):
    kind ("built" | "copy" | "saved"), image, built, orig (a copy's original), stamp (a copy's), from (saved)."""
    d = desc or ""
    first = d.split("\n", 1)[0]
    img, day = re.search(r"\bimage=([0-9a-f]{16})\b", d), re.search(r"\bbuilt=(\S+)", d)
    if m := re.match(rf"{COPY} (\d+)\b", d):
        return {"kind": "copy", "orig": int(m.group(1)),
                "stamp": f"image={img.group(1)}" if img else f"built={day.group(1)}" if day else ""}
    rec = {}
    if first.startswith(SAVED):
        rec = {"kind": "saved", "from": re.sub(r"\s*\bbuilt=\S+", "", first.removeprefix(SAVED)).strip()}
    elif BUILT.match(first):
        rec = {"kind": "built"}
    if img:
        rec["image"] = img.group(1)
    if day:
        rec["built"] = day.group(1)
    return rec


def template_facts(conf):
    """What pvelab knows of template conf: its record (registry), else (made before) what its description
    said (described)."""
    if (conf or {}).get("_vmid") is not None:
        rec = registry.lookup(conf["_vmid"], conf, "templates")
        if rec is not None:
            return rec
    return described((conf or {}).get("description"))


def record_template(vmid, fields):
    """Template vmid's record written (its config read now: the ctime)."""
    registry.update({vmid: fields}, {vmid: read_conf(vmid)}, "templates")


def source_of(conf):
    """The VMID of the template that conf's template is a copy of, or None (not a copy)."""
    f = template_facts(conf)
    return int(f["orig"]) if f.get("kind") == "copy" and f.get("orig") is not None else None


def node_templates(inv=None):
    """{vmid: name} of the templates (originals, not their copies) on any node.
    inv: an inventory() the caller already read."""
    return {i: c.get("name", "") for i, c in (inv or inventory()).items()
            if c.get("template") == "1" and source_of(c) is None}


def vendor_data():
    """Vendor-data for every clone (PVE's own user-data still applies on top). It only makes a
    cloud image usable and restricts nothing: SSH password login (cloud images disable it) and
    qemu-guest-agent. Restrictions are library scripts now (pvelab scripts harden).
    ssh_deletekeys false: a VM keeps its SSH host keys when cloud-init runs again (a changed name or
    address gives it a new instance id); a new clone still gets its own, as templates have none (cloud
    images ship none; save-template removes them)."""
    return ("#cloud-config\nssh_pwauth: true\nssh_deletekeys: false\npackage_update: true\n"
            "packages: [qemu-guest-agent]\n"
            "runcmd:\n  - [sh, -c, \"systemctl start qemu-guest-agent || true\"]\n"
            f"  - [sh, -c, \"{ROOT_SSH}\"]\n")


# root logs in over ssh with its password when it has one (a VM made with --user root; cloud images allow
# root keys only): run by cloud-init on every new instance, so after a rollback --initial too
ROOT_SSH = ("passwd -S root 2>/dev/null | grep -q ' P ' && printf 'PermitRootLogin yes\\\\n' > "
            "/etc/ssh/sshd_config.d/10-pvelab-root.conf && (systemctl reload ssh || systemctl reload sshd) || true")


def ensure_snippet(node=None):
    """Write vendor_data() to local:snippets (templates point their --cicustom vendor= at it);
    True if the file was (re)written, False if it already held exactly that. node: on another node
    (local: is each node's own disk; storage.cfg is the cluster's)."""
    cfg = open(STORAGE_CFG).read()
    m = re.search(r"^dir: local\n((?:[ \t]+.*\n?)*)", cfg, re.M)
    content = re.search(r"^\s+content\s+(\S+)", m.group(1), re.M) if m else None
    cur = content.group(1).split(",") if content else []
    if "snippets" not in cur:
        run("pvesm", "set", "local", "--content", ",".join(cur + ["snippets"]))
    return write_on(node, f"{SNIPPET_DIR}/{SNIPPET}", vendor_data())


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
        print(f"  {t.image}: the downloaded copy is current")
        return path
    ui.log(f"downloading {t.base}/{t.image}")
    h, part = hashlib.new(algo), path + ".part"
    with Spinner(f"{t.name}: downloading the image") as line:
        try:
            with http_open(f"{t.base}/{t.image}") as r, open(part, "wb") as f:
                total, done, shown = int(r.headers.get("Content-Length") or 0), 0, -1
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if total and done * 100 // total > shown:      # the spinner shows the log's last line
                        shown = done * 100 // total
                        ui.log(f"{shown}% of {total >> 20} MB")
        except OSError as e:
            die(f"download failed: {e}")
        line.ok(f"{t.name}: downloaded {t.image}" + (f" ({total >> 20} MB)" if total else ""))
    if h.hexdigest() != want:
        os.remove(part)
        die(f"{t.image}: checksum mismatch")
    os.replace(part, path)
    os.chmod(path, 0o644)
    return path


def image_tag(want):
    """Which image a template was built from, as stamp() gives it: image=<the checksum's first 16 hex>."""
    return f"image={want[:16]}"


def template_state(t, conf):
    """Catalogue template t at its VMID, given that VMID's config (or None): "ours" (pvelab's template
    on this node), "foreign" (some other VM), "elsewhere" (on another node) or "missing".
    A config without "_node" counts as this node's."""
    if conf is None:
        return "elsewhere" if vm_exists(t.vmid) else "missing"
    if not is_local(conf.get("_node")):
        return "elsewhere"
    return "ours" if conf.get("template") == "1" and conf.get("name") == t.name else "foreign"


def template_status(t, inv):
    """What `templates` and the menu say about t. inv: inventory()."""
    conf = inv.get(t.vmid)
    state = template_state(t, conf)
    if state == "ours":
        day = template_facts(conf).get("built")
        return "on this node" + (f", built {day}" if day else "")
    return {"foreign": "VMID in use by something else!", "elsewhere": "VMID in use on another node!",
            "missing": f"not built (pvelab pull {t.short})"}[state]


def linked_clones(*ids):
    """{template vmid: {vmid: name}} of the guests whose disks still need each template's disks (one scan
    for all of ids). On ZFS, Ceph and directory (qcow2) storage a linked clone's volume names its base
    (base-9003-disk-0/vm-201-...), and PVE won't delete that base. On LVM-thin clones are independent of
    it and don't name it."""
    base = re.compile(r"base-(\d+)-disk-\d+[^,\s]*/")
    out = {i: {} for i in ids}
    # not inventory(): clones on any node count, and disks only an old snapshot still uses
    for f in glob.glob(f"{NODES_DIR}/*/qemu-server/*.conf"):
        text = open(f).read()
        bases = {int(b) for b in base.findall(text)} & out.keys()
        if bases:
            m = re.search(r"^name: (\S+)", text, re.M)
            for b in bases:
                out[b][int(os.path.basename(f)[:-5])] = m.group(1) if m else "?"
    return {i: dict(sorted(c.items())) for i, c in out.items()}


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
    if state == "ours":                      # made before its bridge was chosen per host: one its host has
        fit_bridge(t.vmid, conf.get("_node") or node_of(t.vmid), conf.get("net0"))
        rec = registry.lookup(t.vmid, conf, "templates")      # else (built before): its description
        if stamp(conf) == image_tag(want) if rec else image_tag(want) in conf.get("description", ""):
            print(f"  {t.name}: template {t.vmid} is up to date")
            return
        clones = linked_clones(t.vmid)[t.vmid]
        if clones:
            warn(f"{t.name}: a newer image is out, but {len(clones)} VM(s) are linked clones that still need "
                 f"this template's disks ({some(clones.values(), 5, ' ')}) - not updated; it can be once "
                 "they are destroyed")
            return
    img = fetch_image(t, want)
    if state == "ours":
        print(f">> {t.vmid} ({t.name}): a newer image is out - replacing the template "
              "(VMs made from it stay as they are)")
        run("qm", "destroy", t.vmid, "--purge", "1")
    kvm, cpu = ("1", "host") if os.path.exists("/dev/kvm") else ("0", "x86-64-v2-AES")
    if kvm == "0":
        warn("/dev/kvm missing: nested virtualization is off, template uses --kvm 0 (slow)")
    try:
        with Spinner(f"{t.name}: creating template {t.vmid}") as line:
            ui.log("importing the disk")
            run("qm", "create", t.vmid, "--name", t.name, "--ostype", "l26",
                "--memory", 2048, "--cores", 2, "--cpu", cpu, "--kvm", kvm,
                "--net0", f"virtio,bridge={template_bridge()}",   # one this host has: a clone made by hand
                                                                 # starts (create sets each clone's own)
                "--scsihw", "virtio-scsi-single",
                "--scsi0", f"{STORAGE}:0,import-from={img},discard=on,ssd=1",
                "--scsi1", f"{STORAGE}:cloudinit",
                "--boot", "order=scsi0", "--serial0", "socket", "--vga", "serial0",
                "--agent", "enabled=1", "--ipconfig0", "ip=dhcp",
                "--cicustom", f"vendor=local:snippets/{SNIPPET}", capture=False)    # progress: the line's tail
            run("qm", "disk", "resize", t.vmid, "scsi0", TPL_DISK)
            run("qm", "template", t.vmid)
            record_template(t.vmid, {"kind": "built", "image": want[:16], "built": time.strftime("%Y-%m-%d")})
            line.ok(f"{t.name}: template {t.vmid} ready")
    except LabError:
        c = read_conf(t.vmid)             # our half-built VM: remove it so a retry starts clean
        if c is not None and c.get("template") != "1" and c.get("name") == t.name:
            run("qm", "destroy", t.vmid, "--purge", "1", check=False)
        raise


def check_template(vmid):
    c = read_conf(vmid)
    if c is None:
        die(f"template {vmid} does not exist")
    if c.get("template") != "1":
        die(f"VM {vmid} is not a template")


def resolve_template(arg):
    """VMID, template name or alias -> VMID of the template itself (a copy's VMID gives its original);
    builds a catalogue template if missing."""
    t = catalogue_find(arg)
    if t:
        if template_state(t, read_conf(t.vmid)) == "missing":
            print(f">> template {t.name} ({t.vmid}) is not on this node yet - building it")
            build_template(t)
        vmid = t.vmid
    elif (vmid := number(arg, 1, MAX_VMID)) is not None:
        vmid = source_of(read_conf(vmid)) or vmid     # a copy on another node: the template it copies
    else:
        a = str(arg).lower()
        vmid = next((i for i, n in node_templates().items() if n.lower() == a), None)
        if vmid is None:
            die(f"unknown template '{arg}' (see: pvelab templates)")
    check_template(vmid)
    return vmid


def template_label(vmid, on_node):
    """Name of template vmid; on_node: node_templates() (the templates of every node)."""
    t = catalogue_find(vmid)
    return t.name if t else on_node.get(vmid, "?")


def stamp(conf):
    """Which build of a template conf is: image=<16 hex>, else built=YYYY-MM-DD. A copy's record carries its
    original's, so a copy is current when the stamps match."""
    f = template_facts(conf)
    if f.get("kind") == "copy":
        return f.get("stamp") or ""
    return f"image={f['image']}" if f.get("image") else f"built={f['built']}" if f.get("built") else ""


def free_template_id(inv=None):
    """The first free VMID from SAVED_ID_START on (saved templates and template copies). Not one a kept copy
    still names as its source (delete-template can delete an original but keep a copy that clones use):
    a new template there would pass for that copy's original. inv: an inventory() read already."""
    taken = used_vmids() | {source_of(c) for c in (inv or inventory()).values()}
    return next(i for i in range(SAVED_ID_START, SAVED_ID_START + 900) if i not in taken)


def copy_list(orig, inv=None):
    """[(vmid, conf)] of every copy of template orig, on any node."""
    return [(i, c) for i, c in (inv or inventory()).items() if c.get("template") == "1" and source_of(c) == orig]


def copies(orig, inv=None):
    """{node: (vmid, conf)} of template orig's copies: per node the current one if there is one (an old
    one linked clones still use can sit beside it), else the newest."""
    inv = inv or inventory()
    want = stamp(inv.get(orig) or read_conf(orig))
    out = {}
    for i, c in copy_list(orig, inv):
        node = c["_node"]
        if node not in out or stamp(c) == want or stamp(out[node][1]) != want:
            out[node] = (i, c)
    return out


def templates_by_node(inv=None):
    """{template name: {node: "current" | "old"}}: where each template (original or copy) is."""
    inv = inv or inventory()
    out = {}
    for i, name in node_templates(inv).items():
        hosts = out.setdefault(name, {inv[i]["_node"]: "current"})
        for node, (_, c) in copies(i, inv).items():
            hosts.setdefault(node, "current" if stamp(c) == stamp(inv[i]) else "old")
    return out


def with_old(label, status):
    """label, plus ' (old)' when status (a value of templates_by_node) is "old"."""
    return label + (" (old)" if status == "old" else "")


def copy_state(orig, node, inv=None):
    """(template orig's conf, what node has of it as (vmid, conf) or None, whether that is current):
    orig itself when it is on node, else its copy there. inv: an inventory() the caller already read."""
    inv = inv or inventory()
    conf = inv.get(orig) or read_conf(orig)
    if conf and conf["_node"] == node:       # the template itself is there
        return conf, (orig, conf), True
    have = copies(orig, inv).get(node)
    return conf, have, bool(have) and stamp(have[1]) == stamp(conf)


def copy_needed(orig, node, inv=None, gb=None):
    """None if node has template orig (or a current copy of it), else the GB a copy writes there (gb: known
    already, from another host's check). Changes nothing and asks nothing: create puts the copy (and its
    space check) in its plan, the wizard notes it."""
    conf, _, current = copy_state(orig, node, inv)
    if current:
        return None
    return data_gb(orig, conf) if gb is None else gb


def template_on(orig, node, asked=False, inv=None, gb=None):
    """The VMID of template orig on node: orig itself if it is there, else a current copy there - made
    now (after asking, unless asked: the caller's plan did) if there is none. An old copy is replaced,
    unless linked clones still use it. inv: an inventory() read already; gb: copy_needed()'s answer."""
    conf, have, current = copy_state(orig, node, inv)
    if current:
        return have[0]
    if have:
        vmid = have[0]
        clones = linked_clones(vmid)[vmid]
        if clones:
            warn(f"{conf.get('name')}: the copy {vmid} on {node} is out of date, but {len(clones)} VM(s) are "
                 f"linked clones that still need its disks ({some(clones.values(), 5, ' ')}) - left in place; "
                 "making a new copy")
        else:
            print(f">> {conf.get('name')}: the copy {vmid} on {node} is out of date - replacing it")
            run(*on_node(node, "qm", "destroy", vmid, "--purge", "1"))
    return copy_template(orig, node, asked, gb=gb, inv=inv)


def host_bridges(node=None):
    """The bridges (vmbrN, labN, ...) node has now (here by default), in name order."""
    if node is None or is_local(node):
        found = glob.glob("/sys/class/net/*/bridge")
    else:
        found = run(*on_node(node, "sh", "-c", "ls -d /sys/class/net/*/bridge 2>/dev/null"),
                    check=False, quiet=True).split()
    return sorted(p.split("/")[-2] for p in found)


def template_bridge(node=None, have=None):
    """The bridge for a template's net0 on node: one it has, so a VM cloned from it by hand (in the PVE GUI)
    starts - pvelab's DHCP bridge, else the uplink, else its first bridge; vmbr1 when it has none
    (pvelab create sets, and makes, each clone's own). have: host_bridges(node), if read already."""
    have = host_bridges(node) if have is None else have
    return next((b for b in (DHCP_BRIDGE, UPLINK) if b in have), next(iter(have), DHCP_BRIDGE))


def fit_bridge(vmid, node, net0):
    """A template copied to node: its net0 on a bridge node has (the MAC and other settings kept)."""
    m = re.search(r"bridge=([^,]+)", net0 or "")
    if m and m.group(1) not in (have := host_bridges(node)):
        run(*on_node(node, "qm", "set", vmid, "--net0",
                     net0.replace(m.group(0), f"bridge={template_bridge(node, have)}")))


def copy_template(orig, node, asked=False, gb=None, inv=None):
    """Copy template orig to node (a new VMID, the same name): vzdump on its node piped into qmrestore on
    node, so nothing is written but the copy. Asks first; on failure or Ctrl-C the half copy is removed.
    gb: the size, if the caller has it (copy_needed()); inv: an inventory() read already."""
    conf = read_conf(orig)
    name, src = conf.get("name", ""), conf["_node"]
    gb = data_gb(orig, conf) if gb is None else gb          # lvs on the template's node
    if not asked:
        confirm(check_copy_space(gb, node))
        confirm_yes(f"template {name} is not on {node} yet: copy it there (~{gb:.1f} GB)?")
    new = free_template_id(inv)
    try:
        with Spinner(f"copying template {name} to {node} (~{gb:.1f} GB)") as line:
            pipe(on_node(src, "vzdump", orig, "--stdout", "--mode", "stop", "--compress", "0"),
                 on_node(node, "qmrestore", "-", new, "--storage", STORAGE))
            if (read_conf(new) or {}).get("template") != "1":    # qmrestore makes a template's backup one again
                run(*on_node(node, "qm", "template", new))
            gone = ["--delete", "description"] if (read_conf(new) or {}).get("description") else []
            run(*on_node(node, "qm", "set", new, "--name", name, *gone))    # not the original's description
            record_template(new, {"kind": "copy", "orig": orig, "stamp": stamp(conf)})
            fit_bridge(new, node, conf.get("net0"))     # node may lack the source host's bridge
            line.ok(f"template {name} is on {node} now ({new})")
    except BaseException:                 # failed, or Ctrl-C: remove our half copy (a retry starts clean)
        c = read_conf(new)
        if c is not None and node_of(new) == node and (c.get("name") == name or c.get("lock") == "create"):
            discard_copy(node, new)
        raise
    return new


def discard_copy(node, vmid, stop=False, label=None):
    """Remove a half-made template copy (a retry starts clean); errors are ignored. stop: stop it first.
    label: how the message names it (default: "VMID on NODE")."""
    print(f">> removing the unfinished copy {label or f'{vmid} on {node}'}")
    run(*on_node(node, "qm", "unlock", vmid), check=False, quiet=True)
    if stop:
        run(*on_node(node, "qm", "stop", vmid), check=False, quiet=True)
    run(*on_node(node, "qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1"),
        check=False, quiet=True)


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
    return bool(conf) and conf.get("template") == "1" and template_facts(conf).get("kind") == "saved"


def saved_from(conf):
    """What a saved template was made from: 'NAME (VMID n) built=YYYY-MM-DD'."""
    f = template_facts(conf)
    return " ".join(x for x in (f.get("from"), f"built={f['built']}" if f.get("built") else "") if x)


def check_template_name(name, inv):
    if not HOST_LABEL.fullmatch(name):
        die(f"template name '{name}': letters, digits and - only (like docker-lab)")
    if catalogue_find(name):
        die(f"'{name}' is a catalogue template's name - pick another")
    if any(c.get("name", "").lower() == name.lower() for c in inv.values()):
        die(f"a VM or template named '{name}' exists already")


def lv_data(text):
    """{vmid: GB of data} from `lvs` output (LV name, size, data %): the sum of each VM's disks
    (vm|base)-<vmid>-disk-N; a template's disks are base-<vmid>-disk-N."""
    used = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and (m := re.fullmatch(r"(?:vm|base)-(\d+)-disk-\d+", f[0])):
            i = int(m[1])
            used[i] = used.get(i, 0.0) + float(f[1]) * float(f[2]) / 100
    return used


def data_gbs(confs):
    """{vmid: GB of data on its disks} for confs ({vmid: conf}) - what a full copy writes. On LVM-thin what
    the disks really hold, elsewhere (no LV) their full size. One lvs per host, all at the same time."""
    by_node = {}
    for i in confs:
        by_node.setdefault(confs[i]["_node"], []).append(i)
    lvs = ("lvs", "--noheadings", "--nosuffix", "--units", "g", "-o", "lv_name,lv_size,data_percent")
    outs = parallel(lambda n: run(*on_node(n, *lvs), check=False, quiet=True), by_node)
    gb = {}
    for n, ids in by_node.items():
        used = lv_data(outs[n])
        for i in ids:
            gb[i] = used.get(i) or sum(conf_disk_gb({"scsi0": v}) for k, v in confs[i].items()
                                       if re.fullmatch(r"(scsi|virtio|sata)\d+", k))
    return gb


def data_gb(vmid, conf):
    """GB of data on vmid's disks (data_gbs)."""
    return data_gbs({vmid: conf})[vmid]


def copied(line):
    """qm clone's 'mirror-scsi0: transferred 3.3 GiB of 10.0 GiB (33.31%) in 11s' -> '33%'; other lines as they are."""
    m = re.search(r"transferred .* \((\d+)(?:\.\d+)?%\)", line)
    return f"{m.group(1)}%" if m else line


def save_template(src, name):
    """A new template NAME with VM src's disks as they are now; src itself is not changed.
    It is made on src's node."""
    conf = read_conf(src)
    node = node_of(src)

    def qm(*args, **kw):                      # qm on src's node (and so the copy's); its messages to the log
        return run(*on_node(node, "qm", *args), **kw)

    m = re.search(r"bridge=([^,]+)", conf.get("net0", ""))
    if not m:
        die(f"{conf['name']} has no network card (net0) - pvelab VMs have one")
    bridge = m.group(1)
    gb = data_gb(src, conf)
    confirm(check_copy_space(gb, node))       # the copy is made on src's node
    vmid = free_template_id()
    try:
        with Spinner(f"{name}: copying {conf['name']} (~{gb:.1f} GB)", brief=copied) as line:
            qm("clone", src, vmid, "--name", name, "--full", "1", capture=False)    # progress: the line's tail
            gone = "tags,cipassword" + (",description" if (read_conf(vmid) or {}).get("description") else "")
            qm("set", vmid, "--delete", gone, "--onboot", "0", "--ipconfig0", "ip=dhcp",   # not src's notes
                "--net0", f"virtio,bridge={bridge},link_down=1")       # no network: src's IP is in use
            record_template(vmid, {"kind": "saved", "from": f"{conf['name']} (VMID {src})",
                                   "built": time.strftime("%Y-%m-%d")})
            line.ok(f"copied {conf['name']} to {vmid} '{name}'")
        with Spinner(f"{name}: booting the copy once (no network) to make it forget who it was") as line:
            qm("start", vmid)
            wait_for("its boot", lambda: not booting(first_boot_state(vmid)[0]), READY_TIMEOUT)
            r = guest_json("exec", vmid, "--timeout", 300, "--", "sh", "-c", FORGET, timeout=330)
            if not r or r.get("exitcode") != 0:
                die(f"{name}: clean-up in the copy failed: {(r or {}).get('err-data', 'no answer')}".strip())
            qm("shutdown", vmid, "--timeout", 120, check=False)
            if vm_status(vmid) != "stopped":
                qm("stop", vmid)
            qm("set", vmid, "--net0", f"virtio,bridge={template_bridge(node_of(vmid))}")   # one its host has
            qm("template", vmid)
            line.ok(f"{name}: cleaned up and made a template")
    except BaseException:                 # failed, or Ctrl-C: remove our half-made copy (a retry starts clean)
        c = read_conf(vmid) or {}
        mid_clone = c.get("lock") == "clone" and "qmclone temporary" in c.get("description", "")
        if mid_clone or c.get("template") != "1" and c.get("name") == name:
            discard_copy(node, vmid, stop=True, label=vmid)
        raise
    print(f">> template '{name}' ({vmid}) ready - use it: pvelab create 20 {name} --net N")


def delete_template(name):
    """Delete a saved template and its copies on other nodes - each one no linked clone still uses."""
    inv = inventory()
    vmid = next((i for i, c in inv.items() if is_saved(c) and c.get("name", "").lower() == name.lower()), None)
    if vmid is None:
        die(f"no template '{name}' made by save-template (see: pvelab templates)")
    todo = [(vmid, inv[vmid])] + copy_list(vmid, inv)
    kept, deleted, clones_of = [], [], linked_clones(*(i for i, _ in todo))
    for i, c in todo:
        node = c["_node"]
        clones = clones_of[i]
        if clones and len(todo) == 1:         # one node, no copies: as before
            die(f"{len(clones)} VM(s) are linked clones that still need its disks: {some(clones.values(), 5, ' ')}")
        if clones:
            kept.append(f"kept {'template' if i == vmid else 'copy'} {i} on {node}: "
                        f"linked clones still use it ({some(clones.values(), 5, ', ')})")
            continue
        run(*on_node(node, "qm", "destroy", i, "--purge", "1"))
        deleted.append(i)
        if i != vmid:
            print(f">> copy {i} on {node} deleted")
    if not deleted:
        die("nothing deleted:\n  " + "\n  ".join(kept))
    for line in kept:
        print(f">> {line}")
    if vmid in deleted:
        print(f">> template '{name}' ({vmid}) deleted - VMs made from it stay as they are")
