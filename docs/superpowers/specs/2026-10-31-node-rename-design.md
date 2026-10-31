# pvelab node rename: rename a PVE host in the cluster

Date: 2026-10-31. Status: agreed in chat. Reboots are allowed.

## Why

The instructor dislikes the host names. They want:
- workshops (10.0.0.123) renamed to workshop1
- pve9 (10.0.0.229) renamed to workshop2.

Both hosts are in a 2-node cluster called "pvelab", created on workshops. workshops holds every running class: net30 (35 VMs) and net40 (10 VMs), plus the templates. pve9 holds no class VMs. It may hold template copies (VMID 9100 and up) and self-test leftovers.

PVE ties a node's name to many things:
- `/etc/pve/nodes/<name>/` (VM configs, certificates)
- corosync.conf's nodelist
- `/etc/hostname`, `/etc/hosts`
- the RRD data
- the SSH known_hosts and authorized_keys in `/etc/pve/priv`
- SDN, which is cluster-wide and refers to nodes by IP. pvelab's zone uses IPs as peers.

PVE's advice is that a node with guests should not be renamed in place: remove it from the cluster, rename it, then join it again. Renaming a node in place is possible, but it has to be done carefully.

## Goals

1. **`pvelab node rename OLD NEW [--dry-run]`**, run on workshops.
   - NEW must be a valid hostname, unused in the cluster.
   - The domain part of the FQDN in `/etc/hosts` is kept.
2. **A plan first**, then one typed `yes`, as in node add. The plan lists:
   - every step
   - the reboots
   - the VMs that will restart
   - the logged-in students, read through the guest agent's `who`
   - the backups that will be made.
3. **Backups before any change**, in `/root/lab/rename-backup-<time>/` on workshops:
   - `/var/lib/pve-cluster/config.db`
   - `/etc/corosync`, `/etc/pve/corosync.conf`
   - `/etc/hostname`, `/etc/hosts`
   - every VM config in `/etc/pve/nodes/OLD/qemu-server/`

   For the other host, the copies are made on that host and copied to workshops.
4. **A host with no pvelab VMs (pve9):** leave the cluster and join again under the new name.
   1. If it holds template copies, they are deleted. They are made again on demand.
   2. On workshops: `pvecm delnode OLD`.
   3. On the host: clean its cluster state the documented way:
      - stop pve-cluster and corosync
      - `pmxcfs -l`
      - remove `/etc/corosync/*` and `/etc/pve/corosync.conf`
      - `killall pmxcfs`
      - start pve-cluster
   4. Set the hostname and `/etc/hosts`, and reboot.
   5. Rejoin with `node add`'s join path, using the cluster's root ssh while it still works, or else asking for credentials like node add does.
   6. Refresh the SDN peers and push isolation to the host, as node add's prepare step does.
5. **A host with VMs, or the one pvelab runs on (workshops):** rename in place, following the documented manual procedure:
   1. Stop nothing ahead of time. The reboot restarts its VMs, which have onboot=1.
   2. Change `/etc/hostname` and `/etc/hosts`.
   3. Edit corosync.conf in `/etc/pve`:
      - change the node's `name:`
      - bump `config_version`
      - the change spreads to the other node.
   4. Create `/etc/pve/nodes/NEW/` and move `qemu-server/*.conf` (and `lxc/*.conf`) from OLD to NEW.
   5. Reboot.
   6. After the reboot:
      - run `pvecm updatecerts --force`
      - check that `/etc/pve/nodes/NEW` holds the VMs
      - check that `pvecm status` shows NEW and the cluster is quorate
      - check that every pvelab VM is listed and starts
      - remove the old `/etc/pve/nodes/OLD` once it is empty, keeping its certificates in the backup.
   7. pvelab's own state that names hosts must be updated, if there is any. At least: credentials, `blocks.json`, the nft files, the SDN zone peers (by IP, so unchanged), and the template copies' descriptions.

   **Because workshops reboots under pvelab,** the rename is resumable. pvelab writes a small state file before the reboot. After the reboot, `pvelab node rename` (or any pvelab command) notices the unfinished rename and finishes the post-reboot steps.
6. **Order when renaming both:** the instructor runs pve9 → workshop2 first, then workshops → workshop1. The README and the help say so.
7. **On failure,** say what state each host is in and give exact restore steps from the backup:
   - copy back config.db (with pve-cluster stopped)
   - restore the hostname and hosts files
   - reboot.
8. **One log per run.** The summary at the end shows the old and new names, and `pvelab node list`.

## Non-goals

- Changing a host's IP address.
- Renaming several hosts in one command.
- Renaming hosts that are not in a cluster. That is plain Debian work, but it still has to move `/etc/pve/nodes/OLD` → `NEW`. It is fine to support it with the in-place path.

## Anti-goals

- VMs that become invisible or orphaned, with configs left under a node that no longer exists, and no way back.
- Losing quorum for good. Two hosts with mismatched corosync configs is the risk.
- Starting while students are logged in, without a yes.
- Doing anything without a backup.

## Constraints

- **The research must be grounded:** PVE's own Perl sources under `/usr/share/perl5/PVE` (pvecm, Cluster, corosync handling) and PVE's documentation on removing and re-adding a node and on renaming a node.
- **Tests:**
  - Unit tests mock every host command.
  - Nothing runs on real hosts during development.
  - The real renames are done later with the instructor present.
- **Reuse:** the `joining.py` join path, `nodes.run_on`, the `ui` Plan, Board and Spinner, and `start_log`.

## Left to the builder

- The module layout.
- The format of the state file used to resume.
- The exact checks after each step.
