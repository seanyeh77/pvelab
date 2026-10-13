#!/bin/bash
# tests/selftest.sh - end-to-end test of pvelab on the real PVE host (run as root from a checkout;
# builds dist/pvelab first, or tests LAB_CMD if set).
# Only touches its own VMs (net91-*/net92-*); your real student VMs are left alone.
# Uses two throwaway subnets (--net 91 / --net 92, 2 VMs each), then destroys everything it made.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1; L=${LAB_CMD:-./dist/pvelab}
[ -n "${LAB_CMD:-}" ] || python3 build.py || exit 1
SNAP=clean
export LAB_YES=1 LAB_FORCE=1 LAB_TEMPLATE=${LAB_TEMPLATE:-9002}
N1=91; N2=92; A1=net$N1-student01; A2=net$N1-student02; B1=net$N2-student01; B2=net$N2-student02
PASS=0; FAIL=0
ok()   { echo "PASS  $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL  $*"; FAIL=$((FAIL+1)); }
check(){ local d=$1; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
nocheck(){ local d=$1; shift; if "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }
on()   { timeout 60 $L ssh "$1" "${@:2}"; }      # run a command inside a student VM
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
cp /etc/network/interfaces /tmp/lab-selftest.ifaces
iptables -S > /tmp/lab-selftest.ipt; iptables -t nat -S > /tmp/lab-selftest.nat; iptables -t mangle -S > /tmp/lab-selftest.mangle
mangle_blocks_match_file() {   # as many live block rules as interfaces would restore at boot
  [ "$(iptables -t mangle -S FORWARD | grep -c -- '-j DROP')" = \
    "$(grep -cE 'post-up +iptables -t mangle -A .*-j DROP' /etc/network/interfaces)" ]
}

echo "== templates"
check   "templates lists catalogue"        bash -c "$L templates | grep -q debian-13-cloud"
nocheck "unknown template rejected"        $L create 1 no-such-os --net $N1
nocheck "no bridge left by rejected create" ip link show vmbr$N1
nocheck "--expose without --net rejected"   $L create 1 --expose
nocheck "missing --script file rejected"    $L create 1 --net 93 --script /nonexistent.sh
nocheck "script without #! rejected"        $L create 1 --net 93 --script $SD/bad.sh
nocheck "disk smaller than template rejected" $L create 1 --net 93 --disk 5
nocheck "bad --memory rejected"             $L create 1 --net 93 --memory 100
nocheck "no bridge left by those"           ip link show vmbr93

echo "== create"
$L create 2 --net $N1 --expose --script $SD/course-setup.sh && $L create 2 --net $N2 --cores 1 --memory 1536 --disk 12 || { echo "create failed"; exit 1; }
$L list

echo "== used nets"
check   "own net reused (nothing missing)"  $L create 2 --net $N1
ip link add vmbr93 type bridge
nocheck "bridge not made by pvelab refused" $L create 1 --net 93
check   "... and nothing was created"       bash -c "! qm list | grep -q net93-"
ip link del vmbr93

echo "== waiting for VMs to accept ssh (max 15 min)"
for i in $(seq 1 90); do
  n=0; for v in $A1 $A2 $B1 net$N2-student02; do on $v true 2>/dev/null && n=$((n+1)); done
  [ $n -eq 4 ] && break; sleep 10
done
echo "reachable: $n/4 after ~$((i*10))s"

echo "== tests"
for v in $A1 $A2 $B1 net$N2-student02; do check "PVE ssh -> $v" on $v true; done
check   "same subnet: $A1 -> $A2"          on $A1 ping -c2 -W2 "$(ipof $A2)"
nocheck "isolated:   $A1 -> $B1 (ping)"    on $A1 ping -c2 -W2 "$(ipof $B1)"
nocheck "isolated:   $A1 -> $B1 (tcp 22)"  on $A1 timeout 5 bash -c "</dev/tcp/$(ipof $B1)/22"
nocheck "isolated:   $B1 -> $A1 (tcp 22)"  on $B1 timeout 5 bash -c "</dev/tcp/$(ipof $A1)/22"
check   "internet from $A1"                on $A1 curl -sfI --max-time 10 https://deb.debian.org
check   "internet from $B1"                on $B1 curl -sfI --max-time 10 https://deb.debian.org
check   "stop $A2"   $L stop $A2
check   "start $A2"  $L start $A2
check   "reboot $B1" $L reboot $B1
check   "stop by pattern net$N2-*"          $L stop "net$N2-*"
check   "start whole net$N2"                $L start net$N2
nocheck "unknown name rejected"            $L stop no-such-vm

echo "== scripts"
check   "create --script ran it"            on $A1 sudo test -e /root/setup-ran
check   "snapshot 'course-setup' taken"     bash -c "qm listsnapshot $(idof $A1) | grep -qw course-setup"
nocheck "create --script: no 'before-' snap" bash -c "qm listsnapshot $(idof $A1) | grep -qw before-course-setup"
check   "script log has output"             bash -c "grep -rqs hello-from-setup /root/lab/run-logs/*-course-setup/$A1.log"
nocheck "run: failing script reported"      $L run net$N2 $SD/fail-step.sh
check   "run: failure logged with exit 3"   bash -c "grep -qs 'exit code 3' /root/lab/run-logs/*-fail-step/$B1.log"
nocheck "run: no snapshot after failure"    bash -c "qm listsnapshot $(idof $B1) | grep -qE '[ >]fail-step( |$)'"
check   "run on one VM"                     $L run $B1 $SD/second.sh
check   "run: it ran"                       on $B1 sudo test -e /root/second-ran
check   "run: snapshot 'second' taken"      bash -c "qm listsnapshot $(idof $B1) | grep -qw second"
check   "run: 'before-fail-step' on all"    bash -c "qm listsnapshot $(idof $B1) | grep -qw before-fail-step && qm listsnapshot $(idof $B2) | grep -qw before-fail-step"
check   "run: 'before-second' taken"        bash -c "qm listsnapshot $(idof $B1) | grep -qw before-second"
nocheck "run on $B1 only: none on $B2"      bash -c "qm listsnapshot $(idof $B2) | grep -qw before-second"
check   "rollback --to before-second"       $L rollback $B1 --to before-second
for i in $(seq 1 90); do on $B1 true 2>/dev/null && break; sleep 10; done
nocheck "script undone"                     on $B1 sudo test -e /root/second-ran
check   "rollback --to second"              $L rollback $B1 --to second
for i in $(seq 1 90); do on $B1 true 2>/dev/null && break; sleep 10; done
check   "script back"                       on $B1 sudo test -e /root/second-ran

echo "== sizes"
check   "create --cores 1"                  bash -c "qm config $(idof $B1) | grep -qx 'cores: 1'"
check   "create --memory 1536"              bash -c "qm config $(idof $B1) | grep -qx 'memory: 1536'"
check   "create --disk 12"                  bash -c "qm config $(idof $B1) | grep -q '^scsi0:.*size=12G'"
echo "   $B1 root fs: $(on $B1 "df -BG --output=size / | tail -1")"
check   "filesystem grew to the disk"       on $B1 "test \$(df -BG --output=size / | tail -1 | tr -dc 0-9) -ge 11"
check   "list shows 1c/1.5G/12G"            bash -c "$L list --net $N2 | grep $B1 | grep -q 1c/1.5G/12G"

echo "== snapshot / rollback"
check   "create made 'initial' snapshot"    bash -c "qm listsnapshot $(idof $A2) | grep -qw initial"
check   "create made '$SNAP' snapshot"      bash -c "qm listsnapshot $(idof $A2) | grep -qw clean"
check   "'$SNAP' taken after first boot"    bash -c "qm listsnapshot $(idof $A2) | grep -w clean | grep -q 'after first boot'"
for i in $(seq 1 90); do on $A2 true 2>/dev/null && break; sleep 10; done   # it was just restarted
check   "change something in $A2"           on $A2 touch /home/student/marker
check   "rollback $A2"                      $L rollback $A2
for i in $(seq 1 90); do on $A2 true 2>/dev/null && break; sleep 10; done
check   "ssh works after rollback"          on $A2 true
nocheck "rollback removed the change"       on $A2 test -e /home/student/marker
nocheck "'clean' is from before the script" on $A2 sudo test -e /root/setup-ran
check   "rollback --to course-setup"        $L rollback $A2 --to course-setup
for i in $(seq 1 90); do on $A2 true 2>/dev/null && break; sleep 10; done
check   "back to after the script"          on $A2 sudo test -e /root/setup-ran
nocheck "rollback --to unknown rejected"    $L rollback $A2 --to no-such-snap
check   "snapshot $B1 (retake)"             $L snapshot $B1
check   "$B1 still has one '$SNAP'"         bash -c "[ \$(qm listsnapshot $(idof $B1) | grep -cw clean) -eq 1 ]"

echo "== resize"
check   "resize $B1 (cpu, memory, disk +2, reboot)" $L resize $B1 --cores 2 --memory 2G --disk +2 --reboot
check   "cores now 2"                       bash -c "qm config $(idof $B1) | grep -qx 'cores: 2'"
check   "memory now 2048"                   bash -c "qm config $(idof $B1) | grep -qx 'memory: 2048'"
check   "disk now 14G (VM has snapshots)"   bash -c "qm config $(idof $B1) | grep -q '^scsi0:.*size=14G'"
for i in $(seq 1 90); do on $B1 true 2>/dev/null && break; sleep 10; done
echo "   $B1 after resize: cpus=$(on $B1 nproc) mem=$(on $B1 "free -m | awk '/Mem/{print \$2}'")MB root fs=$(on $B1 "df -BG --output=size / | tail -1")"
check   "VM sees 2 CPUs"                    on $B1 "test \$(nproc) -eq 2"
check   "filesystem grew after reboot"      on $B1 "test \$(df -BG --output=size / | tail -1 | tr -dc 0-9) -ge 13"
nocheck "shrinking refused"                 $L resize $B1 --disk 5
check   "no change is fine"                 $L resize $B1 --cores 2
nocheck "resize needs an option"            $L resize $B1

echo "== expose"
ipA1=$(ipof $A1); ipA2=$(ipof $A2); pA1=$((22000 + $(idof $A1))); pA2=$((22000 + $(idof $A2)))
check   "create --expose opened $pA1"      dnat $pA1 $ipA1
check   "expose net$N1"                     $L expose $N1
check   "DNAT $pA1 -> $A1"                  dnat $pA1 $ipA1
check   "DNAT $pA2 -> $A2"                  dnat $pA2 $ipA2
check   "forward rule into vmbr$N1"         iptables -C FORWARD -i vmbr0 -o vmbr$N1 -p tcp --dport 22 -m conntrack --ctstate DNAT -j ACCEPT
check   "list shows ssh port $pA1"          bash -c "$L list | grep -q ' $pA1 '"
check   "list --ssh gives the command"      bash -c "$L list --ssh --net $N1 | grep -q 'ssh -p $pA1 student@'"
nocheck "list --net $N1 hides net$N2"       bash -c "$L list --net $N1 | grep -q net$N2-"
check   "ifupdown2 parses expose lines"     bash -c "ifquery vmbr$N1 | grep -q -- '--dport $pA1'"
nocheck "net$N2 not exposed"                bash -c "iptables -t nat -S PREROUTING | grep -q 'to-destination 10.10.$N2\.'"
check   "expose --off"                      $L expose $N1 --off
nocheck "DNAT gone after --off"             dnat $pA1 $ipA1
nocheck "expose lines gone after --off"     grep -q "expose net $N1" /etc/network/interfaces
check   "expose again"                      $L expose $N1

echo "== block / unblock"
check   "block internet $A1"                $L block internet $A1
nocheck "$A1 has no internet"               on $A1 curl -sfI --max-time 8 https://deb.debian.org
check   "$A2 still has internet"            on $A2 curl -sfI --max-time 10 https://deb.debian.org
check   "ssh from host still works"         on $A1 true
check   "list shows it blocked"             bash -c "$L list --net $N1 | grep $A1 | grep -q internet"
check   "block saved for reboot"            grep -q "# block net $N1 begin" /etc/network/interfaces
check   "ifupdown2 parses block lines"      bash -c "ifquery vmbr$N1 | grep -q -- '--mac-source'"
check   "live rules = boot rules"           mangle_blocks_match_file
check   "block ssh $A2"                     $L block ssh $A2
check   "ssh DROP rule for $A2"             iptables -t mangle -C FORWARD -i vmbr0 -o vmbr$N1 -d $ipA2 -p tcp --dport 22 -j DROP
check   "expose refresh keeps blocks"       bash -c "$L expose $N1 >/dev/null && iptables -t mangle -S FORWARD | grep -q -- '-d $ipA2/32'"
nocheck "block unknown VM refused"          $L block internet no-such-vm
nocheck "block without a kind refused"      $L block $A1
check   "unblock ssh $A2"                   $L unblock ssh $A2
nocheck "ssh DROP rule gone"                iptables -t mangle -C FORWARD -i vmbr0 -o vmbr$N1 -d $ipA2 -p tcp --dport 22 -j DROP
check   "live rules = boot rules (2)"       mangle_blocks_match_file

ip_before=$(ipof $A1); pw_before=$(pwof $A1)
check   "change something in $A1"           on $A1 touch /home/student/marker
check   "rollback --initial $A1"            $L rollback $A1 --initial
[ "$(ipof $A1)" = "$ip_before" ] && ok "rollback keeps IP ($ip_before)" || bad "rollback changed IP"
[ "$(pwof $A1)" = "$pw_before" ] && ok "rollback keeps password" || bad "rollback changed password"
check   "rollback keeps ssh port $pA1"      dnat $pA1 $ipA1
check   "onboot set on new VMs"             bash -c "qm config $(idof $A1) | grep -qx 'onboot: 1'"
for i in $(seq 1 90); do on $A1 true 2>/dev/null && break; sleep 10; done
nocheck "--initial removed the change"      on $A1 test -e /home/student/marker
check   "password login after --initial"    sshpass -p "$(pwof $A1)" ssh -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o ConnectTimeout=10 "student@$ip_before" true
nocheck "reset command is gone"             $L reset $A1
nocheck "still offline after rollback"      on $A1 curl -sfI --max-time 8 https://deb.debian.org
check   "unblock all $A1"                   $L unblock all $A1
check   "$A1 back online"                   on $A1 curl -sfI --max-time 10 https://deb.debian.org
nocheck "block section gone when empty"     grep -q "# block net $N1 begin" /etc/network/interfaces
check   "block internet $A2 (kept to destroy)" $L block internet $A2

echo "== net auto"
AN=$($L create 1 --net auto | sed -n 's/^>> using net \([0-9]*\) .*/\1/p')
[ -n "$AN" ] && ok "--net auto picked net$AN" || bad "--net auto picked nothing"
check   "--net auto made its VM"            bash -c "qm list | grep -qw net$AN-student01"
[ "$AN" != $N1 ] && [ "$AN" != $N2 ] && [ "$AN" != 10 ] && ok "net$AN was free" || bad "net$AN was in use"
check   "destroy net$AN"                    $L destroy net$AN
nocheck "vmbr$AN gone"                      ip link show vmbr$AN

TESTIDS="$(idof $A1) $(idof $A2) $(idof $B1) $(idof net$N2-student02)"
echo "== destroy"
check   "destroy $A1"                       $L destroy $A1
nocheck "port $pA1 dropped with $A1"        dnat $pA1 $ipA1
check   "port $pA2 kept for $A2"            dnat $pA2 $ipA2
check   "destroy $A2 + net$N2 in one call"  $L destroy $A2 net$N2
for v in $A2 $B1 net$N2-student02; do nocheck "$v gone from qm list" bash -c "qm list | grep -qw $v"; [ -z "$(idof $v)" ] && ok "$v credentials gone" || bad "$v credentials left"; done
[ -z "$(awk -F'\t' '$5=='$N1' || $5=='$N2'' /root/lab/credentials.tsv)" ] && ok "credentials cleaned" || bad "credentials left over"
nocheck "vmbr$N1 gone" ip link show vmbr$N1
nocheck "vmbr$N2 gone" ip link show vmbr$N2
check   "/etc/network/interfaces restored" diff -q /tmp/lab-selftest.ifaces /etc/network/interfaces
check   "iptables restored"                diff /tmp/lab-selftest.ipt <(iptables -S)
check   "iptables nat restored"            diff -q /tmp/lab-selftest.nat <(iptables -t nat -S)
check   "iptables mangle restored"         diff -q /tmp/lab-selftest.mangle <(iptables -t mangle -S)
left=0; for id in $TESTIDS; do grep -qs "\"$id\"" /root/lab/blocks.json && left=1; done
[ $left = 0 ] && ok "blocks.json cleaned" || bad "blocks.json still lists test VMs"

echo "== upgrade"
U=/tmp/pvelab-upgrade-test; rm -rf $U; mkdir -p $U; cp $L $U/pvelab
check   "upgrade: up to date"              bash -c "PVELAB_URL=file://$PWD/dist/pvelab $U/pvelab upgrade | grep -q 'up to date'"
echo "# old" >> $U/pvelab
check   "upgrade: replaces an old copy"    bash -c "PVELAB_URL=file://$PWD/dist/pvelab $U/pvelab upgrade | grep -q updated && cmp -s $U/pvelab $PWD/dist/pvelab"
check   "upgrade: keeps it executable"     test -x $U/pvelab
printf '<html>login</html>' > $U/fake
nocheck "upgrade: HTML page refused"       bash -c "PVELAB_URL=file://$U/fake $U/pvelab upgrade"
check   "upgrade: copy untouched after refusal" cmp -s $U/pvelab $PWD/dist/pvelab
rm -rf $U
check   "templates still present"          bash -c 'qm config 9001 && qm config 9002 && qm config 9003'

echo; echo "RESULT: $PASS passed, $FAIL failed"
