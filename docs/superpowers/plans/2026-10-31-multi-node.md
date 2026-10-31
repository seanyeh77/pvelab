# pvelab on several PVE hosts: implementation plan

Spec: `docs/superpowers/specs/2026-10-31-multi-node-design.md`, including "Decisions after review". If this plan and
the spec disagree, the spec wins. Four chunks, in order: A, B, C, D. After each chunk, all of these must pass:

```
cd /home/seanyeh77/pvelab-src
PYTHONPATH=src timeout 180 uvx -q --with pytest pytest -q tests/unit
timeout 60 uvx -q pyflakes src tests build.py
python3 build.py
bash -n tests/selftest.sh
```

Rules:
- Python 3.11, no new dependencies (no paramiko): the `ssh` binary only. Short functions, plain-English comments.
- No git commits. Never run pvelab, `qm`, `pvesh`, `pvecm`, `tests/selftest.sh`, or ssh against a host.
- **One host, or no cluster, behaves exactly as today.** Single-host calls keep their present shape, because the
  existing tests mock them with their old signatures:
  - `run("qm", ...)` through each module's own `run`;
  - `check_resources(n, mem)`, `check_copy_space(gb)`, `storage()`, `ensure_snippet()`, `resolve_template(t)`,
    `clone_vm(...)` with today's arguments.
  - A host argument is passed only when the target is another node.
- **The routing rule.** A command for another node is today's argv with an ssh prefix, built by `nodes.on_node()`.
  For the local node, `on_node()` returns the argv unchanged. So the existing mocks of `run` keep seeing
  `("qm", "clone", ...)`.

Decisions taken here (the spec leaves them to the builder):
- **`ssh root@<node> qm ...`, not `pvesh`.** It is the same CLI, so the output that pvelab already parses
  (`qm list`, `qm status`, `qm guest exec --pass-stdin`, which needs stdin) stays the same.
  - Options are PVE's own (`PVE::SSHInfo`): `BatchMode=yes`, `HostKeyAlias=<node>`,
    `UserKnownHostsFile=/etc/pve/nodes/<node>/ssh_known_hosts`, `GlobalKnownHostsFile=none`.
  - On top of those, a ControlMaster (`ControlPath=/root/lab/ssh-%C`, `ControlPersist=60`), so the many short `qm`
    calls share one connection.
- **Template copy:** `vzdump <id> --stdout --mode stop --compress 0` on the source node, piped into
  `qmrestore - <new> --storage STORAGE` on the target node.
  - There is no temporary full clone, so nothing is written to workshops' nearly full pool. Only the target is
    space-checked.
  - Both commands accept a pipe: see `qmrestore.pm` ("'-' to read from standard input") and `VZDump.pm` `stdout`.
- **Where the rules live:** only the bridge-family nft isolation table runs on every host. Everything else stays on
  workshops, as today: gateway .1, NAT, TCPMSS, mining drops, `block`, `expose`/DNAT, hairpin drop.
  - Reason: a VM on host B reaches the gateway, and so the internet, over VXLAN into workshops' `labN`.
  - So that traffic passes workshops' `FORWARD -i labN` / `-o labN` rules. The frames keep the VM's own MAC, so
    `--mac-source` still matches.
  - This must be shown on two real hosts: see the selftest in chunk D.

---

## Chunk A: node routing

### Files and functions
- **`config.py`**
  - `NODES_DIR = "/etc/pve/nodes"`, `MEMBERS = "/etc/pve/.members"`, `SSH_CTL = f"{LAB_DIR}/ssh-%C"`.
  - Tests point these at `tmp_path`.
- **`util.py`**
  - `run(*cmd, check, quiet, capture, data=None)`: the new `data` is passed as stdin (`input=`). Callers that do not
    pass `data` see no change.
