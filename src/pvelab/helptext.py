"""Help text: the overview, per-command usage, and bad_usage()."""

from .catalogue import TEMPLATES
from .config import CORES, CPU_HIGH, MEM, RUN_LOG_DIR, RUN_TIMEOUT_MIN, UPGRADE_URL
from .util import LabError

USAGE = """pvelab - student VMs on this Proxmox VE node

usage: pvelab <command> [args]          details and examples: pvelab help <command>
       pvelab                            on a terminal: a menu that asks step by step

VMs
  create STUDENTS[xVMS] [TEMPLATE] [--net N|auto] [--expose] [--no-isolate]
         [--script FILE] [--cores N] [--memory MB] [--disk GB]
  list [--ssh] [--net N] [--passwords]   status, CPU, sizes, IPs, ssh ports
  start | stop | reboot VM...
  destroy VM... [--dry-run]              asks before deleting
  resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot] [--dry-run]
  ssh NAME [COMMAND]                     ssh into a VM from this host

Snapshots and scripts
  rollback VM... [--to NAME | --initial] [--dry-run]   undo the students' changes
  snapshot VM...                         current state becomes "clean"
  run VM... FILE [--timeout MIN]         run a script as root in the VMs

Access
  expose --net N [--host ADDR] [--off]   students ssh in from outside (port 22000+VMID)
  block | unblock internet|ssh|all VM... cut internet or ssh; kept across reboots
  isolate --net N [--off]                students can't reach each other's VMs (the default)
  handout VM... [--output FILE]          printable login cards (ssh command + password)

Watching
  top [VM...] [--watch[=SEC]] [--check] CPU / memory / network; flags busy VMs

Templates and pvelab itself
  templates                              available OS templates
  pull TEMPLATE... | all                 build templates, or update them to the newest image
  upgrade                                download the latest pvelab
  version                                this version, and other installed copies
  completion                             bash tab completion (install.sh sets it up)

VM: a name (net40-student01), --net N (the whole net), a range (net40-student01..05),
    'net40-student1*', or all.   Options take --x V or --x=V.
Runs as root; other users are asked before it uses sudo.

example:
  pvelab create 20 ubuntu26 --net auto --expose --script setup.sh
  pvelab list --ssh
"""

VM_ARGS = """  VM          one or more of: a name (net30-student01), --net N (every VM of net N),
              a range (net30-student01..05), a pattern in quotes ('net30-student1*'), or all"""


def command_help(cmd):
    """Readable help for one command: usage, what each argument means, examples."""
    tpls = ", ".join(t.short for t in TEMPLATES)
    h = {
        "create": f"""usage: pvelab create STUDENTS[xVMS] [TEMPLATE] [--net N|auto] [--expose] [--no-isolate]
                     [--host ADDR] [--script FILE] [--cores N] [--memory MB] [--disk GB]

  STUDENTS    how many students (1-99 VMs in all); names that already exist are skipped
  xVMS        several VMs per student, e.g. 20x3: net40-student01-1 .. -3, ...; one password
              per student; needs --net, and a net keeps its number of VMs per student
  TEMPLATE    {tpls}, or a VMID (default: debian13, see: pvelab templates)
              (also as --template NAME / -t NAME)
  --net N     isolated network vmbrN / 10.10.N.0/24, N = 2-254 except 10; a net pvelab made
              before is reused (more VMs for that class); stops if vmbrN or the subnet is
              used by anything that is not pvelab
  --net auto  take the lowest completely free N (printed)
              without --net the VMs go on vmbr1 (DHCP)
  --expose    also let students ssh in from outside (needs --net)
  --no-isolate  a new net normally keeps students apart (each reaches only their own VMs);
              with this, all its VMs reach each other (group work). Later: pvelab isolate
  --host ADDR address shown to students (default: this host's public IP)
  --script FILE  instructor's script, run as root in every VM once it has booted; afterwards
              a snapshot named after the file is taken (setup.sh -> "setup")
  --cores N   CPU cores per VM (default {CORES})
  --memory MB memory per VM, in MB or like 4G (default {MEM} MB)
  --disk GB   disk size per VM (default: the template's, 10 GB); can only be bigger

  Each VM gets snapshots "initial" (before first boot) and "clean" (after it), and starts
  when the host boots. Against botnets: only "student" may ssh in (accounts made in class
  work locally: su - bob), passwords need 10+ characters, mining-pool ports are blocked.
  Settings: LAB_MEM (MB, 2048) LAB_CORES (2) LAB_STORAGE (local-lvm);
  LAB_YES=1 skips questions, LAB_FORCE=1 ignores low memory/disk warnings.

examples:
  pvelab create 20 --net 40                     20 debian13 VMs on net40
  pvelab create 20 ubuntu26 --net 40 --expose --script setup.sh
  pvelab create 20 ubuntu26 --net 40 --expose   20 ubuntu26 VMs, reachable by ssh from outside
  pvelab create 20 --net auto                   20 VMs on the next free net
  pvelab create 25 --net 40                     net40 has 20: adds student21-25
  pvelab create 20x3 ubuntu26 --net 40 --expose 20 students with 3 VMs each
  pvelab create 10 --net 41 --no-isolate        a class that works in groups
  pvelab create 20 --net 40 --cores 4 --memory 4G --disk 30
  pvelab create 3                               3 debian13 VMs on vmbr1 (DHCP)""",
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
        "run": f"""usage: pvelab run VM... FILE [--timeout MIN]

{VM_ARGS}
  FILE        the script (must start with #!, max 1 MiB); runs as root in /root, all VMs at once
  --timeout   minutes to wait (default {RUN_TIMEOUT_MIN})
  output goes to {RUN_LOG_DIR}/<time>-<name>/<VM>.log
  snapshots (an older one with the same name is replaced):
    "before-setup"  every VM, just before setup.sh starts
    "setup"         VMs where setup.sh exited 0

examples:
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
    }
    return h.get({"start": "power", "stop": "power", "reboot": "power", "ls": "list",
                  "unblock": "block"}.get(cmd, cmd))


CMD = ""                                      # the command being run, for bad_usage()


def bad_usage(problem):
    """Wrong arguments: say what is wrong, then the command's help."""
    raise LabError(f"{problem}\n\n{command_help(CMD) or ''}".rstrip())
