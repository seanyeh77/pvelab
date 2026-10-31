"""Help text: the overview, per-command usage, and bad_usage()."""

from .catalogue import TEMPLATES
from .config import (AGENT_WAIT, CORES, CPU_HIGH, INTERNAL_NETS, LOGIN_IP_HIGH, LOGIN_RATE_HIGH, MEM, RUN_LOG_DIR,
                     RUN_TIMEOUT_MIN, STATUS_EVERY, UPGRADE_URL)
from .util import LabError

USAGE = """pvelab - student VMs on this Proxmox VE node

usage: pvelab <command> [args]          details and examples: pvelab help <command>
       pvelab                            on a terminal: a menu that asks step by step

VMs
  create STUDENTS[xVMS] [TEMPLATE] [--net N|auto] [--expose] [--no-isolate]
         [--script SCRIPTS] [--cores N] [--memory MB] [--disk GB] [--node auto|NAME]
  list [--ssh] [--net N] [--passwords]   status, CPU, sizes, IPs, ssh ports
  start | stop | reboot VM...
  destroy VM... [--dry-run]              asks before deleting
  resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot] [--dry-run]
  ssh NAME [COMMAND]                     ssh into a VM from this host

Snapshots and scripts
  rollback VM... [--to NAME | --initial] [--dry-run]   undo the students' changes
  snapshot VM...                         current state becomes "clean"
  run VM... SCRIPT[+SCRIPT...] [--timeout MIN]   run scripts as root in the VMs
  scripts [NAME]                         list the script library, or print one

Access
  expose --net N [--host ADDR] [--off]   students ssh in from outside (port 22000+VMID)
  block | unblock internet|ssh|all VM... cut internet or ssh; kept across reboots
  isolate --net N [--off]                students can't reach each other's VMs (the default)
  handout VM... [--output FILE]          printable login cards (ssh command + password)

Watching
  top [VM...] [--watch[=SEC]] [--check] CPU / memory / network; flags busy VMs
  status [--net N] [--since 1h] [-f]     hosts and cluster links, who is logged in, login attempts

Hosts
  node list                              each PVE host: online, free memory and disk, VMs, templates
  node add ADDR                          check a PVE host, then join it to the cluster (asks first)
  node upgrade [HOST...] [--major] [--dry-run]   update PVE on the hosts, one at a time (reboots)
  node rename OLD NEW [--dry-run]        rename a PVE host (asks first; the host reboots)

Templates and pvelab itself
  templates                              available OS templates
  pull TEMPLATE... | all                 build templates, or update them to the newest image
  save-template VM NAME                  a new template from a VM as it is (Docker, images, ...)
  delete-template NAME                   delete a template made with save-template
  upgrade                                download the latest pvelab
  version                                this version, and other installed copies
  completion                             bash tab completion (install.sh sets it up)

VM: a name (net40-student01), --net N (the whole net), a range (net40-student01..05),
    'net40-student1*', or all.   Options take --x V or --x=V.
Runs as root; other users are asked before it uses sudo.

example:
  pvelab create 20 ubuntu26 --net auto --expose --script harden+setup.sh
  pvelab list --ssh
"""

VM_ARGS = """  VM          one or more of: a name (net30-student01), --net N (every VM of net N),
              a range (net30-student01..05), a pattern in quotes ('net30-student1*'), or all"""