- **`nodes.py`** (new)
  - `this_node()`: `os.uname().nodename`.
  - `members()` → `{name: {"ip": str, "online": bool}}`, read from the `.members` JSON (`nodelist`).
    - With no cluster, or when the file can't be read, it returns `{this_node(): {"ip": uplink_ip(), "online": True}}`.
  - `node_names()` → the sorted member names. `multi()` → `len(members()) > 1`.
  - `node_of(vmid)` → the `<node>` from `glob(NODES_DIR/*/qemu-server/<vmid>.conf)`, or `None` if there is no such file.
  - `is_local(node)` → True for `None` or `this_node()`.
  - `ssh_argv(node)` → `["ssh", <PVE options>, "-o", f"ControlPath={SSH_CTL}", "-o", "ControlMaster=auto",
    "-o", "ControlPersist=60", "-T", f"root@{members()[node]['ip']}"]`.
    - It dies with a clear message if the node is offline: "host X is offline; its VMs can't be reached".
  - `on_node(node, *cmd)` → `cmd` unchanged when local. Otherwise `[*ssh_argv(node), "--", shlex.join(map(str, cmd))]`:
    ssh joins its arguments into one shell line, so `sh -c SCRIPT` must be quoted.
  - `run_on(node, *cmd, **kw)` = `run(*on_node(node, *cmd), **kw)`: runs any command on a given node (lvs, nft,
    ip link, cat > file).
  - `qm_argv(vmid, *args)` = `on_node(node_of(vmid), "qm", *args)`.
    - This is the one wrapper every VM command uses: `run(*qm_argv(vmid, "start", vmid))`.
- **`inventory.py`**
  - `conf_path(vmid)`: the local `CONF_DIR` file if it exists, else the `NODES_DIR` glob.
  - `read_conf()` and `snapshot_names()` use `conf_path()`.
  - `inventory()` globs `NODES_DIR/*/qemu-server/*.conf`, which covers every node. Each conf gets `"_node": <name>`.
    No PVE key starts with `_`.
  - `vm_status(vmid)` → `run(*qm_argv(vmid, "status", vmid))`.
  - `vm_statuses(ids)`:
    - Groups the ids by `node_of()`.
    - Runs one `qm list` per node, at the same time (`concurrently`), and merges the results.
    - With one node it makes exactly today's single call.
- **`guest.py`**
  - `guest_json(*cmd, data, timeout)` runs `subprocess.run(on_node(node_of(cmd[1]), "qm", "guest", *cmd), ...)`.
    `cmd[1]` is always the VMID.
  - stdin (`--pass-stdin`) flows through ssh.
- **`lifecycle.py`**
  - In `clone_vm`, `qm clone` runs on `node_of(tpl)`. The template (or its copy, from chunk B) already sits on the
    target host. Every later `qm set` / `disk resize` / `snapshot` / `start` call uses `qm_argv(vmid, ...)`.
  - `take_snapshot` and `remove_vm` use `qm_argv`.
- **`commands/power.py`, `commands/snapshots.py`**: every `run("qm", ...)` becomes `run(*qm_argv(vmid, ...))`.
- **`commands/resize.py`**
  - `changes()` still returns `("qm", ...)` tuples, so the tests are unchanged. The caller runs
    `run(*on_node(node_of(i), *cmd))`.
  - The reboot step goes through `qm_argv`.
  - The memory check groups `more` by node:
    - the local node uses today's `mem_shortfall()`;
    - another node uses its free memory (`node_room()`, chunk B; until then, skip it with a warning).
- **`templates.py`**
  - The `qm` calls in `save_template` and `delete_template` use `on_node(node_of(vmid), ...)` through the module's
    `run`.
  - `build_template` stays local (`pull` acts on workshops).
  - `template_state(t, conf)`: a conf whose `_node` is another node counts as `"elsewhere"`. A conf without `_node`
    counts as local, which keeps the test mocks working.
- **`resources.py`**
  - `vm_stats()` drops the `node == this` filter. VMIDs are cluster-wide, and every row keeps `r["node"]`.
- **`commands/top.py`**
  - `hour_cpu()` uses `/nodes/{node_of(vmid) or this_node()}/qemu/...`.
  - The header shows one memory line per host when `multi()`.
