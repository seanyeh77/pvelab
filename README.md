# pvelab

Student VMs on a Proxmox VE node: clone from cloud-image templates, isolated NAT nets,
ssh from outside, snapshots/rollback, instructor scripts, internet/ssh blocking.

## Install (on the PVE host)

```bash
curl -fsSL https://seanyeh77.github.io/pvelab/install.sh | bash
pvelab              # usage
pvelab upgrade      # later: download the latest version
```

Root gets `/usr/local/bin/pvelab`, other users `~/.local/bin/pvelab` (pvelab asks before using sudo).
The installed `pvelab` is one file: the package below as a Python zip app (`python3 build.py`).

## Layout

```
src/pvelab/
  cli.py              entry point: dispatch, root / PVE checks, lock
  config.py           settings (LAB_* environment variables)
  catalogue.py        OS templates - add a Debian/Ubuntu release here
  helptext.py         overview and per-command help
  args.py             option parsing shared by all commands (--x V / --x=V, --net N)
  util.py             errors, running commands, downloads, prompts, sudo
  inventory.py        managed VMs; VM arguments (names, --net N, patterns, all) -> VMIDs
  credentials.py      /root/lab/credentials.tsv, host ssh key
  ifaces.py           pvelab's sections in /etc/network/interfaces
  networks.py         NAT bridges vmbrN and the DHCP bridge vmbr1
  expose.py           ssh from outside (DNAT 22000+VMID)
  blocks.py           cutting internet / ssh per VM
  resources.py        CPU / memory / disk sizes, host checks
  templates.py        building templates from cloud images
  scripts.py          instructor scripts via the guest agent
  lifecycle.py        clone, first boot, snapshots, remove
  commands/           one module per command (create, listing, access, run, ...)
tests/unit/           pytest, no PVE needed
tests/selftest.sh     end-to-end on a real PVE host (makes and destroys net91/92 VMs, ~20 min)
build.py              -> dist/pvelab (reproducible zip app)
install.sh            the installer served by GitHub Pages
.github/workflows/    on push to main: pyflakes, pytest, build, publish to GitHub Pages
```

## Develop

```bash
PYTHONPATH=src python3 -m pvelab help     # run from the checkout
pip install pytest pyflakes && pyflakes src tests && pytest
python3 build.py                          # -> dist/pvelab
sudo tests/selftest.sh                    # on the PVE host; tests dist/pvelab
git push                                  # Pages publishes it; hosts update with: pvelab upgrade
```
