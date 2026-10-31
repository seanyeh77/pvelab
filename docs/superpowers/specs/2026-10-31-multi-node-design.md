# pvelab on several PVE hosts: design

Date: 2026-10-31. Status: agreed in chat; builder review done (decisions recorded under "Decisions after review").

## Why

- pvelab runs every classroom VM on one host: workshops, at 10.0.0.123.
  - That host is an OpenStack VM with a 120 GB disk, and its thin pool is often 80–97% full.
  - The instructor does not want a bigger disk. They want more PVE hosts instead.
  - A second host is ready: a PVE install at 10.0.0.229 that is not in any cluster yet.
- The instructor wants to manage everything from workshops with pvelab:
  - Add a host: `pvelab node add 10.0.0.229`.
  - Have pvelab create the cluster if there is none yet.
  - Then create, run and control student VMs on any host, without logging in to the other hosts.
- The SDN work (VXLAN zone "pvelab", vnet labN, pvelab's gateway) was built for this.
  - It has only been tested on a single node so far.

## Goals

1. **`pvelab node add ADDR` on workshops.**
   - It asks, one at a time, for the facts it needs:
     - the SSH user on the new host. This may be a sudoer rather than root; empty means root.
     - the passwords, typed into the real terminal prompts of ssh, sudo and `pvecm add`. pvelab never reads or stores a password.
   - Then it does the following, in order:
     1. **Checks the new host, read-only.** If any check fails, it lists every problem with its fix and changes nothing. The checks:
        - the hostname is unique and resolves to ADDR rather than 127.0.1.1
        - PVE has the same major.minor version
        - there are no guests (`qm list` and `pct list` are empty)
        - `/dev/kvm` exists
        - corosync UDP 5405–5412 and VXLAN UDP 4789 are reachable in both directions
        - TCP 22 and 8006 are reachable
        - the clocks are in sync
     2. **Creates the cluster if needed.** If workshops is not in a cluster, it asks, then runs `pvecm create`.
     3. **Joins the new host.** It runs `pvecm add <workshops IP> --link0 ADDR` on the new host through ssh and sudo.
     4. **Verifies the result.** It checks that both hosts are online and the cluster is quorate.
     5. **Prepares the new host for pvelab.** It:
        - writes the vendor-data snippet
        - adds the new host to the SDN zone's peers and applies
        - checks that each pvelab net is up on the new host
     6. **Warns about two-host quorum.** If one host goes down, the other cannot change `/etc/pve`. It gives the `pvecm expected 1` way out, and suggests a QDevice later.
   - It is safe to run again, for example after fixing a failed check. If the host is already a member, it skips straight to steps 4–5.
2. **`pvelab node list`** shows each host with:
   - online or offline
   - free memory and free disk in the pvelab storage
   - the number of pvelab VMs
   - its templates
3. **`pvelab nodes` also works.** Either form is fine; the builder picks one form and keeps the other as an alias.
4. **Every pvelab command runs on workshops and also acts on VMs on other hosts.** That covers create, list, start, stop, reboot, destroy, run, snapshot, rollback, resize, top, handout, ssh, block, isolate, expose and save-template.
   - Nothing needs pvelab installed on the other hosts.
5. **Placement in `create`.**
   - **A student's VMs always share one host.** This avoids latency between them, and it is the instructor's rule.
   - **A class may span hosts.**
   - **Default, `--node auto`:** each student goes as a whole to the host with the most room. Room means free memory and disk after the VMs already placed in this run, so that students spread across the hosts.
   - **`--node NAME`:** puts every new student of this run on NAME.
   - **New students in an existing class** are placed the same way. Existing students never move.
   - **When not everything fits:** the current resource warning asks first, as it does today.
6. **Templates are copied on demand.**
   - If the chosen host lacks the template a VM needs, pvelab copies it there before cloning. It says how big the copy is and asks first. The copy gets a new VMID (see "Decisions after review") but keeps the template's name.
   - This applies to both catalogue templates and saved templates.
   - Each linked clone then uses its host's own copy.
   - `pull` acts on workshops, as today. `save-template` makes the template on the source VM's host. `templates` shows which hosts have each template.
7. **The wizard changes as the chat showed.**
   - A new menu entry: "Nodes: add a PVE host, or see each host's free space".
     - It shows the host table, then runs the add flow with the checks shown as ✔/✘.
     - It asks before it creates the cluster and before it joins.
   - **create** asks a "Where" question, only when there are 2 or more hosts:
     - It shows a table of each host's free memory and disk, and how many students of this size fit.
     - The choices are: spread by free space (recommended), or all on one named host.
     - It warns when a template must be copied first.
     - The printed command line includes `--node`.
8. **`list` gets a `host` column.** The wizard's VM lists show it as well.
9. **Networking works across hosts for SDN nets (labN).**
   - **Same-student traffic** stays on one host. It never crosses hosts, because of goal 5.
   - **Isolation** (`isolate`): traffic between different students is dropped on every host.
     - That includes traffic that arrives over VXLAN, and traffic from a VM on host B to a VM on host A.
     - Group-work nets (`--no-isolate`) let VMs on different hosts reach each other.
   - **Internet, the gateway, NAT, mining-port drops, `block` and `expose`** work for a VM on any host. The builder decides where each rule lives. The likely answer is workshops, because the gateway .1, NAT and DNAT are on workshops' labN and VMs on other hosts reach .1 over VXLAN. This must be verified on the real two-host setup.

## Non-goals

- **Moving existing students between hosts.** That includes no rebalance command and no live migration.
- **Removing a host from the cluster** (`node remove`). For now that is documented as manual `pvecm delnode` steps in the README.
- **Setting up a QDevice.** It is only mentioned, with its command, in the README and in `node add`'s closing note.
- **Changing OpenStack security groups.** pvelab has no OpenStack access. The checks name the ports to open.
- **Spreading the old bridge nets (vmbrN) across hosts.**
  - That covers net30 and net40, and any net made with `LAB_SDN=0` or where SDN is missing.
  - A vmbrN net exists on one host only. All its VMs stay on the host that has the bridge: `--node auto` picks only that host, and `--node OTHER` is refused with the reason.
- **High availability, or shared storage** such as Ceph.
- **Running pvelab from a host other than workshops.** It should not break, but it is not tested.

## Anti-goals (failures even if it "works")

- **Touching the running classes on workshops** (net30, net40, 45 VMs) during development, testing or `node add`.
  - The only change workshops' guests may see is the short pve-cluster restart from `pvecm create`.
  - That step needs the instructor's explicit yes in the run itself.
- **`node add` leaving a half-joined host without saying so.** On failure it must say:
  - which step failed
  - what state each host is in
  - the exact commands to recover, or that it is safe to rerun `node add`.
- **Storing any password**, or passing one on a command line where it shows up in `ps`.
- **A student's two VMs ending up on different hosts.**
- **Isolation that leaks across hosts:** student A on host 1 reaching student B on host 2 on an isolated net.
- **Any existing single-host behaviour changing** when there is only one host, or when workshops is not in a cluster. The `--node` question and the host column stay hidden then; extra columns may show.

## Constraints and facts found

- **workshops:**
  - PVE 9.2.20, qemu-server 9.2.8, IP 10.0.0.123/24 on vmbr0 with MTU 8942, storage local-lvm.
  - root has a password, and sshd allows root password login.
- **New host:** reachable on TCP 22 and 8006, but not on ICMP. pvelab cannot log in there yet.
- **PVE behaviour:**
  - `pvecm add` must run on the joining host as root. It asks for the existing node's root password, and asks you to accept its fingerprint.
  - After a join, the nodes' root accounts can ssh to each other, through PVE's shared `/etc/pve/priv/authorized_keys`.
  - `qm` acts only on VMs of the local node.
    - pvelab runs `qm` in about 38 places: lifecycle, guest, snapshots, cli, templates, power, inventory and resize.
    - For a VM on another node, pvelab needs either `pvesh` against `/nodes/<node>/qemu/...` (the cluster-wide API) or `ssh root@<node> qm ...`. The builder picks.
    - One wrapper, such as `qm_on(vmid, ...)`, that finds the VM's node from `/etc/pve/nodes/<node>/qemu-server/<vmid>.conf`, is the expected shape.
  - A template on local-lvm cannot be cloned straight to another node, because `qm clone --target` needs shared storage. Some ways to copy it, for the builder to pick:
    - full clone to a temporary VMID, offline `qm migrate` with local disks, then `qm template` on the target
    - or vzdump piped over ssh into `qmrestore`.
- **Today's per-host state:**
  - iptables and nft rules, written by pvelab into workshops' `/etc/network/interfaces`
  - `/root/lab` (credentials, blocks.json, nft files, run logs)
  - the snippet in `/var/lib/vz/snippets`.

  pvelab keeps its state on workshops. Only rules that must be enforced on another host go there.
- **The thin pool is often nearly full.** A template copy must go through the existing space check (`check_copy_space`) on the target host.
- **pvelab rules still apply:**
  - pvelab is installed per user. Never `sudo pvelab`, never install to `/usr/local/bin`.
  - Tests run only on the self-test nets (91–97).

## Testing

- Unit tests for:
  - placement, by student and by room, honoring `--node`
  - the vmbrN restriction
  - the node-routing wrapper
  - parsing the `node add` checks
  - the template copy plan
- `tests/selftest.sh` keeps passing on a single host, in both modes.
- **Two-host tests:** a new self-test section that runs only when the cluster has 2 or more nodes. It covers:
  - create a class spread over both hosts on a self-test net
  - check the placement rule
  - cross-host isolation: a ping between students fails, and with `--no-isolate` it works
  - internet, expose, block and run on a VM on the second host
  - destroy, and that no state is left on either host.
- **The real join of 10.0.0.229** happens once the code is ready, with the instructor present.
  - The instructor types the passwords.
  - It needs the instructor's yes for `pvecm create` on workshops.

## Left to the builder

- `pvesh` or `ssh … qm` for each operation.
- Where each network rule lives, provided goal 9 holds and is tested on two hosts.
- How a template is copied.
- Names: `node` versus `nodes`, the wrapper, and the module layout.
- Wording of the wizard and help text.
- How a `node add` check reaches UDP ports, for example a short-lived `socat` or `nc` listener over ssh.

## Decisions after review (builder's calls, made by the designer)

1. **Template copies get new VMIDs.**
   - In a cluster, a VMID belongs to one guest only, so a copy cannot reuse the original's VMID.
   - Copies get the next free VMID at or above `SAVED_ID_START`.
   - The copy's description records `copy of <original VMID>`, plus the name and the build or save stamp.
   - A template is identified by its **name**. A catalogue alias or the original VMID still selects it.
   - `create` picks, on the chosen host, the copy whose source matches the requested template.
   - **Updates:** `pull` updates the original on workshops.
     - A copy whose source stamp is older is replaced the next time that host needs the template.
     - An old copy that linked clones still use is left in place, as `pull` does today.
   - **Deleting:** `delete-template` deletes the original and every copy that no linked clone uses. It lists any copy it had to keep.
2. **`save-template` of a VM on another host** puts the new template on that VM's host, and `check_copy_space` checks that host's pool. Other hosts get it by on-demand copy, like any template.
3. **Storage.** Every host must have the pvelab storage (`local-lvm`, LVM-thin `pve/data`), because pvelab uses one storage name for the whole cluster.
   - `node add` step 1 checks this read-only and refuses, with the reason, if the storage is missing.
   - Storage set per host is out of scope.
4. **SDN apply on workshops is acceptable without another question.**
   - Every SDN class `create` already runs it, and it has been run on workshops many times, in self-tests and with net30 and net40 up.
   - pvelab's rules are add-once, so a reload does not duplicate them, and guests do not notice it.
   - `node add` says that it applies the network on all hosts.
5. **VMs made without `--net`** (DHCP bridge vmbr1 with dnsmasq) stay on workshops, like the other vmbrN nets.