- **`commands/listing.py`**
  - A `HOST` column (width 10, after `STATUS`) only when `multi()`. It takes `inv[vmid]["_node"]`.
  - The summary's memory line becomes one line per host when `multi()`.
- **`commands/ssh.py`, `handout.py`, `access.py` (block)**: no change.
  - `pvelab ssh` reaches a VM on host B through workshops' `labN` and VXLAN.
  - `block` uses `vm_mac()`, which now reads the conf wherever it is.
- **`cli.py`**: no change in chunk A. `elevate()` already runs everything as root, and root has the cluster ssh keys.

### Tests: `tests/unit/test_nodes.py`
- `members()`: parses a two-node `.members` file in `tmp_path`. With a missing or unreadable file it falls back to
  one node.
- `node_of()` finds `nodes/b/qemu-server/301.conf`, and returns `None` for an unknown VMID.
- `on_node()`:
  - local → argv unchanged;
  - remote → ssh prefix with `HostKeyAlias=b`, `BatchMode=yes` and the ControlPath;
  - `shlex.split(remote string) == cmd` for an argument holding spaces and quotes;
  - an offline node → `LabError`.
- `qm_argv`: power `start` on a VM of node b builds `ssh ... qm start 301`, with `run` mocked.
- `guest_json` routes by `cmd[1]` and passes `data` as stdin (mock `subprocess.run`).
- `vm_statuses`: two nodes give two `qm list` calls, merged. One node gives exactly one call, `("qm", "list")`.
- `inventory()` sets `_node`.
- `list`: the header has no `HOST` with one node, and has it with two (`capsys`, everything mocked).
- `resize.changes` output is unchanged (an existing test).

---

## Chunk B: placement and templates

### Files and functions
- **`placement.py`** (new, pure where possible)
  - `node_room()` → `{node: {"mem": MB free, "disk": GB free}}` for online nodes. It comes from one call to
    `pvesh_json("/cluster/resources")`:
    - `type == "node"` rows: `maxmem - mem`, minus `HOST_RESERVE_MB`;
    - `type == "storage"` rows with `storage == STORAGE`: `maxdisk - disk`.
  - `fits(room, mem, gb)` → how many students of this size fit: `min(mem_free // mem, disk_free // gb)`, never below 0.
    The wizard uses it too.
  - `place(students, mem, gb, room, node=None, allowed=None, fixed={})` → `{student: node}`.
    - `fixed`: students that already have a VM keep its node.
    - `node`: every new student goes there.
    - Otherwise each student goes, greedily, to the allowed node with the most room after the students placed so far:
      the highest `min(mem_free / mem, disk_free / gb)`, ties broken by name.
    - Room is subtracted after each pick, so the class spreads.
  - `bridge_host(net)` → `this_node()` for `"dhcp"` and for vmbrN nets (`not is_sdn(net)`), else `None` (any host).
- **`resources.py`**
  - `storage(node=None)`: the local node keeps today's `pvesm status`. Another node uses
    `pvesh_json(f"/nodes/{node}/storage/{STORAGE}/status")`.
  - `host_mem_mb(node=None)`: the local node keeps `/proc/meminfo`. Another node uses `/nodes/{node}/status`
    `memory.total` / `memory.available`.
  - `check_resources(n, mem, node=None)` and `check_copy_space(gb, node=None)` pass `node` on only when it is not
    local. The messages name the host when `multi()`.
- **`commands/create.py`**
  - Parse `--node auto|NAME`. The default is `auto`.
    - NAME must be in `members()` and online. Otherwise die with `did_you_mean`.
  - With one node, skip all of the following. The code runs exactly as today.
  - Pinned nets: `pin = bridge_host(net)`.
    - `--node OTHER` with a pinned net dies: "net30 is a bridge vmbr30 on workshops only (not an SDN vnet): its VMs
      stay on workshops".
    - `auto` with a pinned net uses only that host.
  - `fixed` = for each student of `todo` who already has a VM on the net (`student_of`), that VM's `node_of()`.
  - `where = place(...)`; then `check_resources(len, mem, node)` once per node.
  - Per node, `tpl_on[node][orig] = template_on(orig, node)`. Then `clone_vm(..., tpl_on[where[student]][tpl[k]], ...)`.
    - The clone's description keeps `template=<original VMID>`, so `list` labels stay stable.
  - `ensure_snippet(node)` for each remote node used.
  - Print one line per host: `>> placement: workshops 6 students, pve2 4 students`.
