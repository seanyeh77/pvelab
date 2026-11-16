# pvelab

pvelab manages VMs on a Proxmox VE host or cluster from one command line (or a menu). It makes VMs from
cloud-image templates and keeps track of them: the networks they are on, whose VMs they are (owners) and
their logins, isolation between owners, ssh access from outside, snapshots and rollbacks, and templates of
your own. It only ever touches the VMs it made.

The words it uses:
- **network** (net): a NAT network behind the host, by number (`net40`) or name (`prod`); its VMs are
  named after it (`net40-user01`).
- **owner**: whose VMs these are. An owner's VMs on a net share one password and one login card, reach
  each other on an isolated net, and stay on one host. `owner=KEY` in the VM's PVE description records it.
- **user**: the login in a VM (`user` by default).

A classroom is one way to use it: see [Example: a classroom](#example-a-classroom).

## Install

```bash
curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash    # -> ~/.local/bin/pvelab, uses sudo itself
pvelab              # menu; Create is one form: every answer on screen, the command it runs below
pvelab help [CMD]   # every command, with examples
pvelab upgrade
```

## Features

- **Create**: Debian/Ubuntu cloud images, one password per owner, snapshots `initial` and `clean`.
- **Menu**: `pvelab` alone. Create shows all its answers in one form under the cursor (not full screen):
  Up/Down move, typing edits, Left/Right change a choice, Space ticks a box, so any answer can be changed in any
  order. Mistakes show in yellow as you type; the command and its warnings update below; Create VMs, or Esc
  twice to leave. Every
  other entry asks step by step. Without a terminal (or too small a one) Create asks step by step too.
  In every list typing filters it (`des` leaves Destroy VMs; `05` the VMs ending in 05) and a long list
  scrolls. "Which VMs" offers a whole net, all VMs, or VMs ticked with Space in a list.
- **Templates**: `pvelab pull` builds them, and updates them when a newer image is out. VMs already made are not changed.
- **Your own templates**: `pvelab save-template VM NAME` saves a set-up VM (software, Docker images) as a template;
  VMs made from it share its data on LVM-thin, so it is stored once, not per VM.
- **Several VMs per owner**: `20x3` (20 owners x 3 VMs) gives `net40-user01-1..3`, ... Each of the 3 can have its
  own OS and size: `ubuntu24,debian13,debian13 --memory 8G,2G,2G` (one value per VM, in order).
- **Networks**: each net is behind NAT. By number, `--net 40` is `net40` on 10.10.40.0/24. By name,
  `--net docker-lab --cidr 192.168.50.0/23` makes a new net called docker-lab with its own range (private, /16
  to /27; default 10.10.N.0/24). VMs are named after the net (`docker-lab-user01`) and get addresses from .11
  up; the gateway is the first address. A name works wherever a number does. Before anything is made, the range
  is checked against vmbr1, the other nets, SDN subnets and every host's routes; `list` warns in red if something
  overlaps a net later. `pvelab net set` and `pvelab net rename` change a net later.
  On PVE with SDN a net is a VXLAN vnet `labN` (the same network on every node of a cluster); otherwise, or
  with `LAB_SDN=0`, a bridge `vmbrN`.
- **Other people's SDN changes**: pvelab holds PVE's SDN lock while it changes SDN and never applies changes
  someone else left pending in the web UI (Datacenter → SDN). If there are any, it lists them and stops before
  anything is made; apply or roll them back, then run pvelab again.
- **Isolation**: on by default. Each owner reaches only their own VMs; `--no-isolate` lets all VMs of a net
  reach each other. `pvelab create --shared --net 30` adds one VM, `net30-shared`, that every owner of the net
  reaches (a shared Git or CI server); owners still can't reach each other.
- **ssh from outside**: `--expose` opens port 22000+VMID on the host.
- **Hardening**: mining-pool ports are always blocked. The `harden` script (`--script harden`) makes
  passwords need 10+ characters (no dictionary words, no `aaaa`/`1234` runs) and bans an address
  for an hour after 5 failed ssh logins (fail2ban; never the gateway, where users come in through the host). Templates restrict nothing.
- **Control**: start/stop, roll back, run scripts as root in every VM, block internet/ssh, resize.
- **Live progress**: while a command works on many VMs, each VM has its own line - its stage, how long, and
  (dim) the newest line it printed, e.g. `script 2/2 docker  0:48  Unpacking docker-ce ...`; one that prints
  nothing for 2 minutes turns yellow. A command shows its plan and every warning first and asks once, and
  ends with what to do next. Everything is logged in `/root/lab/logs/`.
- **Watching**: `list` (status, IPs, ports) and `top` (CPU/memory/network; flags busy VMs). Under the table,
  `list` says why each VM that is not running is down, from PVE's tasks and the host's journal: stopped or shut
  down by whom, start failed (and why), paused because a disk write failed while the storage was full, killed
  because the host ran out of memory, powered off inside the VM. Each line ends with the command that brings
  it back. `pvelab start` also resumes a VM that is paused (a failed disk write once space is free again, or a
  suspended VM).
- **Status**: `pvelab status [--net N] [--since 1h] [-f]` shows in one place the hosts and their cluster links,
  who is logged in to which VM (from where, for how long, internet or not), and login attempts: failed ssh and
  web UI logins on each host, and failed ssh logins on exposed VMs, with outside IPs apart from inside ones.
  A user's own IP is counted as a mistyped password, not an attack; a login after many failures is red.
  `-f` refreshes it like `top`. It only looks and changes nothing.
- **Handout**: printable login cards, one per owner.
- Safe to try: `--dry-run` for destroy/resize/rollback/move, bash tab completion.

## Usage

```bash
pvelab create 20x2 ubuntu24 --net 40 --cores 4 --memory 8G --disk 30 --expose --script harden+setup.sh
pvelab create 20x2 ubuntu24,debian13 --net 41 --cores 4,1 --memory 8G,2G   # -1 big, -2 small
pvelab create 15 docker-lab --net web101 --cidr 192.168.50.0/24 --expose  # a named net with its own range
pvelab list                                   # ... and why any VM is not running
pvelab list --ssh                             # ssh lines: public port if exposed, else the VM's address
pvelab list --ssh --exposed                   # only what users outside can reach, to hand out
pvelab handout --net 40                       # print, hand out, delete the file
pvelab run --net 40 lab2.sh                   # run a script as root in every VM
pvelab run --net 40 harden+docker             # or library scripts, one after the other
pvelab block internet --net 40                # cut a net off; unblock internet --net 40
pvelab rollback 'net40-user0*' --to clean     # undo the changes made in them
pvelab resize net40-user01..05 --memory 16G --reboot
pvelab top --check
pvelab status --net 40 -f                     # who is in, login attempts
pvelab destroy --net 40                       # the whole net
pvelab pull all                               # OS templates up to the newest images
pvelab save-template net40-user01 docker-lab && pvelab create 30 docker-lab --net 41
```

To pick VMs, use one of:
- a name: `net40-user01-2`
- a whole net: `--net 40`, `--net net40` or `--net web101`
- a range: `net40-user01..05`
- a pattern: `'net40-user*-1'`
- `all`

New VMs are named `NET-user01`, `NET-user02`, ... with the login `user`; `LAB_PREFIX` (the name) and
`LAB_USER` (the login) change that. A net that already has VMs keeps its own name prefix and login when
more are made.

## Owners, accounts and passwords

Each VM has an owner: `owner=KEY` in its PVE description (`user01` for `net40-user01` and
`net40-user01-1..3`). An owner's VMs share a password and a login card, and reach each other on an isolated
net; renaming a VM keeps its owner. To group VMs yourself:

```sh
pvelab owner web01 net2-db alice      # web01 and net2-db are alice's
pvelab owner --check                  # VMs made by an older pvelab: the owners and tags pvelab will record
```

By default each VM's login is `user` with a random password per owner. To give them yourself, a CSV
file (or an inline list) with one owner per line:

```
name,user,password
,s1101001,Pass-0001          # VM prod-s1101001
alice01,s1101002,            # VM alice01, a random password
,root,Root-pass-99           # root: pvelab lets root log in over ssh with its password
```

```sh
pvelab create --net prod --accounts accounts.csv           # the count is the number of lines
pvelab create 1x3 --net prod --user root,user,user         # or the login per VM, --password likewise
pvelab passwd --check --net prod                           # did users change their passwords?
pvelab passwd prod-s1101001 --password 'New-pass-2026'     # a new one (all of that owner's VMs)
pvelab passwd --net prod --keep-changed                    # new random ones, but not for owners who chose theirs
```

In the menu: "Create VMs from a list of accounts" and "Passwords".

A VM's own new name (its hostname too; its owner, IP and password stay): `pvelab rename prod-s1101001
prod-alice` (menu: "Rename a VM"). A running VM reboots once.

`passwd` sets the password in a running VM (chpasswd) and leaves PVE's cloud-init password of the VM as
it was, so the next boot does not undo a password the user picks later; the VM's snapshots get the new
one, so it is right after a rollback too. A stopped VM takes it at its next start.

## Networks

A network can be made before its VMs, with its own range and gateway (in PVE's web UI the
vnet's alias is the name):

```sh
pvelab net create prod --cidr 10.5.0.0/24 --gateway .254      # .254: 10.5.0.254
pvelab net create nonprod --cidr 10.5.1.0/24 --gateway .254
pvelab create 20 --net prod                                   # VMs get 10.5.0.11 up, gateway .254
pvelab net list                                               # name, CIDR, gateway, bridge, VMs
pvelab net set net2 --cidr 10.5.2.0/24 --gateway .254        # a network that has VMs (--dry-run first)
pvelab net rename net2 prod                                   # the VMs keep their names
pvelab net rename net2 prod --rename-vms                      # net2-user01 becomes prod-user01 too
pvelab net remove nonprod                                     # only without VMs
```

`net set` keeps the VMs: each keeps its place in the range (.11 stays .11), its address changes in its
config and snapshots, and the running ones reboot onto it with their passwords and SSH host keys kept.
Stopped VMs take it at their next start (with the handout password again). Do it when nobody is using the
VMs: they reboot.

`net rename` changes the net's name, its vnet's alias and its VMs' tag `net-NAME`; the VMs keep their
names, IPs, passwords and ssh ports, and nothing reboots. With `--rename-vms` the VMs named after the net
are renamed too (configs, snapshots, credentials); the running ones reboot, as their hostname is their
name, keeping passwords and SSH host keys. Print the handout again for the new names.

`net list` reads PVE's SDN config: vnets made in the web UI (any zone) are listed too, with their subnet,
gateway and SNAT. `pvelab create 20 --net prod` on such a vnet (by its alias or id) takes it over: its VMs
get pvelab's isolation, ssh forwards and blocks, while the range, gateway and SNAT stay PVE's (net set and
rename change them in PVE). Destroying its last VM removes only pvelab's rules; the vnet stays.

`create --net NEW --cidr ... --gateway ...` makes one too. Every network has NAT to the internet; networks
never reach each other. Also in the menu: "Networks".

## Script library

Scripts that come with pvelab, run as root in the VMs:
- `pvelab scripts` lists them; `pvelab scripts harden` prints one.
- `pvelab create 20x2 --net 40 --script harden+docker,` runs `harden` then `docker` on every `-1`,
  and nothing on `-2` (a comma separates an owner's VMs, `+` chains scripts).
- `pvelab run VM... harden+docker` runs them on VMs that already exist.
- A name can also be a file: `--script harden+./setup.sh`. The output says which one was used.
- New and changed scripts arrive with `pvelab upgrade`.

To add one, put `scripts/NAME.sh` in the repo: `#!` on line 1, and on line 2 a `# ` comment in
English that says what it does (shown by `pvelab scripts`). Comments must be English (ASCII);
the unit tests check this.

## Several hosts

pvelab runs on one PVE host (workshop1) and can put VMs on other PVE hosts of its cluster too.
Nothing is installed on the other hosts; pvelab reaches them with the cluster's own root ssh.

Add a host, from workshop1:

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
- Then it creates the cluster on workshop1 if there is none (asks for a typed yes; the guests keep running),
  joins the host, waits for quorum, and prepares the host: vendor-data snippet, isolation tables, and the
  SDN peers (the network is applied on all hosts).
- Safe to run again, for example after fixing a failed check.

Placement:
- `create` spreads new owners over the hosts by free memory and disk (`--node auto`, the default), or puts
  them all on one host with `--node NAME`. An owner's VMs are always on one host; existing owners never move.
- Only SDN nets (`labN`) span hosts. Bridge nets (`vmbrN`, `LAB_SDN=0`) and VMs without `--net` stay on workshop1.
- A host that lacks a template gets a copy first (pvelab says how big and asks). The copy has its own VMID and
  the same name. `pvelab templates` shows which hosts have each template; `delete-template` removes the copies too.

Moving VMs to another host (to balance the hosts, or to empty one; also in the menu with 2 or more hosts):

```bash
pvelab move 'net95-user05*' --to pve2 --dry-run      # one owner: only the plan
pvelab move --net 95 --to pve2                       # a whole net
pvelab move all --to workshop1                       # everything back to workshop1
```

- VMs are picked as for the other commands. An owner's VMs always move together, to the same host: naming one
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
  line), start, snapshot `clean`. If one fails, its line says which step and what to do, that owner's other
  VMs are not moved, and the next owner goes on. Then the nets' isolation tables are updated on every host.

