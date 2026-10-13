"""Help text: the overview, per-command usage, and bad_usage()."""

from .catalogue import TEMPLATES
from .config import CORES, MEM, RUN_LOG_DIR, RUN_TIMEOUT_MIN, UPGRADE_URL
from .util import LabError

USAGE = """pvelab - student VMs on this Proxmox VE node

usage: pvelab <command> [args]          details and examples: pvelab help <command>

VMs
  create COUNT [TEMPLATE] [--net N|auto] [--expose] [--script FILE]
         [--cores N] [--memory MB] [--disk GB]
  list [--ssh] [--net N]                 names, sizes, IPs, ssh ports, passwords
  start | stop | reboot VM...
  destroy VM...                          asks before deleting
  resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot]
  ssh NAME [COMMAND]                     ssh into a VM from this host

Snapshots and scripts
  rollback VM... [--to NAME | --initial] undo the students' changes
  snapshot VM...                         current state becomes "clean"
  run VM... FILE                         run a script as root in the VMs

Access
  expose N [--off]                       students ssh in from outside (port 22000+VMID)
  block | unblock internet|ssh|all VM... cut internet or ssh; kept across reboots

Templates and pvelab itself
  templates                              available OS templates
  pull TEMPLATE... | all                 build templates ahead of time
  upgrade                                download the latest pvelab

VM: a name (net40-student01), netN (the whole net), 'net40-student1*', or all
Runs as root; other users are asked before it uses sudo.

example:
  pvelab create 20 ubuntu26 --net auto --expose --script setup.sh
  pvelab list --ssh
"""

VM_ARGS = """  VM          one or more of: a name (net30-student01), netN (every VM of net N),
              a pattern in quotes ('net30-student1*'), or all"""