- **`templates.py`**
  - `COPY = "copy of"`.
  - `stamp(conf)`: the `image=<16hex>` tag if there is one, else `built=YYYY-MM-DD`.
  - `free_template_id()`: the next free VMID at or above `SAVED_ID_START`. `save_template` uses it too.
  - `source_of(conf)` → the original's VMID from `copy of N`, else `None`.
  - Template identity is the **name**. `resolve_template(arg)` takes a catalogue name, alias or VMID, a saved
    template's name or VMID, or a copy's VMID (mapped through `source_of`). It returns the **original's** VMID.
    The building logic is unchanged.
  - `copies(orig)` → `{node: (vmid, conf)}` of the templates whose description says `copy of <orig>`.
  - `template_on(orig, node)`:
    - The original is on `node` → return it.
    - A copy with the same `stamp` is there → return it.
    - A stale copy is there:
      - no `linked_clones` → destroy it on its node, then copy again;
      - linked clones still use it → leave it, and say so (as `pull` does).
    - Otherwise call `copy_template(orig, node)`.
  - `copy_template(orig, node)`:
    1. `gb = data_gb(orig, conf, node)`. `lvs` runs through `run_on(source node)`.
    2. `check_copy_space(gb, node)`.
    3. `confirm_yes(f"template {name} is not on {node} yet: copy it there (~{gb:.1f} GB)?")`.
    4. `new = free_template_id()`.
    5. A two-process pipe (`subprocess.Popen`): `on_node(src, "vzdump", orig, "--stdout", "--mode", "stop",
       "--compress", "0")` piped into `on_node(node, "qmrestore", "-", new, "--storage", STORAGE)`.
    6. `qm template new`, unless the restored conf already has `template: 1`.
    7. `qm set new --name <name> --description "copy of <orig> <name> <stamp>"`.
    - On failure or Ctrl-C, remove the half copy on `node`, the same way `save_template` cleans up.
  - `data_gb(vmid, conf, node=None)`: the existing test mocks `run` with an `lvs` reply. That still works for the local
    node.
  - `save_template(src, name)`:
    - `node = node_of(src)`. Every `qm` call goes through `on_node(node, ...)`.
    - `check_copy_space(gb)` when local, `check_copy_space(gb, node)` otherwise.
  - `delete_template(name)`: the original plus every copy (any node).
    - Delete each one that has no `linked_clones`, on its own node.
    - Print the ones it kept: "kept copy 9107 on pve2: linked clones still use it (a, b)".
    - Die only if nothing could be deleted.
  - `node_templates(inv, node=None)`: originals only. With `node`, it lists what that host has.
  - `templates_by_node(inv)` → `{name: {node: "current" | "old"}}`, for `templates` and `node list`.
  - `template_label()` looks names up across all nodes.
- **`commands/templates.py`**: `cmd_templates`
  - With `multi()`, adds a `HOSTS` column, e.g. `workshops, pve2 (old)`.
  - With one node, prints exactly what it prints today.
  - `check_template_name()` checks names across the whole cluster. It already uses `inventory()`, which is now
    cluster-wide.

### Tests: `tests/unit/test_placement.py`, additions to `test_ux.py`
- `place`:
  - a student's VMs share one node;
  - a class spreads to the roomier host and then alternates as room shrinks;
  - `node=` puts everyone on that node;
  - `fixed` students keep their node;
  - when nothing fits, it still picks the best host (the warning asks later).
- `bridge_host` / create:
  - a vmbrN net with `--node pve2` dies with the reason;
  - `auto` + vmbrN → workshops only;
  - `dhcp` → workshops.
