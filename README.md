# pvelab

Classroom VMs on one Proxmox VE node: one command makes a VM (or several) per student, on its own NAT net.

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
- **Watching**: `list` (status, IPs, ports) and `top` (CPU/memory/network; flags busy VMs).
- **Handout**: printable login cards.
- Safe to try: `--dry-run` for destroy/resize/rollback, bash tab completion.

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

## Develop

```bash
PYTHONPATH=src python3 -m pvelab help    # run from the checkout
pyflakes src tests && pytest             # unit tests, no PVE needed
python3 build.py                         # -> dist/pvelab (one-file zip app)
sudo tests/selftest.sh                   # end-to-end on the PVE host (net91/92/94, ~20 min)
git push                                 # CI publishes to GitHub Pages; hosts: pvelab upgrade
```

Code: `src/pvelab/` (one module per area, `commands/` one per command).
