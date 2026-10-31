#!/bin/bash
# tests/selftest.sh - end-to-end test of pvelab on the real PVE host (run as root from a checkout;
# builds dist/pvelab first, or tests LAB_CMD if set).
# Only touches its own VMs (net91-* .. net96-*); your real student VMs are left alone.
# Uses two throwaway subnets (--net 91 / --net 92, 2 VMs each), then destroys everything it made.
# With 2 or more PVE hosts, a "two hosts" section also uses nets 95 and 96 across the hosts.
set -uo pipefail
# Modes (the first argument):
#   (none)      everything (the full run; about 30 min with two hosts)
#   --quick     the core only: one create with a script, isolation, power, destroy (about 4 min).
#               Enough after a change that does not touch networking or the multi-host code.
#   --net-only  only what depends on how a net is built (SDN vnet or bridge): nets, isolation,
#               expose, block, net auto, two hosts (about 6 min). Meant for the second mode:
#               tests/selftest.sh && LAB_SDN=0 tests/selftest.sh --net-only
MODE=full; case "${1:-}" in --quick) MODE=quick ;; --net-only) MODE=net ;; "") ;;
  *) echo "usage: tests/selftest.sh [--quick|--net-only]"; exit 2 ;; esac
want() {   # is section $1 part of this mode?
  case $MODE in
    full)  return 0 ;;
    quick) case $1 in templates|create|waiting|tests|destroy|upgrade) return 0 ;; esac ;;
    net)   case $1 in create|"used nets"|waiting|tests|expose|block|"net auto"|"two hosts"|destroy) return 0 ;; esac ;;
  esac
  return 1
}
cd "$(dirname "$0")/.." || exit 1; L=${LAB_CMD:-./dist/pvelab}
[ -n "${LAB_CMD:-}" ] || python3 build.py || exit 1
SNAP=clean
export LAB_YES=1 LAB_FORCE=1 LAB_TEMPLATE=${LAB_TEMPLATE:-9002}
N1=91; N2=92; A1=net$N1-student01; A2=net$N1-student02; B1=net$N2-student01; B2=net$N2-student02
N3=94; C11=net$N3-student01-1; C12=net$N3-student01-2; C21=net$N3-student02-1; C22=net$N3-student02-2
PASS=0; FAIL=0
# Several PVE hosts: H2 is another one. The sections before "two hosts" keep their VMs here (--node $H1),
# because they check them with the local qm.
NODES=$(pvesh get /nodes --output-format json | python3 -c 'import json,sys; print(" ".join(sorted(n["node"] for n in json.load(sys.stdin))))')
H1=$(hostname -s); H2=$(for h in $NODES; do [ "$h" != "$H1" ] && echo "$h"; done | head -1)
HERE=(); [ -n "$H2" ] && HERE=(--node "$H1")
BR=lab; [ "${LAB_SDN:-auto}" = 0 ] && BR=vmbr     # new nets: SDN vnets labN (LAB_SDN=0: bridges vmbrN)
vnet() { pvesh get /cluster/sdn/vnets --output-format json | grep -q "\"lab$1\""; }
ok()   { echo "PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL+1)); }
OUT=/tmp/pvelab-selftest.out                      # the output of the last check (shown when it fails)
check(){ local d=$1; shift; if "$@" >$OUT 2>&1; then ok "$d"; else bad "$d"; tail -n 5 $OUT | sed 's/^/      | /'; fi; }
nocheck(){ local d=$1; shift; if "$@" >$OUT 2>&1; then bad "$d"; tail -n 5 $OUT | sed 's/^/      | /'; else ok "$d"; fi; }
on()   { timeout 60 $L ssh "$1" "${@:2}"; }      # run a command inside a student VM
up()   {   # wait (up to UP_SECS, 15 min) until every VM named answers ssh; a VM that answered is not asked again
  local left="$*" now v; SECONDS=0
  while [ -n "$left" ] && [ $SECONDS -lt ${UP_SECS:-900} ]; do
    now=""; for v in $left; do on $v true 2>/dev/null || now="$now $v"; done
    left=$now; [ -n "$left" ] && sleep 2
  done
  [ -z "$left" ]
}
ipof() { awk -F'\t' -v n="$1" '$2==n{print $6}' /root/lab/credentials.tsv; }
pwof() { awk -F'\t' -v n="$1" '$2==n{print $4}' /root/lab/credentials.tsv; }
idof() { awk -F'\t' -v n="$1" '$2==n{print $1}' /root/lab/credentials.tsv; }
dnat() { iptables -t nat -C PREROUTING -i vmbr0 -p tcp --dport "$1" -j DNAT --to-destination "$2:22"; }

command -v sshpass >/dev/null || apt-get install -y -q sshpass >/dev/null
SD=/tmp/pvelab-selftest-scripts; rm -rf $SD; mkdir -p $SD
printf '#!/bin/bash\necho hello-from-setup\nid -u\ntouch /root/setup-ran\n' > $SD/course-setup.sh
printf '#!/bin/bash\necho going to fail >&2\nexit 3\n' > $SD/fail-step.sh
printf '#!/bin/bash\ntouch /root/second-ran\n' > $SD/second.sh
printf 'echo no shebang\n' > $SD/bad.sh
printf '#!/bin/bash\ntouch /root/mark-ran\n' > $SD/mark.sh
cp /etc/network/interfaces /tmp/lab-selftest.ifaces
iptables -S > /tmp/lab-selftest.ipt; iptables -t nat -S > /tmp/lab-selftest.nat; iptables -t mangle -S > /tmp/lab-selftest.mangle
mangle_blocks_match_file() {   # as many live block rules as interfaces would restore at boot
  [ "$(iptables -t mangle -S FORWARD | grep -c -- '-j DROP')" = \
    "$(grep -cE 'post-up +iptables -t mangle -C .*-j DROP' /etc/network/interfaces)" ]
}

if want "templates"; then
echo "== templates"
check   "templates lists catalogue"        bash -c "$L templates | grep -q debian-13-cloud"
nocheck "unknown template rejected"        $L create 1 no-such-os --net $N1
nocheck "no bridge left by rejected create" ip link show $BR$N1
nocheck "--expose without --net rejected"   $L create 1 --expose
nocheck "missing --script file rejected"    $L create 1 --net 93 --script /nonexistent.sh
nocheck "script without #! rejected"        $L create 1 --net 93 --script $SD/bad.sh
nocheck "disk smaller than template rejected" $L create 1 --net 93 --disk 5
nocheck "bad --memory rejected"             $L create 1 --net 93 --memory 100
nocheck "no bridge left by those"           ip link show ${BR}93
fi

echo "== create"
$L create 2 --net $N1 --expose --script $SD/course-setup.sh "${HERE[@]}" \
  && $L create 2 --net $N2 --cores 1 --memory 1536 --disk 12 --no-isolate "${HERE[@]}" \
  && { [ $MODE != full ] || $L create 2x2 --net $N3 --cores 1 --memory 1024 --script harden+$SD/mark.sh, "${HERE[@]}" 2>&1 | tee $SD/create-n3.out; } \
  || { echo "create failed"; exit 1; }
$L list
check   "net$N1 is $BR$N1"                 ip link show $BR$N1
if [ $BR = lab ]; then check "SDN vnet lab$N1" vnet $N1; check "lab$N1 has its gateway" bash -c "ip -4 addr show lab$N1 | grep -q 10.10.$N1.1/"; fi

if want "used nets"; then
echo "== used nets"
check   "own net reused (nothing missing)"  $L create 2 --net $N1 "${HERE[@]}"
ip link add ${BR}93 type bridge
nocheck "bridge not made by pvelab refused" $L create 1 --net 93
check   "... and nothing was created"       bash -c "! qm list | grep -q net93-"
ip link del ${BR}93
fi

ipA1=$(ipof $A1); ipA2=$(ipof $A2); pA1=$((22000 + $(idof $A1))); pA2=$((22000 + $(idof $A2)))
if want "waiting"; then
echo "== waiting for VMs to accept ssh (max 15 min)"
VMS="$A1 $A2 $B1 $B2"; [ $MODE = full ] && VMS="$VMS $C11 $C12 $C21 $C22"; NV=$(echo $VMS | wc -w)
up $VMS && echo "reachable: all $NV after ${SECONDS}s" || echo "not all $NV reachable after ${SECONDS}s"
fi

if want "tests"; then
echo "== tests"
for v in $A1 $A2 $B1 net$N2-student02; do check "PVE ssh -> $v" on $v true; done
nocheck "students isolated: $A1 -> $A2"    on $A1 ping -c2 -W2 "$(ipof $A2)"
nocheck "students isolated: $A1 -> $A2 :22" on $A1 "timeout 5 bash -c '</dev/tcp/$(ipof $A2)/22'"
check   "--no-isolate net: $B1 -> $B2"      on $B1 ping -c2 -W2 "$(ipof $B2)"
check   "--no-isolate net: $B1 -> $B2 :22"  on $B1 "timeout 5 bash -c '</dev/tcp/$(ipof $B2)/22'"
check   "list: net$N1 isolated"             bash -c "$L list --net $N1 | grep -q 'net$N1: students isolated'"
check   "list: net$N2 open"                 bash -c "$L list --net $N2 | grep -q 'net$N2: students can reach'"
DEV='$(ip -4 -o route get 1.1.1.1 | sed -n "s/.* dev \([^ ]*\).*/\1/p")'
nocheck "new IP doesn't get around it"      on $A1 "sudo ip addr add 10.10.$N1.250/24 dev $DEV; ping -c2 -W2 -I 10.10.$N1.250 $(ipof $A2); r=\$?; sudo ip addr del 10.10.$N1.250/24 dev $DEV; exit \$r"
nocheck "routing via the host doesn't either" on $A1 "sudo ip route add $(ipof $A2)/32 via 10.10.$N1.1; ping -c2 -W2 $(ipof $A2); r=\$?; sudo ip route del $(ipof $A2)/32; exit \$r"
check   "isolate --off"                     $L isolate --net $N1 --off
check   "... then $A1 -> $A2 works"         on $A1 ping -c2 -W2 "$(ipof $A2)"
check   "isolate again"                     $L isolate --net $N1
nocheck "... then $A1 -> $A2 is cut"        on $A1 ping -c2 -W2 "$(ipof $A2)"
check   "isolation kept for reboot"         grep -q "# isolate net $N1 begin" /etc/network/interfaces
fi

if want "several"; then
echo "== several VMs per student"
check   "own VMs: $C11 -> $C12"             on $C11 ping -c2 -W2 "$(ipof $C12)"
check   "own VMs: $C22 -> $C21 :22"         on $C22 "timeout 5 bash -c '</dev/tcp/$(ipof $C21)/22'"
nocheck "other student: $C11 -> $C21"       on $C11 ping -c2 -W2 "$(ipof $C21)"
nocheck "other student: $C21 -> $C12 :22"   on $C21 "timeout 5 bash -c '</dev/tcp/$(ipof $C12)/22'"
check   "gateway + internet from $C11"      on $C11 curl -sfI --max-time 10 https://deb.debian.org
[ "$(pwof $C11)" = "$(pwof $C12)" ] && [ "$(pwof $C11)" != "$(pwof $C21)" ] && ok "one password per student" || bad "passwords per student"
nocheck "other VMs-per-student refused"     $L create 2x3 --net $N3
nocheck "plain count refused on NxM net"    $L create 3 --net $N3
check   "handout: one card per student"     bash -c "$L handout --net $N3 -o /tmp/pvelab-ho3.html && [ \$(grep -c 'class=\"card\"' /tmp/pvelab-ho3.html) = 2 ]"; rm -f /tmp/pvelab-ho3.html
nocheck "isolated:   $A1 -> $B1 (ping)"    on $A1 ping -c2 -W2 "$(ipof $B1)"
nocheck "isolated:   $A1 -> $B1 (tcp 22)"  on $A1 "timeout 5 bash -c '</dev/tcp/$(ipof $B1)/22'"
nocheck "isolated:   $B1 -> $A1 (tcp 22)"  on $B1 "timeout 5 bash -c '</dev/tcp/$(ipof $A1)/22'"
check   "internet from $A1"                on $A1 curl -sfI --max-time 10 https://deb.debian.org
check   "internet from $B1"                on $B1 curl -sfI --max-time 10 https://deb.debian.org
check   "stop $A2"   $L stop $A2
check   "start $A2"  $L start $A2
check   "reboot $B1" $L reboot $B1
check   "stop by pattern net$N2-*"          $L stop "net$N2-*"
check   "start whole net$N2"                $L start --net $N2
check   "... really running right after"    bash -c "qm status $(idof $B1) | grep -q running && qm status $(idof $B2) | grep -q running"
nocheck "bare net$N2 points at --net"       $L start net$N2
nocheck "unknown name rejected"            $L stop no-such-vm
fi

if want "scripts"; then
echo "== scripts"
check   "create --script ran it"            on $A1 sudo test -e /root/setup-ran
check   "snapshot 'course-setup' taken"     bash -c "qm listsnapshot $(idof $A1) | grep -qw course-setup"
nocheck "create --script: no 'before-' snap" bash -c "qm listsnapshot $(idof $A1) | grep -qw before-course-setup"
check   "script log has output"             bash -c "grep -rqs hello-from-setup /root/lab/run-logs/*-course-setup/$A1/1-course-setup.log"
nocheck "run: failing script reported"      $L run --net $N2 $SD/fail-step.sh
check   "run: failure logged with exit 3"   bash -c "grep -qs 'exit code 3' /root/lab/run-logs/*-fail-step/$B1/1-fail-step.log"
nocheck "run: no snapshot after failure"    bash -c "qm listsnapshot $(idof $B1) | grep -qE '[ >]fail-step( |$)'"
check   "run on one VM"                     $L run $B1 $SD/second.sh
check   "run: it ran"                       on $B1 sudo test -e /root/second-ran
check   "run: snapshot 'second' taken"      bash -c "qm listsnapshot $(idof $B1) | grep -qw second"
check   "run: 'before-fail-step' on all"    bash -c "qm listsnapshot $(idof $B1) | grep -qw before-fail-step && qm listsnapshot $(idof $B2) | grep -qw before-fail-step"
check   "run: 'before-second' taken"        bash -c "qm listsnapshot $(idof $B1) | grep -qw before-second"
nocheck "run on $B1 only: none on $B2"      bash -c "qm listsnapshot $(idof $B2) | grep -qw before-second"
check   "rollback --to before-second"       $L rollback $B1 --to before-second
up $B1
nocheck "script undone"                     on $B1 sudo test -e /root/second-ran
check   "rollback --to second"              $L rollback $B1 --to second
up $B1
check   "script back"                       on $B1 sudo test -e /root/second-ran
# library + file chain, per VM: create 2x2 --net $N3 --script harden+mark.sh,  (nothing on -2)
check   "chain ran on $C11"                 on $C11 sudo test -e /root/mark-ran
nocheck "... not on $C12"                   on $C12 sudo test -e /root/mark-ran
check   "snapshot 'harden-mark' on $C11"    bash -c "qm listsnapshot $(idof $C11) | grep -qw harden-mark"
nocheck "... none on $C12"                  bash -c "qm listsnapshot $(idof $C12) | grep -qw harden-mark"
nocheck "create: no 'before-harden-mark'"   bash -c "qm listsnapshot $(idof $C11) | grep -qw before-harden-mark"
check   "create names 'harden (library)'"   grep -q 'harden (library)' $SD/create-n3.out
check   "harden: pwquality file on $C11"    on $C11 sudo test -e /etc/security/pwquality.conf.d/pvelab.conf
nocheck "harden: no 10-pvelab.conf on $C11"  on $C11 sudo test -e /etc/ssh/sshd_config.d/10-pvelab.conf
# $C12 ran no scripts: a clean template clone, restricting nothing
nocheck "no 10-pvelab.conf on $C12"         on $C12 sudo test -e /etc/ssh/sshd_config.d/10-pvelab.conf
nocheck "no pwquality file on $C12"         on $C12 sudo test -e /etc/security/pwquality.conf.d/pvelab.conf
check   "guest agent on $C12"               on $C12 systemctl is-active qemu-guest-agent
check   "ssh passwords on $C12"             on $C12 "sudo sshd -T | grep -qi '^passwordauthentication yes'"
check   "scripts lists harden"              bash -c "$L scripts | grep -q harden"
check   "scripts harden prints it"          bash -c "$L scripts harden | head -1 | grep -q '^#!'"
nocheck "unknown script rejected"           $L create 1 --net 93 --script no-such-script
check   "destroy net$N3"                    $L destroy --net $N3
nocheck "nft table of net$N3 gone"          nft list table bridge pvelab_net$N3
nocheck "$BR$N3 gone"                      ip link show $BR$N3
fi

if want "sizes"; then
echo "== sizes"
check   "create --cores 1"                  bash -c "qm config $(idof $B1) | grep -qx 'cores: 1'"
check   "create --memory 1536"              bash -c "qm config $(idof $B1) | grep -qx 'memory: 1536'"
check   "create --disk 12"                  bash -c "qm config $(idof $B1) | grep -q '^scsi0:.*size=12G'"
echo "   $B1 root fs: $(on $B1 "df -BG --output=size / | tail -1")"
check   "filesystem grew to the disk"       on $B1 "test \$(df -BG --output=size / | tail -1 | tr -dc 0-9) -ge 11"
check   "list shows 1c/1.5G/12G"            bash -c "$L list --net $N2 | grep $B1 | grep -q 1c/1.5G/12G"
fi

if want "list"; then
echo "== list / top / handout / ranges / dry runs"
check   "list hides passwords"             bash -c "! $L list --net $N1 | grep -qF $(pwof $A1)"
check   "list --passwords shows them"      bash -c "$L list --net $N1 --passwords | grep -qF $(pwof $A1)"
check   "list has a CPU column"            bash -c "$L list --net $N1 | head -1 | grep -q CPU"
check   "range selects both"               bash -c "[ \$($L destroy net$N1-student01..02 --dry-run | grep -c 'net$N1-student0[12]') = 2 ]"
check   "destroy --dry-run keeps the VMs"  bash -c "qm list | grep -qw $A1"
check   "resize --dry-run changes nothing" bash -c "$L resize $B1 --memory 4G --dry-run | grep -q '1536->4096' && qm config $(idof $B1) | grep -qx 'memory: 1536'"
check   "rollback --dry-run"               bash -c "$L rollback $A1 --dry-run | grep -q 'would roll back'"
check   "top shows the VMs"                bash -c "$L top --net $N1 | grep -q $A1"
check   "top --check runs"                 bash -c "$L top --check --net $N1; [ \$? -le 1 ]"
check   "status shows hosts and VMs"       bash -c "$L status --net $N1 | grep -q 'Login attempts'"
check   "status hides passwords"           bash -c "! $L status --net $N1 | grep -qF $(pwof $A1)"
check   "status works while another runs"  flock /root/lab/.lock $L status --net $N1 --since 10m
check   "handout writes a card each"       bash -c "$L handout --net $N1 -o /tmp/pvelab-ho.html && [ \$(grep -c 'class=\"card\"' /tmp/pvelab-ho.html) = 2 ]"
check   "handout file is private"          bash -c "[ \$(stat -c %a /tmp/pvelab-ho.html) = 600 ]"; rm -f /tmp/pvelab-ho.html
check   "did you mean"                     bash -c "$L start net$N1-studnet01 2>&1 | grep -q 'did you mean $A1'"
check   "version"                          bash -c "$L version | grep -qE '^pvelab [0-9]{4}\.'"
check   "completion knows the VMs"         bash -c "$L __complete 2 start net$N1 | grep -qx $A1"
check   "list works while another runs"    flock /root/lab/.lock $L list --net $N1
fi

if want "snapshot"; then
echo "== snapshot / rollback"
check   "create made 'initial' snapshot"    bash -c "qm listsnapshot $(idof $A2) | grep -qw initial"
check   "create made '$SNAP' snapshot"      bash -c "qm listsnapshot $(idof $A2) | grep -qw clean"
check   "'$SNAP' taken after first boot"    bash -c "qm listsnapshot $(idof $A2) | grep -w clean | grep -q 'after first boot'"
up $A2   # it was just restarted
check   "change something in $A2"           on $A2 touch /home/student/marker
check   "rollback $A2"                      $L rollback $A2
up $A2
check   "ssh works after rollback"          on $A2 true
nocheck "rollback removed the change"       on $A2 test -e /home/student/marker
nocheck "'clean' is from before the script" on $A2 sudo test -e /root/setup-ran
check   "rollback --to course-setup"        $L rollback $A2 --to course-setup
up $A2
check   "back to after the script"          on $A2 sudo test -e /root/setup-ran
nocheck "rollback --to unknown rejected"    $L rollback $A2 --to no-such-snap
check   "snapshot $B1 (retake)"             $L snapshot $B1
check   "$B1 still has one '$SNAP'"         bash -c "[ \$(qm listsnapshot $(idof $B1) | grep -cw clean) -eq 1 ]"
fi

if want "resize"; then
echo "== resize"
check   "resize $B1 (cpu, memory, disk +2, reboot)" $L resize $B1 --cores 2 --memory 2G --disk +2 --reboot
check   "cores now 2"                       bash -c "qm config $(idof $B1) | grep -qx 'cores: 2'"
check   "memory now 2048"                   bash -c "qm config $(idof $B1) | grep -qx 'memory: 2048'"
check   "disk now 14G (VM has snapshots)"   bash -c "qm config $(idof $B1) | grep -q '^scsi0:.*size=14G'"
up $B1
echo "   $B1 after resize: cpus=$(on $B1 nproc) mem=$(on $B1 "free -m | awk '/Mem/{print \$2}'")MB root fs=$(on $B1 "df -BG --output=size / | tail -1")"
check   "VM sees 2 CPUs"                    on $B1 "test \$(nproc) -eq 2"
check   "filesystem grew after reboot"      on $B1 "test \$(df -BG --output=size / | tail -1 | tr -dc 0-9) -ge 13"
nocheck "shrinking refused"                 $L resize $B1 --disk 5
check   "no change is fine"                 $L resize $B1 --cores 2
nocheck "resize needs an option"            $L resize $B1
fi

if want "expose"; then
echo "== expose"
check   "create --expose opened $pA1"      dnat $pA1 $ipA1
check   "expose net$N1"                     $L expose --net $N1
check   "DNAT $pA1 -> $A1"                  dnat $pA1 $ipA1
check   "DNAT $pA2 -> $A2"                  dnat $pA2 $ipA2
check   "forward rule into $BR$N1"         iptables -C FORWARD -i vmbr0 -o $BR$N1 -p tcp --dport 22 -m conntrack --ctstate DNAT -j ACCEPT
check   "list shows ssh port $pA1"          bash -c "$L list | grep -q ' $pA1 '"
check   "list --ssh gives the command"      bash -c "$L list --ssh --net $N1 | grep -q 'ssh -p $pA1 student@'"
nocheck "list --net $N1 hides net$N2"       bash -c "$L list --net $N1 | grep -q net$N2-"
check   "ifupdown2 parses expose lines"     bash -c "ifquery $BR$N1 | grep -q -- '--dport $pA1'"
nocheck "net$N2 not exposed"                bash -c "iptables -t nat -S PREROUTING | grep -q 'to-destination 10.10.$N2\.'"
check   "expose --off"                      $L expose --net $N1 --off
nocheck "DNAT gone after --off"             dnat $pA1 $ipA1
nocheck "expose lines gone after --off"     grep -q "expose net $N1" /etc/network/interfaces
check   "expose again"                      $L expose --net $N1
fi

if want "block"; then
echo "== block / unblock"
check   "block internet $A1"                $L block internet $A1
nocheck "$A1 has no internet"               on $A1 curl -sfI --max-time 8 https://deb.debian.org
check   "$A2 still has internet"            on $A2 curl -sfI --max-time 10 https://deb.debian.org
check   "ssh from host still works"         on $A1 true
check   "list shows it blocked"             bash -c "$L list --net $N1 | grep $A1 | grep -q internet"
check   "block saved for reboot"            grep -q "# block net $N1 begin" /etc/network/interfaces
check   "ifupdown2 parses block lines"      bash -c "ifquery $BR$N1 | grep -q -- '--mac-source'"
check   "live rules = boot rules"           mangle_blocks_match_file
check   "block ssh $A2"                     $L block ssh $A2
check   "ssh DROP rule for $A2"             iptables -t mangle -C FORWARD -i vmbr0 -o $BR$N1 -d $ipA2 -p tcp --dport 22 -j DROP
check   "expose refresh keeps blocks"       bash -c "$L expose --net $N1 >/dev/null && iptables -t mangle -S FORWARD | grep -q -- '-d $ipA2/32'"
nocheck "block unknown VM refused"          $L block internet no-such-vm
nocheck "block without a kind refused"      $L block $A1
check   "unblock ssh $A2"                   $L unblock ssh $A2
nocheck "ssh DROP rule gone"                iptables -t mangle -C FORWARD -i vmbr0 -o $BR$N1 -d $ipA2 -p tcp --dport 22 -j DROP
check   "live rules = boot rules (2)"       mangle_blocks_match_file

ip_before=$(ipof $A1); pw_before=$(pwof $A1)
check   "change something in $A1"           on $A1 touch /home/student/marker
check   "rollback --initial $A1"            $L rollback $A1 --initial
[ "$(ipof $A1)" = "$ip_before" ] && ok "rollback keeps IP ($ip_before)" || bad "rollback changed IP"
[ "$(pwof $A1)" = "$pw_before" ] && ok "rollback keeps password" || bad "rollback changed password"
check   "rollback keeps ssh port $pA1"      dnat $pA1 $ipA1
check   "onboot set on new VMs"             bash -c "qm config $(idof $A1) | grep -qx 'onboot: 1'"
up $A1
nocheck "--initial removed the change"      on $A1 test -e /home/student/marker
check   "password login after --initial"    sshpass -p "$(pwof $A1)" ssh -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o ConnectTimeout=10 "student@$ip_before" true
nocheck "reset command is gone"             $L reset $A1
nocheck "still offline after rollback"      on $A1 curl -sfI --max-time 8 https://deb.debian.org
check   "unblock all $A1"                   $L unblock all $A1
check   "$A1 back online"                   on $A1 curl -sfI --max-time 10 https://deb.debian.org
nocheck "per-VM block rules gone"          bash -c "sed -n '/# block net $N1 begin/,/# block net $N1 end/p' /etc/network/interfaces | grep -qE 'mac-source|-j DROP.*-d |-d .*--dport 22'"
check   "mining-port rules stay"            iptables -t mangle -C FORWARD -i $BR$N1 -o vmbr0 -p tcp -m multiport --dports 3333,4444,5555,6666,7777,9999,14433,14444,45560,45700 -m conntrack --ctstate NEW -j DROP
check   "block internet $A2 (kept to destroy)" $L block internet $A2
fi

if want "net auto"; then
echo "== net auto"
AN=$($L create 1 --net auto "${HERE[@]}" | sed -n 's/^>> using net \([0-9]*\) .*/\1/p')
[ -n "$AN" ] && ok "--net auto picked net$AN" || bad "--net auto picked nothing"
check   "--net auto made its VM"            bash -c "qm list | grep -qw net$AN-student01"
[ "$AN" != $N1 ] && [ "$AN" != $N2 ] && [ "$AN" != 10 ] && ok "net$AN was free" || bad "net$AN was in use"
check   "destroy --net $AN"                 $L destroy --net $AN
nocheck "$BR$AN gone"                      ip link show $BR$AN
fi

if want "two hosts"; then
echo "== two hosts"
# Only with 2 or more PVE hosts in the cluster. Uses self-test nets 95 (isolated) and 96 (--no-isolate).
if [ -z "$H2" ]; then
  echo "SKIP two-host (one PVE host)"
else
  echo "   hosts: $NODES (this: $H1, other: $H2)"
  H2IP=$(python3 -c 'import json,sys; print(json.load(open("/etc/pve/.members"))["nodelist"][sys.argv[1]]["ip"])' "$H2")
  KH=/etc/pve/nodes/$H2/ssh_known_hosts
  onh2() { ssh -o BatchMode=yes -o HostKeyAlias="$H2" $([ -f $KH ] && echo "-o UserKnownHostsFile=$KH -o GlobalKnownHostsFile=none") "root@$H2IP" "$@"; }
  hostof() { pvesh get /cluster/resources --type vm --output-format json | python3 -c \
    'import json,sys; print(next((r["node"] for r in json.load(sys.stdin) if r.get("name")==sys.argv[1]),""))' "$1"; }
  vmsof() { awk -F'\t' -v n="$1" '$5==n{print $2}' /root/lab/credentials.tsv; }          # VM names of net N
  first_on() { for v in $(vmsof "$1"); do [[ $v == *-2 ]] && continue; [ "$(hostof $v)" = "$2" ] && { echo $v; return; }; done; }
  copies_on_h2() { grep -lsE '^#copy( |%20)of' /etc/pve/nodes/$H2/qemu-server/*.conf | sed 's#.*/##; s#\.conf##' | sort; }
  COPIES_BEFORE=$(copies_on_h2)

  nocheck "no --net: --node $H2 refused"       $L create 1 --node $H2
  if [ $BR = vmbr ]; then
    nocheck "vmbr net$N1: --node $H2 refused"  $L create 3 --net $N1 --node $H2
    nocheck "... and nothing was created"      bash -c "qm list | grep -qw net$N1-student03"
    echo "SKIP two-host SDN checks (LAB_SDN=0: bridge nets stay on $H1)"
  else
    check   "create 4 --net 95 (auto)"         $L create 4 --net 95
    used=$(for v in $(vmsof 95); do hostof $v; done | sort -u | xargs)
    [ "$(wc -w <<<"$used")" -ge 2 ] && ok "net95 spread over hosts: $used" || bad "net95 all on one host: $used"
    check   "list shows HOST"                  bash -c "$L list --net 95 | head -1 | grep -q HOST"
    check   "create 2x2 --net 96 --no-isolate" $L create 2x2 --net 96 --no-isolate
    for s in 01 02; do
      [ "$(hostof net96-student$s-1)" = "$(hostof net96-student$s-2)" ] && ok "net96-student$s: both VMs on one host" \
        || bad "net96-student$s: VMs on different hosts"
    done
    check   "--node $H2 puts student05 there"  $L create 5 --net 95 --node $H2
    [ "$(hostof net95-student05)" = "$H2" ] && ok "net95-student05 is on $H2" || bad "net95-student05 not on $H2"
    [ -n "$(first_on 95 $H1)" ] || $L create 6 --net 95 --node $H1       # a student on each host
    [ -n "$(first_on 96 $H1)" ] || $L create 3x2 --net 96 --node $H1
    [ -n "$(first_on 96 $H2)" ] || $L create 4x2 --net 96 --node $H2
    X1=$(first_on 95 $H1); X2=$(first_on 95 $H2); G1=$(first_on 96 $H1); G2=$(first_on 96 $H2)
    echo "   net95: $X1 on $H1, $X2 on $H2; net96: $G1 on $H1, $G2 on $H2"
    up $X1 $X2 $G1 $G2
    check   "PVE ssh -> $X2 on $H2"            on $X2 true
    nocheck "isolated across hosts: $X1 -> $X2" on $X1 ping -c2 -W2 "$(ipof $X2)"
    nocheck "isolated across hosts: $X2 -> $X1 :22" on $X2 "timeout 5 bash -c '</dev/tcp/$(ipof $X1)/22'"
    check   "--no-isolate across hosts: $G1 -> $G2" on $G1 ping -c2 -W2 "$(ipof $G2)"
    check   "VM on $H2 reaches the gateway"    on $X2 ping -c2 -W2 10.10.95.1
    check   "internet from $X2 on $H2"         on $X2 curl -sfI --max-time 10 https://deb.debian.org
    check   "password login on $X2"            sshpass -p "$(pwof $X2)" ssh -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o ConnectTimeout=10 "student@$(ipof $X2)" true
    UPIP=$(ip -4 -o route get 1.1.1.1 | sed -n 's/.* src \([^ ]*\).*/\1/p'); pX2=$((22000 + $(idof $X2)))
    check   "expose --net 95"                  $L expose --net 95
    check   "DNAT $pX2 -> $X2"                 dnat $pX2 "$(ipof $X2)"
    # DNAT is for packets that come in on vmbr0, so knock from the other host (sshpass may not be there)
    check   "ssh port $pX2 answers (from $H2)" onh2 "timeout 5 bash -c 'exec 3<>/dev/tcp/$UPIP/$pX2; head -c 7 <&3' | grep -q SSH-2.0"
    check   "block internet $X2"               $L block internet $X2
    nocheck "$X2 has no internet"              on $X2 curl -sfI --max-time 8 https://deb.debian.org
    check   "unblock all $X2"                  $L unblock all $X2
    check   "$X2 back online"                  on $X2 curl -sfI --max-time 10 https://deb.debian.org
    check   "run on $X2"                       $L run $X2 $SD/mark.sh
    check   "... it ran"                       on $X2 sudo test -e /root/mark-ran
    # move: student X1 (net95, isolated) from $H1 to $H2; a 2-VM student of net96 moves as a whole (dry run)
    snapsof() { grep -ho '^\[[^]]*\]' /etc/pve/nodes/*/qemu-server/"$(idof $1)".conf | tr -d '[]' \
      | grep -vE '^(PENDING|special:.*)$' | xargs; }
    check   "move --dry-run: ${G1%-1}-2 joins $G1" bash -c "$L move $G1 --to $H2 --dry-run | grep -q 'stay together: ${G1%-1}-2'"
    nocheck "move --to an unknown host"         $L move $X1 --to no-such-host --dry-run
    check   "move $X1 --to $H2"                $L move $X1 --to $H2
    [ "$(hostof $X1)" = "$H2" ] && ok "$X1 is on $H2" || bad "$X1 is not on $H2"
    [ "$(snapsof $X1)" = "$SNAP" ] && ok "$X1 has only '$SNAP'" || bad "$X1 snapshots: $(snapsof $X1)"
    UP_SECS=150 up $X1
    check   "moved $X1 answers"                on $X1 true
    nocheck "isolated after the move: $X1 -> $X2" on $X1 ping -c2 -W2 "$(ipof $X2)"
    X3=$(first_on 95 $H1)
    [ -z "$X3" ] || nocheck "isolated across hosts: $X1 -> $X3" on $X1 ping -c2 -W2 "$(ipof $X3)"
    check   "internet from moved $X1"          on $X1 curl -sfI --max-time 10 https://deb.debian.org
    check   "destroy --net 95 --net 96"        $L destroy --net 95 --net 96
    nocheck "no net95.nft on $H2"              onh2 test -e /etc/pvelab/nft/net95.nft
    nocheck "no pvelab_net95 table on $H2"     onh2 nft list table bridge pvelab_net95
    nocheck "no net95-/net96- VM configs left" bash -c "grep -qsE '^name: net9[56]-' /etc/pve/nodes/*/qemu-server/*.conf"
  fi
  for id in $(comm -13 <(echo "$COPIES_BEFORE") <(copies_on_h2)); do   # template copies this section made
    check   "delete template copy $id on $H2"  onh2 qm destroy $id --purge 1
    nocheck "... it is gone"                   test -e /etc/pve/nodes/$H2/qemu-server/$id.conf
  done
fi
fi

TESTIDS="$(idof $A1) $(idof $A2) $(idof $B1) $(idof net$N2-student02)"
if want "destroy"; then
echo "== destroy"
check   "destroy $A1"                       $L destroy $A1
if want expose; then
  nocheck "port $pA1 dropped with $A1"      dnat $pA1 $ipA1
  check   "port $pA2 kept for $A2"          dnat $pA2 $ipA2
fi
check   "destroy $A2 + net$N2 in one call"  $L destroy $A2 --net=$N2
for v in $A2 $B1 net$N2-student02; do nocheck "$v gone from qm list" bash -c "qm list | grep -qw $v"; [ -z "$(idof $v)" ] && ok "$v credentials gone" || bad "$v credentials left"; done
[ -z "$(awk -F'\t' '$5=='$N1' || $5=='$N2'' /root/lab/credentials.tsv)" ] && ok "credentials cleaned" || bad "credentials left over"
nocheck "$BR$N1 gone" ip link show $BR$N1
nocheck "nft table of net$N1 gone" nft list table bridge pvelab_net$N1
nocheck "$BR$N2 gone" ip link show $BR$N2
for n in $N1 $N2 $N3; do nocheck "no SDN vnet lab$n left" vnet $n; done
check   "/etc/network/interfaces restored" diff -q /tmp/lab-selftest.ifaces /etc/network/interfaces
check   "iptables restored"                diff /tmp/lab-selftest.ipt <(iptables -S)
check   "iptables nat restored"            diff -q /tmp/lab-selftest.nat <(iptables -t nat -S)
check   "iptables mangle restored"         diff -q /tmp/lab-selftest.mangle <(iptables -t mangle -S)
left=0; for id in $TESTIDS; do grep -qs "\"$id\"" /root/lab/blocks.json && left=1; done
[ $left = 0 ] && ok "blocks.json cleaned" || bad "blocks.json still lists test VMs"
fi

if want "upgrade"; then
echo "== upgrade"
U=/tmp/pvelab-upgrade-test; rm -rf $U; mkdir -p $U; cp $L $U/pvelab
check   "upgrade: up to date"              bash -c "PVELAB_URL=file://$PWD/dist/pvelab $U/pvelab upgrade | grep -q 'up to date'"
python3 -c "import sys,zipfile; z=zipfile.ZipFile(sys.argv[1],'a'); z.comment=b'old'; z.close()" $U/pvelab
nocheck "upgrade: old copy differs"         cmp -s $U/pvelab $PWD/dist/pvelab
check   "upgrade: replaces an old copy"    bash -c "PVELAB_URL=file://$PWD/dist/pvelab $U/pvelab upgrade | grep -q updated && cmp -s $U/pvelab $PWD/dist/pvelab"
check   "upgrade: keeps it executable"     test -x $U/pvelab
printf '<html>login</html>' > $U/fake
nocheck "upgrade: HTML page refused"       bash -c "PVELAB_URL=file://$U/fake $U/pvelab upgrade"
printf '#!/usr/bin/env python3\nprint(1)\n' > $U/plain
nocheck "upgrade: non-zip python refused"   bash -c "PVELAB_URL=file://$U/plain $U/pvelab upgrade"
check   "upgrade: copy untouched after refusal" cmp -s $U/pvelab $PWD/dist/pvelab
nocheck "upgrade: refused from a checkout"  bash -c "PVELAB_URL=file://$PWD/dist/pvelab PYTHONPATH=src python3 -m pvelab upgrade"
rm -rf $U
check   "templates still present"          bash -c 'qm config 9001 && qm config 9002 && qm config 9003'
fi

echo; echo "RESULT ($MODE): $PASS passed, $FAIL failed"