- `fits()`: arithmetic and the 0 floor.
- `node_room()` parses a `/cluster/resources` JSON sample (node and storage rows, one node offline).
- `template_on`:
  - original present → no copy;
  - current copy → reused;
  - stale copy without clones → destroyed and re-copied;
  - stale copy with clones → kept and a new copy made;
  - the new VMID is ≥ 9100 and skips used ones;
  - the description has `copy of 9002 debian-13-cloud image=...`.
- `copy_template`:
  - the pipe argvs are `vzdump ... --stdout` → `ssh ... qmrestore - NEW --storage local-lvm`;
  - the space check is made with the target node;
  - a failure removes the half copy;
  - with no yes, nothing is copied.
- `resolve_template`: a copy's VMID resolves to its original.
- `delete_template`: deletes the unused copies and lists the kept one.
- `fake_create` and the save-template tests pass unchanged.

---

## Chunk C: networking and nodes

### Isolation on every host (`isolation.py`, `networks.py`)
- `gw_mac(n)` = `02:00:0a:0a:{n:02x}:01`: a fixed MAC for workshops' gateway on `labN`.
  - New SDN gateway stanzas (`bridge_stanza(..., sdn_made=True)`) get the line `hwaddress {gw_mac(n)}`.
  - `ensure_gw_mac(n)`: an older SDN net that lacks the line gets it inserted into its block, and
    `ip link set labN address ...` now.
  - It runs only when a net first gets VMs on another host. net30 and net40 are vmbrN, so they are never touched.
- `nft_table(n, vms, gateway=True)`: unchanged for the gateway host.
  - Its existing `@same_student`-or-drop rule already drops `vxlan_labN` ↔ tap traffic, because the VXLAN port is in no
    pair.
  - For the other hosts (`gateway=False`), it adds these rules before `drop`:
    - `oifname "vxlan_lab{n}" ether daddr {gw} accept`
    - `oifname "vxlan_lab{n}" arp operation request arp daddr ip {subnet}.1 accept`
    - `iifname "vxlan_lab{n}" ether saddr {gw} accept`
  - So on host B, a VM reaches only the gateway over VXLAN, and only the gateway reaches it. Student A on host 1 → B
    on host 2 is dropped twice: on the sender's host and on the receiver's host.
  - The VXLAN port name `vxlan_<vnet>` comes from `VxlanPlugin.pm`.
- `set_isolation(n, on)`: the local part is as today.
  - When `multi()` and `is_sdn(n)`, also `push_nft(node, n, text)` / `drop_nft(node, n)` for every other online node:
    - `run_on(node, "sh", "-c", f"mkdir -p {NODE_NFT_DIR} && cat > {path} && nft -f {path}", data=text)`;
    - off: `rm -f` the file, and `nft delete table bridge pvelab_net{n}`, allowed to fail.
  - `NODE_NFT_DIR = "/etc/pvelab/nft"` on the other hosts. That host-side state is needed there, so it is allowed
    (spec: only rules that must be enforced elsewhere).
  - `refresh_net` and `remove_bridge` already call `set_isolation`, so create and destroy keep every host current.
- `ensure_nft_unit(node)`: writes and enables `pvelab-nft.service`, which loads `/etc/pvelab/nft/*.nft` at boot.
  - It is a oneshot, `After=network.target`, and the tables match bridge names, so the bridge may come up later.
  - Called by node prep.
- No other rule moves (see the decisions at the top).
- `sdn.node_ips()` prefers `.members` IPs, and falls back to corosync / `uplink_ip` as today.
  - `ensure_zone()` already rewrites the peers when the list changed.

### `pvelab node list | add ADDR` (`commands/node.py`, `joining.py`)
- **`cli.py`**
  - `COMMANDS["node"] = COMMANDS["nodes"] = (cmd_node, "write")`.
  - In `main()`, `node`/`nodes` without `add` counts as `"read"` (no lock).
