# pvelab

Classroom VMs on a Proxmox VE node (or several): one command makes a VM (or several) per student, on its own NAT net.

## Install

```bash
curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash    # -> ~/.local/bin/pvelab, uses sudo itself
pvelab              # step-by-step menu
pvelab help [CMD]   # every command, with examples
pvelab upgrade
```

## Features

- **Create**: Debian/Ubuntu cloud images, one password per student, snapshots `initial` and `clean`.
- **Templates**: `pvelab pull` builds them, and updates them when a newer image is out. VMs already made are not changed.
- **Your own templates**: `pvelab save-template VM NAME` saves a set-up VM (software, Docker images) as a template;
  VMs made from it share its data on LVM-thin, so it is stored once, not per student.
- **Several VMs per student**: `20x3` gives `net40-student01-1..3`. Each of the 3 can have its own OS and size:
  `ubuntu24,debian13,debian13 --memory 8G,2G,2G` (one value per VM, in order).
- **Networks**: each class gets 10.10.N.0/24 behind NAT. On PVE with SDN it is a VXLAN vnet `labN`
  (the same network on every node of a cluster); otherwise, or with `LAB_SDN=0`, a bridge `vmbrN`.
- **Isolation**: on by default. Each student reaches only their own VMs; `--no-isolate` is for group work.
- **ssh from outside**: `--expose` opens port 22000+VMID on the host.
- **Hardening**: mining-pool ports are always blocked. The `harden` script (`--script harden`) makes
  passwords need 10+ characters. Templates restrict nothing.
- **Class control**: start/stop, roll back, run scripts as root in every VM, block internet/ssh, resize.
- **Live progress**: while a command works on many VMs, each VM has its own line - its stage, how long, and
  (dim) the newest line it printed, e.g. `script 2/2 docker  0:48  Unpacking docker-ce ...`; one that prints
  nothing for 2 minutes turns yellow. A command shows its plan and every warning first and asks once, and
  ends with what to do next. Everything is logged in `/root/lab/logs/`.
- **Watching**: `list` (status, IPs, ports) and `top` (CPU/memory/network; flags busy VMs).
- **Status**: `pvelab status [--net N] [--since 1h] [-f]` shows in one place the hosts and their cluster links,
  who is logged in to which VM (from where, for how long, internet or not), and login attempts: failed ssh and
  web UI logins on each host, and failed ssh logins on exposed VMs, with outside IPs apart from inside ones.
  A student's own IP is counted as a mistyped password, not an attack; a login after many failures is red.
  `-f` refreshes it like `top`. It only looks and changes nothing.
- **Handout**: printable login cards.
- Safe to try: `--dry-run` for destroy/resize/rollback/move, bash tab completion.

## Usage

```bash
pvelab create 20x2 ubuntu24 --net 40 --cores 4 --memory 8G --disk 30 --expose --script harden+setup.sh
pvelab create 20x2 ubuntu24,debian13 --net 41 --cores 4,1 --memory 8G,2G   # -1 big, -2 small
pvelab list --ssh
pvelab handout --net 40                       # print, hand out, delete the file
pvelab run --net 40 lab2.sh                   # run a script as root in every VM
pvelab run --net 40 harden+docker             # or library scripts, one after the other
pvelab block internet --net 40                # exam time; unblock internet --net 40
pvelab rollback 'net40-student0*' --to clean  # undo a student's changes
pvelab resize net40-student01..05 --memory 16G --reboot
pvelab top --check
pvelab status --net 40 -f                     # during class: who is in, login attempts
pvelab destroy --net 40                       # end of term
pvelab pull all                               # OS templates up to the newest images
pvelab save-template net40-student01 docker-lab && pvelab create 30 docker-lab --net 41
```

To pick VMs, use one of:
- a name: `net40-student01-2`
- a whole net: `--net 40`
- a range: `net40-student01..05`
- a pattern: `'net40-student*-1'`
- `all`

## Script library