def command_help(cmd):
    """Readable help for one command: usage, what each argument means, examples."""
    tpls = ", ".join(t.aliases[0] if t.aliases else t.name for t in TEMPLATES)
    h = {
        "create": f"""usage: pvelab create COUNT [TEMPLATE] [--net N|auto] [--expose] [--host ADDR] [--script FILE]
                     [--cores N] [--memory MB] [--disk GB]

  COUNT       how many VMs to have (1-99); names that already exist are skipped
  TEMPLATE    {tpls}, or a VMID (default: debian13, see: pvelab templates)
  --net N     isolated network vmbrN / 10.10.N.0/24, N = 2-254 except 10; a net pvelab made
              before is reused (more VMs for that class); stops if vmbrN or the subnet is
              used by anything that is not pvelab
  --net auto  take the lowest completely free N (printed)
              without --net the VMs go on vmbr1 (DHCP)
  --expose    also let students ssh in from outside (needs --net)
  --host ADDR address shown to students (default: this host's public IP)
  --script FILE  instructor's script, run as root in every VM once it has booted; afterwards
              a snapshot named after the file is taken (setup.sh -> "setup")
  --cores N   CPU cores per VM (default {CORES})
  --memory MB memory per VM, in MB or like 4G (default {MEM} MB)
  --disk GB   disk size per VM (default: the template's, 10 GB); can only be bigger

  Each VM gets snapshots "initial" (before first boot) and "clean" (after it), and starts
  when the host boots. Settings: LAB_MEM (MB, 2048) LAB_CORES (2) LAB_STORAGE (local-lvm);
  LAB_YES=1 skips questions, LAB_FORCE=1 ignores low memory/disk warnings.

examples:
  pvelab create 20 --net 40                     20 debian13 VMs on net40
  pvelab create 20 ubuntu26 --net 40 --expose --script setup.sh
  pvelab create 20 ubuntu26 --net 40 --expose   20 ubuntu26 VMs, reachable by ssh from outside
  pvelab create 20 --net auto                   20 VMs on the next free net
  pvelab create 25 --net 40                     net40 has 20: adds student21-25
  pvelab create 20 --net 40 --cores 4 --memory 4G --disk 30
  pvelab create 3                               3 debian13 VMs on vmbr1 (DHCP)""",
        "list": """usage: pvelab list [--ssh] [--net N]

  --ssh       only "name  ssh command  password" for VMs students can reach
  --net N     only the VMs of net N

examples:
  pvelab list
  pvelab list --ssh --net 40                    lines to hand out to the class of net40""",
        "power": f"""usage: pvelab start|stop|reboot VM...

{VM_ARGS}

examples:
  pvelab start net40
  pvelab stop net40-student05 net40-student06
  pvelab reboot 'net40-student1*'""",
        "destroy": f"""usage: pvelab destroy VM...

{VM_ARGS}
  asks for "yes" first; a net's bridge and ssh ports go away with its last VM
  only VMs made by pvelab (tag lab-student) are ever touched - never templates or your own VMs

examples:
  pvelab destroy net40-student21
  pvelab destroy net40                          the whole class""",
        "rollback": f"""usage: pvelab rollback VM... [--to NAME | --initial]

{VM_ARGS}
  --to NAME   go back to that snapshot (e.g. "setup", taken after setup.sh) instead of "clean"
  --initial   go back to before the first boot
  name, IP, ssh port and password stay the same

examples:
  pvelab rollback net40-student05
  pvelab rollback net40 --to setup
  pvelab rollback net40 --initial""",
        "run": f"""usage: pvelab run VM... FILE [--timeout MIN]

{VM_ARGS}
  FILE        the script (must start with #!, max 1 MiB); runs as root in /root, all VMs at once
  --timeout   minutes to wait (default {RUN_TIMEOUT_MIN})
  output goes to {RUN_LOG_DIR}/<time>-<name>/<VM>.log
  snapshots (an older one with the same name is replaced):
    "before-setup"  every VM, just before setup.sh starts
    "setup"         VMs where setup.sh exited 0

examples:
  pvelab run net40 setup.sh
  pvelab run net40-student05 fix-docker.sh
  pvelab rollback net40 --to setup              later: back to right after setup.sh
  pvelab rollback net40 --to before-setup       undo setup.sh""",
        "snapshot": f"""usage: pvelab snapshot VM...

{VM_ARGS}
  the current state becomes the new "clean" that rollback returns to

examples:
  pvelab snapshot net40                         after installing the course software""",
        "expose": """usage: pvelab expose N [--host ADDR]
       pvelab expose N --off

  N           the net number (40 for net40)
  --host ADDR address shown to students (saved; default: this host's public IP)
  --off       close it again
  students connect with: ssh -p 22000+VMID student@ADDR

examples:
  pvelab expose 40
  pvelab expose 40 --off""",
        "block": f"""usage: pvelab block internet|ssh|all VM...
       pvelab unblock internet|ssh|all VM...

  internet    the VMs can't reach the internet; ssh from outside keeps working
  ssh         nobody can ssh in from outside (pvelab ssh from this host still works);
              sessions that are open get cut
  all         both
{VM_ARGS}
  only VMs made with --net N; stays after reboot and rollback; see "pvelab list" (BLOCKED)

examples:
  pvelab block internet net40                   exam: whole class offline
  pvelab block ssh net40-student07
  pvelab unblock all net40""",
        "resize": f"""usage: pvelab resize VM... [--cores N] [--memory MB] [--disk GB|+GB] [--reboot]

{VM_ARGS}
  --cores N   CPU cores
  --memory MB memory, in MB or like 4G
  --disk GB   new disk size (30) or how much to add (+10); disks can only grow
  --reboot    reboot the running VMs right away so the change takes effect
  without --reboot, running VMs get the new size at their next reboot; the filesystem
  grows into a bigger disk on that reboot as well

examples:
  pvelab resize net40 --cores 4 --memory 8G --reboot
  pvelab resize net40-student05 --disk +20""",
        "upgrade": f"""usage: pvelab upgrade

  downloads pvelab from {UPGRADE_URL} (set PVELAB_URL to use another address)
  and replaces this copy; asks for sudo only if the file isn't yours""",
        "ssh": """usage: pvelab ssh NAME [COMMAND...]

examples:
  pvelab ssh net40-student01
  pvelab ssh net40-student01 df -h""",
        "templates": """usage: pvelab templates""",
        "pull": f"""usage: pvelab pull TEMPLATE... | all

  TEMPLATE    {tpls}

examples:
  pvelab pull ubuntu24
  pvelab pull all""",
    }
    return h.get({"start": "power", "stop": "power", "reboot": "power", "ls": "list",
                  "unblock": "block"}.get(cmd, cmd))


CMD = ""                                      # the command being run, for bad_usage()


def bad_usage(problem):
    """Wrong arguments: say what is wrong, then the command's help."""
    raise LabError(f"{problem}\n\n{command_help(CMD) or ''}".rstrip())