- **`cmd_node(args)`**: `list` is the default; `add ADDR`; anything else is `bad_usage`.
- **`node_list()`**
  - A table with the columns `HOST`, `STATUS` (online/offline), `MEM FREE`, `DISK FREE ({STORAGE})`, `VMS` (pvelab
    VMs, from `inventory()` `_node`) and `TEMPLATES` (`templates_by_node`).
  - It works with no cluster: one row.
- **`joining.py`**
  - `class Remote(addr, user)`:
    - `open()`: `ssh -o ControlMaster=yes -o ControlPersist=600 -o ControlPath=/root/lab/ssh-join-%C -fN user@addr`
      in the foreground. ssh's own tty prompts ask for the host key and the password.
    - `run(cmd)` → `(rc, out)` over the master, with `BatchMode=yes`.
    - `tty(cmd)`: `ssh -t` over the master, for prompts.
    - `close()`: `ssh -O exit`, always run in `finally`.
    - No password is ever read, stored, or put on a command line.
  - `CHECKS`: each check is `(label, function(remote, local) -> problem text | None, fix text)`. All of them are
    read-only, run as the plain SSH user (no sudo needed), and print ✔/✘:
    - **hostname**: `hostname` is not a member's name, and not workshops' name.
      Fix: `hostnamectl set-hostname NEW` + `/etc/hosts`, before joining.
    - **resolves**: `getent hosts $(hostname)` gives ADDR, not 127.0.1.1.
      Fix: the `/etc/hosts` line `ADDR name.domain name`.
    - **version**: `pveversion` has the same major.minor as the local one.
      Fix: `apt update && apt full-upgrade` on the older host.
    - **no guests**: `ls /etc/pve/nodes/$(hostname)/qemu-server /etc/pve/nodes/$(hostname)/lxc` is empty. Non-root
      can read these.
      Fix: back up with `vzdump`, then remove the guests.
    - **storage**: `test -e /dev/mapper/pve-data-tpool` (LVM-thin `pve/data`, which `local-lvm` needs on every host).
      Fix: the host must use the default LVM-thin layout; pvelab uses one storage name for the whole cluster.
    - **/dev/kvm** exists.
      Fix: enable nested virtualization / CPU passthrough on the hypervisor.
    - **TCP 22, 8006 both ways**:
      - local: a Python socket connect to ADDR;
      - remote: `perl -MIO::Socket::INET` connects to workshops' IP. perl is always on PVE.
    - **UDP 4789 and 5405-5412 both ways**:
      - The receiver opens a short-lived listener: perl on the remote side, Python on the local side. The other side
        sends a random token, and the check passes when the token arrives.
      - A local port that is already bound (corosync, VXLAN) can't be tested inbound. It is listed as "make sure UDP
        P from ADDR is allowed", as a note, not a failure.
      - Fix: open these ports between the two IPs in the OpenStack security group.
    - **time**:
      - `|remote date +%s.%N − local midpoint| < 2 s`;
      - `timedatectl show -p NTPSynchronized --value` is `yes` on both hosts.
      - Fix: `systemctl enable --now chrony`.
    - **local**: if workshops is in a cluster, it is quorate.
  - `parse_*` helpers (version, getent, pvecm status, members) are pure, for the tests.