Updating PVE on the hosts, from workshop1 (one host at a time, workshop1 last; hosts may reboot):

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
- For each host: if users are logged in to its VMs (guest agent `who`), it asks first (default no;
  `LAB_YES=1` skips the question). With `--major` it moves the apt sources to the new release (the old files
  stay next to them as `*.pvelab-bak`). Then a non-interactive `apt-get dist-upgrade` (old config files kept),
  a reboot after a new kernel or a major upgrade, and a wait until the host is back with the new version.
  The dist-upgrade runs in a systemd unit of its own (`pvelab-upgrade`, output in `/var/log/pvelab-upgrade.log`),
  so a dropped ssh session or Ctrl-C stops only the watching, never apt half-way.
- When workshop1 itself reboots, pvelab ends there: once it is back, `pvelab node upgrade` checks that nothing
  is left. Hosts already current are skipped. On a failure it stops, names the host and the step, and says how to recover.
  The log: `/root/lab/logs/node-upgrade-*.log`.

Removing a host that is gone for good, from a host that stays (`--dry-run` only shows the plan):

```sh
pvelab node remove workshop2 --dry-run
pvelab node remove workshop2
```

- First, read-only: the host is offline (power it off), has no VMs or containers (move or destroy them while
  it is online; pvelab's template copies may stay), and another host is online. Any problem: nothing is changed.
- The plan is asked once. config.db, `/etc/corosync`, `corosync.conf` and `/etc/pve/nodes/NAME` are backed up
  to `/root/lab/node-remove-backup-<time>/`.
- Then `pvecm expected 1` if the hosts left have no quorum, `pvecm delnode NAME`, `/etc/pve/nodes/NAME` and the
  host's ssh keys in `/etc/pve/priv/known_hosts` removed, and its IP taken out of the SDN zone's VXLAN peers.
  That SDN apply reloads every host's network: users on SDN nets may lose the connection for a few seconds.
- The removed host still has the cluster's configuration: never start it on this network as it is.
  Reinstall it first; `pvelab node add` can join it again then.

Renaming a host, from workshop1 (it asks first; `--dry-run` only shows the plan; also in the menu: "Nodes", then "rename a host"):

```sh
pvelab node rename pve9 workshop2         # first the other host
pvelab node rename workshops workshop1    # then this one
```

- The new name must be a valid host name, not used in the cluster; the domain in `/etc/hosts` is kept.
- A script on the host that writes `/etc/hosts` with the old name in it (a boot script with a fixed `FQDN=`)
  stops the rename before anything changes: at boot it would put the old name back and pve-cluster would not
  start under the new one. Make it follow the hostname (`$(hostname -s)`), then rename again.
- If pve-cluster does not come up after the reboot, any pvelab command says why (`/etc/hosts`, such a
  script) and what to do. Fix it, then reboot (restarting pve-cluster alone leaves pvestatd, pve-guests and
  the other PVE services that failed without it failed); the next pvelab command finishes the rename.
- After the reboot (and at the end of `node add`), PVE's services that start before pve-cluster answers
  and then stay failed (pvestatd: the host shows `?` in the cluster) are restarted: pvelab checks pvestatd,
  pvedaemon, pveproxy, pve-firewall, pvescheduler, pve-ha-lrm and pve-ha-crm, and names any still down.
- The plan shows the steps, the reboots, the VMs that restart, the users logged in (`who` through the
  guest agent) and the backups; one typed yes starts it.
- Before any change each host is backed up to `/root/lab/rename-backup-<time>/<host>/`: `config.db`,
  `/etc/corosync`, `/etc/pve/corosync.conf`, `/etc/hostname`, `/etc/hosts`, `/etc/pve/nodes/OLD`.
- A host with no VMs (template copies are deleted; they are made again when needed) leaves the cluster
  (PVE's documented separation, then `pvecm delnode`), gets its new name, reboots and joins again the way
  `node add` does (it asks for workshop1's root password unless the host's root key is trusted), then is prepared again.
- A host with VMs, or workshop1 itself, is renamed in place: hostname and hosts, the node's name in
  `corosync.conf`, its VMs shut down and their configs moved to `/etc/pve/nodes/NEW`, reboot. After the
  reboot: `pvecm updatecerts --force`, checks (the configs, `pvecm nodes`, quorum), the VMs that ran are
  started again, and `/etc/pve/nodes/OLD` is removed. When workshop1 itself reboots, log in again and run
  any pvelab command (`pvelab node list`, or `pvelab node rename` alone): it finishes the rename.
- If a step fails, pvelab says what state each host is in and how to restore from the backup.

With two hosts, when one is down the other has no quorum: VMs keep running, but nothing can be started
or changed. On the host that is up: `pvecm expected 1`. A third vote avoids this: install `corosync-qnetd`
on a third machine, then `pvecm qdevice setup IP` on a host.

## Example: a classroom

pvelab was first written for a classroom, and it still fits one well: each class gets its own network,
each student is an owner, and the login cards are the handout.

```sh
LAB_PREFIX=student LAB_USER=student pvelab create 20x2 ubuntu24 --net 40 --expose --script harden
                                              # net40-student01-1..2 .. net40-student20-1..2, login student
pvelab create --shared --net 40               # net40-shared: the class's Git server, every student reaches it
pvelab handout --net 40                       # one card per student: ssh commands and password
pvelab block internet --net 40                # exam: the whole class offline; unblock internet --net 40
pvelab status --net 40 -f                     # during class: who is in, login attempts
pvelab rollback 'net40-student05*' --to clean # undo a student's changes
pvelab destroy --net 40                       # end of term
```

`LAB_PREFIX` and `LAB_USER` are needed only for the first VMs of a net: a net keeps its prefix and login when
more VMs are added (`pvelab create 25 --net 40` adds student21-25).

With the students' own accounts, `--accounts` takes the list from the school system, one student per line:

```
name,user,password
,s1101001,                   # VM net40-s1101001, login s1101001, a random password
,s1101002,
```

```sh
pvelab create --net 40 --accounts students.csv
pvelab passwd --check --net 40                # did students change their passwords?
pvelab passwd net40-s1101001                  # a student forgot theirs: a new one on all their VMs
```

Students are kept apart by default (each reaches only their own VMs); for group work,
`pvelab isolate --net 40 --off`. Changes that reboot VMs (`net set`, `net rename --rename-vms`, `node
upgrade`) are best done outside class time.

## Develop

```bash
PYTHONPATH=src python3 -m pvelab help    # run from the checkout
pyflakes src tests && pytest             # unit tests, no PVE needed
python3 build.py                         # -> dist/pvelab (one-file zip app)
sudo tests/selftest.sh                   # end-to-end on the PVE host, everything (self-test nets 91-96 only)
sudo tests/selftest.sh --quick           # the core only: one create with a script, isolation, power, destroy
sudo env LAB_SDN=0 tests/selftest.sh --net-only   # the bridge (vmbrN) mode: only what depends on it
git push                                 # CI publishes to GitHub Pages; hosts: pvelab upgrade
```

Code: `src/pvelab/` (one module per area, `commands/` one per command).