def command_help(cmd):
    """Readable help for one command: usage, what each argument means, examples."""
    tpls = ", ".join(t.short for t in TEMPLATES)
    h = {
        "create": f"""usage: pvelab create STUDENTS[xVMS] [TEMPLATE] [--net N|auto] [--expose] [--no-isolate]
                     [--host ADDR] [--script SCRIPTS] [--cores N] [--memory MB] [--disk GB]
                     [--node auto|NAME]

  STUDENTS    how many students (1-99 VMs in all); names that already exist are skipped
  xVMS        several VMs per student, e.g. 20x3: net40-student01-1 .. -3, ...; one password
              per student; needs --net, and a net keeps its number of VMs per student
  TEMPLATE    {tpls}, or a VMID (default: debian13, see: pvelab templates)
              (also as --template NAME / -t NAME)
  --net N     isolated network 10.10.N.0/24, N = 2-254 except 10: a PVE SDN vnet labN (the same
              network on every node of the cluster), or a bridge vmbrN where SDN is missing
              (LAB_SDN=0: always); a net pvelab made before is reused (more VMs for that
              class); stops if its bridge or the subnet is used by anything that is not pvelab
  --net auto  take the lowest completely free N (printed)
              without --net the VMs go on vmbr1 (DHCP)
  --expose    also let students ssh in from outside (needs --net)
  --no-isolate  a new net normally keeps students apart (each reaches only their own VMs);
              with this, all its VMs reach each other (group work). Later: pvelab isolate
  --host ADDR address shown to students (default: this host's public IP)
  --script SCRIPTS  scripts run as root in every VM once it has booted: library names
              (see: pvelab scripts) or files; + runs several in order (harden+setup.sh).
              A comma gives one chain for each of a student's VMs: harden+docker, runs
              harden+docker on every -1 and nothing on -2. Afterwards a snapshot named after
              the chain is taken (harden+setup.sh -> "harden-setup")
  --cores N   CPU cores per VM (default {CORES})
  --memory MB memory per VM, in MB or like 4G (default {MEM} MB)
  --disk GB   disk size per VM (default: the template's, 10 GB); can only be bigger
              With 20x3, TEMPLATE / --cores / --memory / --disk can also be a list, one
              value for each of a student's VMs: --memory 2G,8G,8G gives every -1 2 GB and
              every -2 and -3 8 GB (an empty --disk entry keeps the template's)
  --node auto|NAME  which PVE host gets the new students (with 2 or more hosts; see: pvelab node).
              A student's VMs are always on one host. auto (the default): each new student goes
              to the host with the most free memory and disk, so a class spreads over the hosts;
              NAME: all on that host. Students who already have a VM stay on its host. A template
              the host lacks is copied there first (asks, with its size). Nets that are a bridge
              (vmbrN, LAB_SDN=0) and VMs without --net (vmbr1, DHCP) stay on this host

  Each VM gets snapshots "initial" (before first boot) and "clean" (after it), and starts
  when the host boots. Templates restrict nothing; --script harden makes passwords need 10+
  characters (recommended with --expose). Mining-pool ports are always blocked.
  Settings: LAB_MEM (MB, 2048) LAB_CORES (2) LAB_STORAGE (local-lvm);
  LAB_YES=1 skips questions, LAB_FORCE=1 ignores low memory/disk warnings.

examples:
  pvelab create 20 --net 40                     20 debian13 VMs on net40
  pvelab create 20 ubuntu26 --net 40 --expose --script harden+setup.sh
  pvelab create 20x2 --net 40 --script harden+docker,
                                                harden+docker on every -1, nothing on -2
  pvelab create 20 ubuntu26 --net 40 --expose   20 ubuntu26 VMs, reachable by ssh from outside
  pvelab create 20 --net auto                   20 VMs on the next free net
  pvelab create 25 --net 40                     net40 has 20: adds student21-25
  pvelab create 20x3 ubuntu26 --net 40 --expose 20 students with 3 VMs each
  pvelab create 10 --net 41 --no-isolate        a class that works in groups
  pvelab create 20 --net 40 --cores 4 --memory 4G --disk 30
  pvelab create 20x2 ubuntu26,debian13 --net 40 --cores 4,1 --memory 8G,2G --disk 40,
                                                -1: ubuntu26 4 cores 8 GB 40 GB, -2: small debian13
  pvelab create 3                               3 debian13 VMs on vmbr1 (DHCP)
  pvelab create 10 --net 42 --node pve2         all 10 on host pve2""",
        "list": """usage: pvelab list [--ssh] [--net N] [--passwords]

  --ssh       only "name  ssh command  password" for VMs students can reach
  --net N     only the VMs of net N (--net dhcp: those on vmbr1)
  --passwords add the PASSWORD column (hidden by default: safe to show on a projector)
  CPU is the VM's current use of its cores; 90%+ is shown in red (pvelab top for more)

examples:
  pvelab list
  pvelab list --ssh --net 40                    lines to hand out to the class of net40
  pvelab list --passwords --net 40""",
        "power": f"""usage: pvelab start|stop|reboot VM...

{VM_ARGS}
  all VMs at once, one line each; start and reboot wait until the VM has booted (its guest
  agent answers, up to {AGENT_WAIT // 60} min)

examples:
  pvelab start --net 40
  pvelab stop net40-student05 net40-student06
  pvelab reboot 'net40-student1*'""",
        "destroy": f"""usage: pvelab destroy VM... [--dry-run]

{VM_ARGS}
  --dry-run   only show what would be destroyed
  asks for "yes" first; a net's bridge and ssh ports go away with its last VM
  only VMs made by pvelab (tag lab-student) are ever touched - never templates or your own VMs

examples:
  pvelab destroy net40-student21
  pvelab destroy --net 40                       the whole class""",
        "rollback": f"""usage: pvelab rollback VM... [--to NAME | --initial] [--dry-run]

{VM_ARGS}
  --dry-run   only show which VMs would go back to which snapshot
  --to NAME   go back to that snapshot (e.g. "setup", taken after setup.sh) instead of "clean"
  --initial   go back to before the first boot
  name, IP, ssh port and password stay the same

examples:
  pvelab rollback net40-student05
  pvelab rollback --net 40 --to setup
  pvelab rollback --net 40 --initial""",
        "run": f"""usage: pvelab run VM... SCRIPT[+SCRIPT...] [--timeout MIN]

{VM_ARGS}
  SCRIPT      a library script (see: pvelab scripts) or a file (must start with #!, max 1 MiB);
              + runs several in order (harden+docker); a failed one stops the rest on that VM.
              Runs as root in /root, all VMs at once
  --timeout   minutes to wait for each script (default {RUN_TIMEOUT_MIN})
  output goes to {RUN_LOG_DIR}/<time>-<name>/<VM>/<step>-<script>.log
  one snapshot pair per run, named after the chain (an older one with the same name is replaced):
    "before-harden-docker"  every VM, just before the first script starts
    "harden-docker"         VMs where every script exited 0

examples:
  pvelab run --net 40 harden+docker
  pvelab run --net 40 setup.sh
  pvelab run net40-student05 fix-docker.sh
  pvelab rollback --net 40 --to setup           later: back to right after setup.sh
  pvelab rollback --net 40 --to before-setup    undo setup.sh""",
        "snapshot": f"""usage: pvelab snapshot VM...

{VM_ARGS}
  the current state becomes the new "clean" that rollback returns to

examples:
  pvelab snapshot --net 40                      after installing the course software""",
        "expose": """usage: pvelab expose --net N [--host ADDR]
       pvelab expose --net N --off

  --net N     the net number (40 for net40); --net=40 works too
  --host ADDR address shown to students (saved; default: this host's public IP)
  --off       close it again
  students connect with: ssh -p 22000+VMID student@ADDR

examples:
  pvelab expose --net 40
  pvelab expose --net 40 --off""",
        "block": f"""usage: pvelab block internet|ssh|all VM...
       pvelab unblock internet|ssh|all VM...

  internet    the VMs can't reach the internet; ssh from outside keeps working
  ssh         nobody can ssh in from outside (pvelab ssh from this host still works);
              sessions that are open get cut
  all         both
{VM_ARGS}
  only VMs made with --net N; stays after reboot and rollback; see "pvelab list" (BLOCKED)
  always blocked on every --net N: connections to mining-pool ports (3333, 4444, ...)

examples:
  pvelab block internet --net 40                exam: whole class offline
  pvelab block ssh net40-student07
  pvelab unblock all --net 40""",
        "resize": f"""usage: pvelab resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot] [--dry-run]

{VM_ARGS}
  --cores N   CPU cores
  --memory MB memory, in MB or like 4G
  --disk GB   new disk size (30) or how much to add (+10); disks can only grow
  --reboot    reboot the running VMs right away so the change takes effect
  --dry-run   only show the changes, and how much memory the host has for them
  without --reboot, running VMs get the new size at their next reboot; the filesystem
  grows into a bigger disk on that reboot as well

examples:
  pvelab resize --net 40 --cores 4 --memory 8G --reboot
  pvelab resize net40-student05 --disk +20
  pvelab resize net40-student01..05 --memory 4G --dry-run""",
        "isolate": """usage: pvelab isolate --net N [--off]

  on          students of net N can't reach each other's VMs; each student's own VMs
              (net40-student01-1, -2, ...) still reach each other, the gateway and the internet.
              New nets start like this. Changing a VM's IP or MAC doesn't get around it.
  --off       all VMs of net N reach each other (group work)
  pvelab list shows which nets are isolated

examples:
  pvelab isolate --net 40 --off                 group project this week
  pvelab isolate --net 40                       back to everyone on their own""",
        "top": f"""usage: pvelab top [VM...] [--watch[=SECONDS]] [--check]

  VM          which VMs (default: all student VMs); as for the other commands
  --watch     refresh every 5 s (--watch=10: every 10 s) until Ctrl-C
  --check     for cron: VMs whose CPU averaged {round(CPU_HIGH * 100)}%+ over the last hour; exit code 1 if any
  CPU {round(CPU_HIGH * 100)}%+ is flagged: fine while students compile, suspicious for hours with nobody
  logged in (cryptominers)

examples:
  pvelab top --net 30
  pvelab top --watch
  0 * * * * root pvelab top --check >/tmp/pvelab-check || mail -s "busy VMs" you@example.org </tmp/pvelab-check""",
        "status": f"""usage: pvelab status [--net N] [--since 1h] [-f]

  hosts       each host online or offline, its corosync link to each other host and the round
              trip to it; the cluster: quorate, votes, QDevice; the VXLAN zone's peers
  students    each VM someone is logged in to: who, from where, for how long, its ssh sessions,
              whether it reaches the internet; VMs with nobody logged in are one line; stopped
              VMs and VMs whose guest agent does not answer are counted
  logins      failed logins in the window, per host (ssh and the web UI on 8006) and per
              exposed VM (ssh, from its journal): top source IPs, the user names tried.
              Inside ({', '.join(INTERNAL_NETS)}, the hosts) is listed apart from outside.
              A student's own IP (logged in as the student in the last 7 days) is counted
              as "wrong password (student)", never as an attack.
              Highlighted: an outside IP with {LOGIN_IP_HIGH}+ failures; red: a host or VM with {LOGIN_RATE_HIGH}+
              failures per hour, a login after {LOGIN_IP_HIGH}+ failures from an IP not known before
              (maybe a guessed password), and a VM login from outside as root or another user
  --net N     only the VMs of net N (--net dhcp: those on vmbr1); the hosts are always shown
  --since T   the window for login attempts: 30m, 1h (the default), 2d
  -f          keep the screen open and refresh it every {STATUS_EVERY} s, like top, until Ctrl-C
  only looks: it changes nothing, takes no lock, and runs during a create. Passwords are never shown.

examples:
  pvelab status
  pvelab status --net 40 -f                     during class
  pvelab status --since 1d                      login attempts of the last day""",
        "handout": """usage: pvelab handout VM... [--output FILE]

  one card per student with the ssh command of each of their VMs and their password,
  as an HTML page to print
  --output FILE, -o FILE  file to write (default: pvelab-handout-netN.html here)
  the file holds the passwords: it is readable only by you; delete it after printing

examples:
  pvelab handout --net 40
  pvelab handout net40-student01..05 --output make-up-session.html""",
        "version": """usage: pvelab version

  this pvelab's version and file, and other pvelab copies on this host that differ
  ('pvelab' and 'sudo pvelab' can be different files)""",
        "completion": """usage: pvelab completion

  prints the bash completion script (commands, options, VM names, nets, snapshots);
  install.sh installs it. By hand:
  pvelab completion | sudo tee /etc/bash_completion.d/pvelab""",
        "upgrade": f"""usage: pvelab upgrade

  downloads pvelab from {UPGRADE_URL} (set PVELAB_URL to use another address)
  and replaces this copy; asks for sudo only if the file isn't yours""",
        "scripts": """usage: pvelab scripts [NAME]

  the script library that comes with pvelab: each script's name and what it does;
  with NAME, prints that script. Needs no root; pvelab upgrade brings new scripts.
  use them with: pvelab create ... --script harden+docker   or   pvelab run VM... harden+docker

examples:
  pvelab scripts
  pvelab scripts harden""",
        "ssh": """usage: pvelab ssh NAME [COMMAND...]

examples:
  pvelab ssh net40-student01
  pvelab ssh net40-student01 df -h""",
        "templates": """usage: pvelab templates

  the OS templates, whether they are built and when""",
        "pull": f"""usage: pvelab pull TEMPLATE... | all

  TEMPLATE    {tpls}

  builds the template from the distribution's newest cloud image; when it is built already, it
  is rebuilt only if a newer image is out (it checks the published checksum). VMs made from the
  old template keep working and are not changed; new VMs get the new one. On ZFS, Ceph or qcow2
  storage, VMs cloned from a template still need it: it is then left as it is until they are gone.

examples:
  pvelab pull ubuntu24       build it, or update it
  pvelab pull all            every template up to date""",
        "save-template": """usage: pvelab save-template VM NAME

  VM          a student VM, e.g. one where you installed and set up what a class needs
  NAME        the new template's name: letters, digits and - (like docker-lab)

  copies the VM's disks as they are now into a new template (VMID 9100 and up); the VM itself
  is not changed. The copy is booted once without network to forget who it was (machine id, ssh
  host keys, cloud-init, shell history), so each VM made from it gets its own name, password and
  IP. Everything else is kept - installed software, Docker images, and files in /home.
  On LVM-thin, the VMs made from it share the template's data: what you put in the template is
  stored once, not once per student.

examples:
  pvelab save-template net30-student01 docker-lab
  pvelab create 35 docker-lab --net 31 --expose""",
        "node": """usage: pvelab node [list]
       pvelab node add ADDR          (pvelab nodes works too)
       pvelab node upgrade [HOST...] [--major] [--dry-run]
       pvelab node rename OLD NEW [--dry-run]

  list        each host of the cluster: online or offline, free memory, free disk, pvelab VMs, templates
  add ADDR    add the PVE host at IPv4 address ADDR. It asks for the ssh user there (a sudoer, or
              root); passwords are typed only into the prompts of ssh, sudo and pvecm.
              1. checks the host, read-only: unique hostname that resolves to ADDR, same PVE
                 version, no guests, LVM-thin storage local-lvm, /dev/kvm, TCP 22 and 8006 and
                 UDP 4789 and 5405-5412 both ways, clocks in sync. Any problem: nothing is changed.
              2. creates cluster 'pvelab' here if there is none (asks for a typed yes)
              3. joins the host (pvecm add on it; it asks for this host's root password)
              4. waits until it is online and the cluster is quorate
              5. prepares it: vendor-data snippet, isolation tables, SDN peers (applied on all hosts)
              Safe to run again: a host already in the cluster skips to steps 4-5.
  upgrade     update PVE on every host (or the HOSTs named), one host at a time, this host last.
              1. checks, read-only: every host online, the cluster quorate, free space on / and
                 /boot, apt-get update works, what would be upgraded (kernel, pve-manager).
                 With --major: PVE's checker (pve9to10 --full) has no FAILURES, and the new PVE
                 and Debian releases are published; if there is no new major version, it says so.
              2. shows the plan and every warning, and asks once (LAB_YES=1: no question)
              3. for each host: who is logged in to its students' VMs (asks if anyone is, default
                 no); with --major, the apt sources move to the new Debian release (the old files
                 are kept as *.pvelab-bak); apt-get update and dist-upgrade (non-interactive, old
                 config files kept; in its own systemd unit, pvelab-upgrade, so it goes on even if
                 pvelab stops; its output: /var/log/pvelab-upgrade.log); a reboot after a new kernel
                 or a major upgrade (else only if /run/reboot-required); then it waits until the
                 host is back with the new version.
              When this host reboots, pvelab ends with it: it says first how to check (pvelab node
              upgrade). Hosts already current are skipped, so a rerun goes on where it stopped.
              On a failure it stops there: which host and step, each host's state, how to recover.
              --dry-run: only the checks and the plan. Log: /root/lab/logs/node-upgrade-*.log
  rename OLD NEW
              rename host OLD to NEW (a valid host name, not used in the cluster; the domain in
              /etc/hosts is kept). First it checks, then shows the plan: the steps, the reboots, the
              VMs that restart, the students logged in (guest agent's who), the backups; one typed yes.
              Before any change it backs up each host (config.db, /etc/corosync, corosync.conf,
              hostname, hosts, /etc/pve/nodes/OLD) to /root/lab/rename-backup-<time>/<host>/.
              A host with no guests but template copies (not this one): the copies are deleted, it
              leaves the cluster (pvecm delnode, then PVE's documented separation), gets the new name,
              reboots and joins again as node add does (asks for this host's root password).
              A host with VMs, or this host: renamed in place - hostname and hosts, corosync.conf,
              its VMs shut down and their configs moved to /etc/pve/nodes/NEW, reboot; then pvecm
              updatecerts --force, the checks, and the VMs that ran are started again.
              When this host reboots, the next pvelab command (or pvelab node rename alone) finishes
              the rename.
              A failure says each host's state and how to restore from the backup.
              Renaming both hosts: the other host first (pve9 -> workshop2), then this one
              (workshops -> workshop1).

  with two hosts, when one is down the other has no quorum; on the host that is up:
  pvecm expected 1
  a third vote (a QDevice: corosync-qnetd on another machine) avoids this: pvecm qdevice setup IP
  removing a host is manual (see the README): pvecm delnode NAME

examples:
  pvelab node list
  pvelab node add 10.0.0.229
  pvelab node upgrade --dry-run
  pvelab node upgrade --major
  pvelab node rename pve9 workshop2 --dry-run
  pvelab node rename pve9 workshop2""",
        "delete-template": """usage: pvelab delete-template NAME

  deletes a template made with save-template; VMs made from it keep working (on LVM-thin).
  Catalogue templates (pvelab pull) are not deleted this way.""",
    }
    return h.get({"start": "power", "stop": "power", "reboot": "power", "ls": "list",
                  "unblock": "block", "nodes": "node"}.get(cmd, cmd))




class UsageError(LabError):
    """Wrong arguments; pvelab prints the command's help after it."""


def bad_usage(problem):
    raise UsageError(problem)