- **`node_add(addr)`**, step by step. Each step is wrapped so that a failure prints the step, each host's state, and
  the recovery (`RECOVERY[step]`).
  0. **Already a member?** If ADDR is in `members()` (by IP or name): print "already in the cluster" and go to step 4.
     This makes the command rerunnable.
  1. **Checks.** Ask for the SSH user (empty means root). `Remote.open()`, then run all checks. Print every problem with
     its fix; if there are any, stop: "nothing was changed; fix these and run `pvelab node add ADDR` again".
  2. **Cluster.** If there is no cluster:
     - Print what it does: "creates cluster 'pvelab' on workshops; pve-cluster restarts for a few seconds; running VMs
       are not touched".
     - Require a typed `yes` on a real tty, **even with LAB_YES=1**. With no tty, die.
     - Run `pvecm create pvelab --link0 <workshops IP>`, then wait until it is quorate.
  3. **Join.**
     - `confirm_yes("join ADDR (hostname) to the cluster; the network is applied on all hosts afterwards")`.
     - Then one `Remote.tty()` call: `[sudo -p 'sudo password for %u on ADDR: '] sh -c 'cd / && pvecm add <ws IP>
       --link0 ADDR --fingerprint <FP>'`. `sudo` is dropped when the user is root.
     - FP comes from workshops' API certificate: `pveproxy-ssl.pem` if it exists, else `pve-ssl.pem`, via
       `openssl x509 -noout -fingerprint -sha256`.
     - pvecm asks for workshops' root password on that tty.
  4. **Verify.** For up to 120 s, poll `members()` (the new node is online) and `pvecm status` (`Quorate: Yes`).
  5. **Prepare**, through the new root ssh with the cluster keys (`run_on`):
     - `ensure_snippet(node)`;
     - `ensure_nft_unit(node)`;
     - push the nft tables of the isolated SDN nets;
     - `sdn.ensure_zone()` (peers) + `sdn.apply()`, with a note that it applies on all hosts;
     - `ip link show labN` on the node for each SDN net of pvelab: report each one ✔/✘.
  6. **Note:**
     - With two hosts, if one is down the other loses quorum: `/etc/pve` is read-only, and VMs can't be started or
       changed.
     - The way out, on the surviving host: `pvecm expected 1`.
     - Later, a QDevice: `pvecm qdevice setup <IP>`.
  - **`RECOVERY`**:
    - checks: nothing changed.
    - create: `pvecm status` on workshops; if it shows no cluster, rerun.
    - join: ADDR may be half-joined.
      - If `pvecm status` on ADDR shows the cluster: rerun `node add`.
      - Otherwise, on ADDR as root: `systemctl stop pve-cluster corosync; pmxcfs -l; rm -f /etc/corosync/*
        /etc/pve/corosync.conf; killall pmxcfs; systemctl start pve-cluster`.
      - Then on workshops: `pvecm delnode NAME`, if it is listed.
    - verify: check `pvecm status` on both hosts, then rerun.
    - prepare: the join is done; rerunning `node add` skips to step 4.

### Tests: `tests/unit/test_joining.py`, `test_isolation_hosts.py`
- **Each check**, with a fake `Remote` (a dict from command → `(rc, out)`):
  - passes on a good host;
  - on a bad host, fails with its own fix: 127.0.1.1 resolution, 9.1 vs 9.2, a guest present, no pve-data-tpool,
    no /dev/kvm, a UDP token that never arrives, 5 s clock skew;
  - a bound local port → a note, not a failure.
- **Parsers:** `pvecm status` quorate yes/no, `pveversion` major.minor, `.members`.
- **`node_add` with every step mocked:**
  - already a member → only steps 4 and 5 run;
  - failed checks → no `pvecm` call at all;
  - no cluster + LAB_YES + no tty → dies before `pvecm create`;
  - a join failure → the message names the step and contains `pvecm delnode`;
  - the `Remote` argvs contain no password, and `close()` runs on failure.
- **nft:**
  - `nft_table(..., gateway=False)` has the three gateway rules, and the gateway variant does not;
  - `set_isolation` pushes to the other online nodes only when `multi()` and SDN (mock `run_on`, `members`);
  - `ensure_gw_mac` inserts `hwaddress` once.
- **`node list`** with no cluster prints one row.

---

## Chunk D: UI and docs

- **`wizard.py`**
  - The MENU gets `("nodes", "Nodes: add a PVE host, or see each host's free space")`.
    - `Wizard.nodes()` prints `node_list()`, then `choose` between "add a host" and "back".
    - For "add", it asks for ADDR (an IPv4 check) and returns `["node", "add", ADDR]`. `node add` itself shows ✔/✘ and
      asks its own questions on the tty.
  - In `create`, after the network question, only when `multi()` and the net is not pinned (`bridge_host` is None):
    - Print the fit table: host, free memory, free disk, students of this size that fit (`fits`).
    - `choose("Where", [("auto", "spread by free space (recommended)"), *((n, f"all on {n}") ...)])`.
    - Warn in yellow for each host that lacks the template: "debian-13-cloud is copied to pve2 first (~2.1 GB)".
    - `argv += ["--node", choice]`.
    - A pinned net prints a dim note: "net30 is on workshops only".
  - With one host, nothing is asked. The existing wizard tests are unchanged.