Scripts that come with pvelab, run as root in the VMs:
- `pvelab scripts` lists them; `pvelab scripts harden` prints one.
- `pvelab create 20x2 --net 40 --script harden+docker,` runs `harden` then `docker` on every `-1`,
  and nothing on `-2` (a comma separates a student's VMs, `+` chains scripts).
- `pvelab run VM... harden+docker` runs them on VMs that already exist.
- A name can also be a file: `--script harden+./setup.sh`. The output says which one was used.
- New and changed scripts arrive with `pvelab upgrade`.

To add one, put `scripts/NAME.sh` in the repo: `#!` on line 1, and on line 2 a `# ` comment in
English that says what it does (shown by `pvelab scripts`). Comments must be English (ASCII);
the unit tests check this.

## Several hosts

pvelab runs on one PVE host (workshops) and can put VMs on other PVE hosts of its cluster too.
Nothing is installed on the other hosts; pvelab reaches them with the cluster's own root ssh.

Add a host, from workshops:

```bash
pvelab node add 10.0.0.229    # also in the menu: "Nodes"
pvelab node list              # each host: online, free memory and disk, pvelab VMs, templates
```

- It asks for the ssh user on the new host (a sudoer, or root). Passwords are typed only into the
  prompts of ssh, sudo and `pvecm add`; pvelab never reads or stores one.
- First it checks the new host, read-only, and changes nothing if any check fails: a unique hostname that
  resolves to its address, the same PVE version, no guests, storage `local-lvm` (LVM-thin), `/dev/kvm`,
  clocks in sync, and the ports. Open these between the two hosts (e.g. in the OpenStack security group):
  TCP 22 and 8006, UDP 4789 (VXLAN) and UDP 5405-5412 (corosync), both ways.
- Then it creates the cluster on workshops if there is none (asks for a typed yes; the guests keep running),
  joins the host, waits for quorum, and prepares the host: vendor-data snippet, isolation tables, and the
  SDN peers (the network is applied on all hosts).
- Safe to run again, for example after fixing a failed check.

Placement:
- `create` spreads new students over the hosts by free memory and disk (`--node auto`, the default), or puts
  them all on one host with `--node NAME`. A student's VMs are always on one host; existing students never move.
- Only SDN nets (`labN`) span hosts. Bridge nets (`vmbrN`, `LAB_SDN=0`) and VMs without `--net` stay on workshops.
- A host that lacks a template gets a copy first (pvelab says how big and asks). The copy has its own VMID and
  the same name. `pvelab templates` shows which hosts have each template; `delete-template` removes the copies too.

Moving VMs to another host (to balance the hosts, or to empty one; also in the menu with 2 or more hosts):

```bash
pvelab move 'net95-student05*' --to pve2 --dry-run   # one student: only the plan
pvelab move --net 95 --to pve2                       # a whole class
pvelab move all --to workshops                       # everything back to workshops
```

- VMs are picked as for the other commands. A student's VMs always move together, to the same host: naming one
  adds the others (the plan says so). VMs already on the target are skipped.
- Only SDN nets (`labN`) can move. VMs on a bridge net (`vmbrN`) or without `--net` are refused, as are a target
  that is offline or not in the cluster, and a move whose disks don't fit on the target's `local-lvm`.
- The costs, all in the plan before the one typed yes (`LAB_YES=1` answers it):
  - **snapshots are deleted** (PVE can't migrate a VM with snapshots on LVM-thin); the plan names them, and
    there is no rollback to them afterwards. A new `clean` is taken on the target.
  - **the disks become full copies** on the target, no longer linked clones of the template: the plan says how
    many GB that takes there.
  - running VMs are shut down, moved offline with `qm migrate`, and started again; the plan says who is logged in.
- One VM at a time, each on its own line: shut down, delete snapshots, migrate (with `qm migrate`'s newest
  line), start, snapshot `clean`. If one fails, its line says which step and what to do, that student's other
  VMs are not moved, and the next student goes on. Then the nets' isolation tables are updated on every host.

Updating PVE on the hosts, from workshops (one host at a time, workshops last; hosts may reboot):

```bash
pvelab node upgrade --dry-run    # only the checks and the plan
pvelab node upgrade              # within this major version (9.2 to 9.x); asks if a new major one is out
pvelab node upgrade --major      # the next major version (9 to 10), Debian's next release with it
pvelab node upgrade --minor      # this major version only, never asks
```

- It first checks every host, read-only: online and quorate, free space on `/` and `/boot`, `apt-get update`,
  and what would be upgraded. With `--major` also PVE's own checker (`pve9to10 --full`, no FAILURES) and that the
  new PVE and Debian releases are published; when there is no new major version yet, it says so and stops.
- Then the plan and one question. With two hosts, while one reboots the other has no quorum (`/etc/pve` is
  read-only there; running VMs keep running).
- For each host: if students are logged in to its VMs (guest agent `who`), it asks first (default no;
  `LAB_YES=1` skips the question). With `--major` it moves the apt sources to the new release (the old files
  stay next to them as `*.pvelab-bak`). Then a non-interactive `apt-get dist-upgrade` (old config files kept),
  a reboot after a new kernel or a major upgrade, and a wait until the host is back with the new version.
  The dist-upgrade runs in a systemd unit of its own (`pvelab-upgrade`, output in `/var/log/pvelab-upgrade.log`),
  so a dropped ssh session or Ctrl-C stops only the watching, never apt half-way.
- When workshops itself reboots, pvelab ends there: once it is back, `pvelab node upgrade` checks that nothing
  is left. Hosts already current are skipped. On a failure it stops, names the host and the step, and says how to recover.
  The log: `/root/lab/logs/node-upgrade-*.log`.

Removing a host (manual for now):
1. Destroy its VMs (`pvelab list` shows the host of each).
2. Power the host off.
3. On workshops: `pvecm delnode NAME`, then `rm -r /etc/pve/nodes/NAME`.
4. The SDN zone still lists its IP as a VXLAN peer until a new SDN net is made or `pvelab node add` runs.
   To drop it now, as root on workshops: `pvesh set /cluster/sdn/zones/pvelab --peers IP1,IP2` (the hosts left),
   then `pvesh set /cluster/sdn`.

Renaming a host, from workshops (it asks first; `--dry-run` only shows the plan):

```sh
pvelab node rename pve9 workshop2         # first the other host
pvelab node rename workshops workshop1    # then this one
```

- The new name must be a valid host name, not used in the cluster; the domain in `/etc/hosts` is kept.
- The plan shows the steps, the reboots, the VMs that restart, the students logged in (`who` through the
  guest agent) and the backups; one typed yes starts it.
- Before any change each host is backed up to `/root/lab/rename-backup-<time>/<host>/`: `config.db`,
  `/etc/corosync`, `/etc/pve/corosync.conf`, `/etc/hostname`, `/etc/hosts`, `/etc/pve/nodes/OLD`.
- A host with no VMs (template copies are deleted; they are made again when needed) leaves the cluster
  (`pvecm delnode`, then PVE's documented separation), gets its new name, reboots and joins again the way
  `node add` does (it asks for workshops' root password), then is prepared again.
- A host with VMs, or workshops itself, is renamed in place: hostname and hosts, the node's name in
  `corosync.conf`, its VMs shut down and their configs moved to `/etc/pve/nodes/NEW`, reboot. After the
  reboot: `pvecm updatecerts --force`, checks (the configs, `pvecm nodes`, quorum), the VMs that ran are
  started again, and `/etc/pve/nodes/OLD` is removed. When workshops itself reboots, log in again and run
  any pvelab command (`pvelab node list`, or `pvelab node rename` alone): it finishes the rename.
- If a step fails, pvelab says what state each host is in and how to restore from the backup.

With two hosts, when one is down the other has no quorum: VMs keep running, but nothing can be started
or changed. On the host that is up: `pvecm expected 1`. A third vote avoids this: install `corosync-qnetd`
on a third machine, then `pvecm qdevice setup IP` on a host.

## Develop

```bash
PYTHONPATH=src python3 -m pvelab help    # run from the checkout
pyflakes src tests && pytest             # unit tests, no PVE needed
python3 build.py                         # -> dist/pvelab (one-file zip app)
sudo tests/selftest.sh                   # end-to-end on the PVE host (net91/92/94, ~20 min)
git push                                 # CI publishes to GitHub Pages; hosts: pvelab upgrade
```

Code: `src/pvelab/` (one module per area, `commands/` one per command).