- **`helptext.py`**
  - `USAGE`:
    - `create ... [--node auto|NAME]`;
    - a "Hosts" section with `node list` and `node add ADDR`.
  - `command_help("create")`: a `--node` paragraph covering spread by room, one student = one host, and vmbrN/dhcp
    staying on workshops.
  - `command_help("node")` (also used for `nodes`): checks, cluster create, join, rerun, quorum note.
- **`commands/complete.py`**
  - Add `"node"` and `"nodes"` to `COMMANDS`, and `"--node"` to `OPTIONS["create"]` and `VALUE_OPTIONS`.
  - The second word after `node`/`nodes` → `list`, `add`.
  - The value of `--node` → `data("nodes") + ["auto"]`.
  - `lab_data()` adds `"nodes": node_names()`.
- **README**: a "Several hosts" section:
  - `pvelab node add ADDR` (what it checks, the ports to open, passwords typed only into the prompts);
  - placement (`--node`) and template copies;
  - manual node removal: move off or destroy its VMs; power the host off; `pvecm delnode NAME` on workshops;
    `rm -r /etc/pve/nodes/NAME`; the next `create` refreshes the SDN peers;
  - the two-host quorum note with `pvecm expected 1`, and QDevice (`corosync-qnetd` on a third machine, then
    `pvecm qdevice setup IP`).
- **`tests/selftest.sh`**: a "two hosts" section before `== destroy`.
  - It runs only if `pvesh get /nodes --output-format json` lists 2 or more nodes. Otherwise it prints `SKIP two-host`.
  - It uses self-test nets 95 (isolated) and 96 (`--no-isolate`).
  - Helpers: `hostof NAME` (from `pvesh get /cluster/resources --type vm`), and `N2` = the other node.
  - Steps:
    - `create 4 --net 95`:
      - check both hosts are used;
      - check that `list` shows `HOST`.
    - `create 2x2 --net 96 --no-isolate`: each student's `-1` and `-2` have the same `hostof`.
    - `create 1 --net 95 --node $N2` puts its VM on N2. `create 1 --net 91 --node $N2` is refused when net 91 is
      vmbr (`LAB_SDN=0` mode).
    - Cross-host: a student on host 1 pings a net95 student on host 2 → fails. On net96 → works. A VM on N2 pings
      10.10.95.1 → works.
    - On a VM on N2:
      - `curl` works (internet);
      - `expose --net 95` → the DNAT rule exists, and `sshpass ssh -p PORT student@<uplink IP> true` works;
      - `block internet` → `curl` fails; `unblock all` → it works again;
      - `run VM $SD/mark.sh` → `/root/mark-ran` exists.
    - `destroy --net 95 --net 96`. Then, on N2 through `ssh root@N2`:
      - no `/etc/pvelab/nft/net95.nft`;
      - no `pvelab_net95` table;
      - no `net95-`/`net96-` VM configs.
    - A template copy that the section itself made on N2 is deleted and checked gone.
    - The existing restore checks then cover workshops.

### Tests: additions to `test_ux.py`
- Completion:
  - `node <TAB>` → list, add;
  - `create 2 --node <TAB>` → auto + the node names;
  - `test_completion_commands_match_cli` and `test_completion_options_are_documented` pass.
- Wizard:
  - with `multi()` mocked true and an SDN net, create asks "Where" and the argv ends with `--node auto` / `--node pve2`;
  - with a pinned net, nothing is asked;
  - `Wizard.nodes` returns `["node", "add", "10.0.0.229"]`.
- `command_help("node")` mentions `pvecm expected 1` and the UDP ports.
