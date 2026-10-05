#!/usr/bin/env python3
"""netlab: hands-on Linux networking labs.

Every "host" in a lab is a Linux network namespace living inside this container,
wired together with veth cables, bridges and routers. The commands you type act on
real kernel state, and `lab check` inspects that state to grade you.
"""
import ipaddress
import json
import os
import random
import re
import subprocess
import sys
import textwrap

DATA = os.path.expanduser("~/.netlab")
PROGRESS = os.path.join(DATA, "progress.json")
STATE = os.path.join(DATA, "state.json")

# ---------------------------------------------------------------- terminal output

C = dict(b="\033[1m", d="\033[2m", r="\033[31m", g="\033[32m", y="\033[33m",
         bl="\033[34m", m="\033[35m", c="\033[36m", x="\033[0m")


def col(s, *styles):
    return "".join(C[k] for k in styles) + s + C["x"]


def say(s=""):
    print(s)


def block(text, indent="  "):
    """Print a dedented block, highlighting `code` spans and lines that start with '$ '."""
    for line in textwrap.dedent(text).strip("\n").splitlines():
        if line.lstrip().startswith("$ "):
            say(indent + col(line, "c"))
        else:
            say(indent + re.sub(r"`([^`]+)`", lambda m: col(m.group(1), "c"), line))


def header(s):
    say()
    say(col("━━ " + s + " " + "━" * max(0, 68 - len(s)), "b", "bl"))


def ok(s):
    say("  " + col("✔ ", "g") + s)


def bad(s):
    say("  " + col("✘ ", "r") + s)


# ---------------------------------------------------------------- shell helpers


def sh(cmd, timeout=15):
    """Run a bash command, return (rc, combined output)."""
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def ns_ok(ns, cmd, timeout=10):
    return sh(f"ip netns exec {ns} {cmd}", timeout)[0] == 0


def ping(ns, ip, extra=""):
    return ns_ok(ns, f"ping -c1 -W1 {extra} {ip}") or ns_ok(ns, f"ping -c2 -W1 {extra} {ip}")


def tcp_open(ns, ip, port):
    return ns_ok(ns, f"nc -z -w1 {ip} {port}", timeout=5)


def jcmd(cmd):
    rc, out = sh(cmd)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def link_info(ns, dev):
    pre = f"-n {ns} " if ns else ""
    data = jcmd(f"ip {pre}-j addr show dev {dev}")
    return data[0] if data else None


def addrs(ns, dev=None):
    pre = f"-n {ns} " if ns else ""
    out = []
    for l in jcmd(f"ip {pre}-j addr show" + (f" dev {dev}" if dev else "")):
        for a in l.get("addr_info", []):
            if a.get("family") == "inet":
                out.append(f"{a['local']}/{a['prefixlen']}")
    return out


def routes(ns):
    return jcmd(f"ip -n {ns} -j route show")


def sysctl(ns, key):
    rc, out = sh(f"ip netns exec {ns} sysctl -n {key}")
    return out.strip()


def rmac(r):
    return "02:" + ":".join(f"{r.randrange(256):02x}" for _ in range(5))


def token(r, n=8):
    return "".join(r.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(n))


PRELUDE = r"""
set -e
_i=0
mkns() {   # mkns NAME...    create isolated hosts (network namespaces)
  for n in "$@"; do
    ip netns add "$n"; ip -n "$n" link set lo up
    # fail DNS fast: Docker's resolver is unreachable from lab hosts
    mkdir -p /etc/netns/$n; printf 'nameserver 127.0.0.1\noptions timeout:1 attempts:1\n' > /etc/netns/$n/resolv.conf
    ip netns exec "$n" sysctl -qw net.ipv4.ip_forward=0 net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1
  done
}
_mv() {
  if [ "$1" = root ]; then ip link set "$2" name "$3"; ip link set "$3" up
  else ip link set "$2" netns "$1"; ip -n "$1" link set "$2" name "$3"; ip -n "$1" link set "$3" up; fi
}
wire() {   # wire NS1 DEV1 NS2 DEV2   connect two hosts with a virtual cable
  _i=$((_i+1)); ip link add lnka$_i type veth peer name lnkb$_i
  _mv "$1" lnka$_i "$2"; _mv "$3" lnkb$_i "$4"
}
ipa() { ip -n "$1" addr add "$3" dev "$2"; }          # ipa NS DEV CIDR
gw()  { ip -n "$1" route add default via "$2"; }      # gw NS GATEWAY
fwd() { ip netns exec "$1" sysctl -qw net.ipv4.ip_forward=1; }
mac() { ip -n "$1" link set "$2" address "$3"; }
sw()  { local s=$1; shift; ip -n "$s" link add br0 type bridge; ip -n "$s" link set br0 up
        for p in "$@"; do ip -n "$s" link set "$p" master br0; done; }
bg()  { local n="$1"; shift; ip netns exec "$n" setsid "$@" >/dev/null 2>&1 </dev/null & }
mkdir -p /run/netlab
httpd() {  # httpd NS IP PORT DIR   tiny static web server
  cat > /run/netlab/httpd.py <<'PY'
import sys, functools, http.server, socketserver
socketserver.ThreadingTCPServer.allow_reuse_address = True
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[4])
h.log_message = lambda *a: None
socketserver.ThreadingTCPServer((sys.argv[2], int(sys.argv[3])), h).serve_forever()
PY
  bg "$1" python3 /run/netlab/httpd.py "$@"
}
"""

TEARDOWN = r"""
for n in $(ip netns list | awk '{print $1}'); do
  for p in $(ip netns pids "$n" 2>/dev/null); do
    case " $KEEP " in *" $p "*) ;; *) kill -9 "$p" 2>/dev/null || true;; esac
  done
  ip netns del "$n" 2>/dev/null || true
done
for l in $(ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1); do
  case "$l" in lab*|br0|lnk*|veth*) ip link del "$l" 2>/dev/null || true;; esac
done
rm -rf /etc/netns/* /run/netlab
"""


def ancestors():
    pids, p = [], os.getpid()
    while p > 1:
        pids.append(str(p))
        try:
            with open(f"/proc/{p}/stat") as f:
                p = int(f.read().rsplit(")", 1)[1].split()[1])
        except OSError:
            break
    return " ".join(pids)


def teardown():
    env = dict(os.environ, KEEP=ancestors())
    subprocess.run(["bash", "-c", TEARDOWN], env=env, capture_output=True)


def run_setup(script):
    teardown()
    p = subprocess.run(["bash", "-c", PRELUDE + script], capture_output=True, text=True)
    if p.returncode != 0:
        say(col("Lab setup failed (this is a netlab bug):", "r"))
        say(p.stdout + p.stderr)
        sys.exit(1)
    sh("sleep 0.6")  # let background services bind


# ---------------------------------------------------------------- lessons

LESSONS = []


def lesson(**kw):
    def deco(fn):
        kw.setdefault("kind", "state")
        kw["setup"] = fn
        LESSONS.append(kw)
        return fn
    return deco


def checker(i):
    def deco(fn):
        LESSONS[i - 1]["check"] = fn
        return fn
    return deco


# --- Module 1: interfaces & addressing ------------------------------------------

@lesson(module="Interfaces & addressing", title="Bring an interface up",
        topo="""
          container (root namespace)
            └── lab0   (a virtual NIC: currently DOWN, no address)
        """,
        brief="""
          `ip` (from iproute2) is THE Linux networking tool. `ifconfig` is legacy; know `ip`.

            $ ip link                      # layer 2: interfaces, state, MAC, MTU
            $ ip -br addr                  # brief: one line per interface with IPs
            $ ip link set DEV up|down      # administrative state
            $ ip addr add 10.1.2.3/24 dev DEV
            $ ip route                     # routing table

          When you add an address with a prefix, the kernel automatically installs a
          "connected" route for that subnet (proto kernel scope link).

          Ignore tunl0, gre0, sit0, ip6tnl0 and friends: always-DOWN placeholder
          devices the kernel creates in every namespace when tunnel modules are loaded.
        """,
        task="Bring interface `lab0` UP and give it the address `10.10.0.1/24`.",
        hints=["Start by looking: `ip link show lab0` — note the state and flags in <...>.",
               "Two commands: one with `ip link set`, one with `ip addr add`."],
        solution="ip link set lab0 up\nip addr add 10.10.0.1/24 dev lab0\nip route   # see the connected route",
        explain="""
          `ip addr add` with /24 installed `10.10.0.0/24 dev lab0 proto kernel scope link`
          for you. That connected route is how the kernel knows 10.10.0.x is directly
          reachable on lab0 (resolve with ARP, no gateway). Note: dummy interfaces show
          state UNKNOWN when up; the <UP> flag is the admin state, LOWER_UP is carrier.
        """)
def _(r):
    return "ip link add lab0 type dummy", {}


@checker(1)
def _(st, ans):
    info = link_info(None, "lab0")
    if not info:
        bad("lab0 no longer exists. Run `lab reset`."); return False
    up = "UP" in info.get("flags", [])
    (ok if up else bad)("lab0 is administratively UP" if up else "lab0 is still DOWN")
    has = "10.10.0.1/24" in addrs(None, "lab0")
    (ok if has else bad)("lab0 has 10.10.0.1/24" if has else f"lab0 addresses: {addrs(None, 'lab0') or 'none'}")
    return up and has


@lesson(module="Interfaces & addressing", title="Interface forensics", kind="answer",
        topo="""
          container (root namespace)
            ├── lab-a   ├── lab-b   ├── lab-c   └── lab-d
        """,
        brief="""
          Useful views of the same data:

            $ ip -br link                  # name, state, MAC
            $ ip -d link show DEV          # details (type, mtu, ...)
            $ ip -j link | python3 -m json.tool   # JSON, great for scripting
            $ cat /sys/class/net/DEV/mtu   # sysfs: every attribute is a file

          MTU = largest L3 packet the interface sends without fragmenting.
          Standard Ethernet is 1500; "jumbo frames" (9000) are common in datacenters.
        """,
        task="Four interfaces named lab-* exist. One of them runs jumbo frames (MTU 9000). "
             "What is that interface's MAC address?  Submit with `lab answer <mac>`.",
        hints=["`ip link` shows `mtu N` and `link/ether XX:..` for each interface.",
               "Try `ip link | grep -A1 'mtu 9000'`."],
        solution="ip link | grep -A1 'mtu 9000'",
        explain="""
          MTU mismatches between neighbours are a classic outage cause: small packets
          (pings, TCP handshakes) work, large ones silently vanish. You'll debug one later.
        """)
def _(r):
    names = ["lab-a", "lab-b", "lab-c", "lab-d"]
    jumbo = r.choice(names)
    macs = {n: rmac(r) for n in names}
    lines = []
    for n in names:
        mtu = 9000 if n == jumbo else r.choice([1500, 1500, 1400, 4000])
        lines.append(f"ip link add {n} type dummy; ip link set {n} address {macs[n]} mtu {mtu} up")
    return "\n".join(lines), {"answer": macs[jumbo]}


@lesson(module="Interfaces & addressing", title="Longest prefix match", kind="answer",
        topo="""
          host `rt` (a namespace) has eth0 = 10.255.0.10/16 and a pile of static routes
        """,
        brief="""
          Network namespaces are isolated copies of the network stack: own interfaces,
          routes, ARP table, firewall. Every "host" in these labs is one. To act inside one:

            $ ip netns list
            $ ip netns exec rt ip route    # run one command in namespace rt
            $ ip -n rt route               # shorthand for ip commands
            $ ip netns exec rt bash        # open a shell inside it (exit to leave)

          Route lookup picks the MOST SPECIFIC matching prefix (longest prefix match), not
          the first line. Ask the kernel directly:

            $ ip -n rt route get 1.2.3.4
        """,
        task="Inside host `rt`, a packet is sent to the destination shown by `lab show`. "
             "Which next-hop (gateway IP) will the kernel use? Work it out from `ip route` "
             "first, then confirm with `ip route get`. Submit with `lab answer <ip>`.",
        hints=["List the routes: `ip -n rt route`. Which prefixes contain the destination?",
               "Of those, the one with the largest /N wins. `ip -n rt route get <dest>` confirms."],
        solution="ip -n rt route\nip -n rt route get {dest}",
        explain="""
          Longest prefix match is the core of all IP forwarding, on Linux hosts and on
          routers alike. The default route (0.0.0.0/0) is just the least specific prefix.
        """)
def _(r):
    x, y = r.randrange(1, 250), r.randrange(0, 256)
    tbl = [("default", "10.255.0.1"), ("10.0.0.0/8", "10.255.0.2"),
           (f"10.{x}.0.0/16", "10.255.0.3"), (f"10.{x}.{y}.0/24", "10.255.0.4"),
           (f"10.{x}.{y}.128/25", "10.255.0.5"), (f"10.{x}.{y}.192/27", "10.255.0.6")]
    pick = r.choice(range(6))
    dest = {0: f"172.{r.randrange(16, 31)}.{r.randrange(256)}.{r.randrange(1, 255)}",
            1: f"10.{(x + 1) % 250 + 1}.{r.randrange(256)}.9",
            2: f"10.{x}.{(y + 7) % 256}.20",
            3: f"10.{x}.{y}.{r.randrange(1, 127)}",
            4: f"10.{x}.{y}.{r.randrange(129, 191)}",
            5: f"10.{x}.{y}.{r.randrange(193, 223)}"}[pick]
    exp = {0: "10.255.0.1", 1: "10.255.0.2", 2: "10.255.0.3", 3: "10.255.0.4", 4: "10.255.0.5", 5: "10.255.0.6"}[pick]
    order = tbl[:]
    r.shuffle(order)
    s = "mkns rt\nip -n rt link add eth0 type dummy\nip -n rt link set eth0 up\nipa rt eth0 10.255.0.10/16\n"
    s += "\n".join(f"ip -n rt route add {p} via {g}" for p, g in order)
    return s, {"answer": exp, "dest": dest}


# --- Module 2: namespaces, cables & switches ------------------------------------

@lesson(module="Hosts, cables & switches", title="Two hosts, one cable",
        topo="""
          [h1]                         [h2]
           (no interfaces yet)          (no interfaces yet)
        """,
        brief="""
          A veth pair is a virtual Ethernet cable: two interfaces, whatever goes in one
          comes out the other. Create it, then move each end into a namespace:

            $ ip link add v1 type veth peer name v2
            $ ip link set v1 netns h1
            $ ip -n h1 link set v1 up
            $ ip -n h1 addr add ... dev v1

          Interfaces moved into a namespace disappear from the root namespace.
        """,
        task="Connect `h1` and `h2` with a veth pair. Give h1 `192.168.1.1/24` and h2 "
             "`192.168.1.2/24`, and make `ip netns exec h1 ping 192.168.1.2` work.",
        hints=["Order: create pair → move ends → bring both up → add addresses → ping.",
               "Don't forget BOTH ends need `up`. Check with `ip -n h1 -br addr`."],
        solution="ip link add v1 type veth peer name v2\nip link set v1 netns h1\nip link set v2 netns h2\n"
                 "ip -n h1 link set v1 up\nip -n h2 link set v2 up\nip -n h1 addr add 192.168.1.1/24 dev v1\n"
                 "ip -n h2 addr add 192.168.1.2/24 dev v2\nip netns exec h1 ping -c2 192.168.1.2",
        explain="""
          This is exactly how Docker and Kubernetes network containers: each container is
          a namespace, connected to the host by a veth pair (usually into a bridge, next).
        """)
def _(r):
    return "mkns h1 h2", {}


@checker(4)
def _(st, ans):
    a1, a2 = addrs("h1"), addrs("h2")
    (ok if "192.168.1.1/24" in a1 else bad)(f"h1 addresses: {a1 or 'none'}")
    (ok if "192.168.1.2/24" in a2 else bad)(f"h2 addresses: {a2 or 'none'}")
    p = ping("h1", "192.168.1.2")
    (ok if p else bad)("h1 → 192.168.1.2 ping " + ("works" if p else "fails"))
    return p and "192.168.1.1/24" in a1


@lesson(module="Hosts, cables & switches", title="ARP: who is out there?", kind="answer",
        topo="""
          [h1] 10.0.0.1/24 ──── [sw: switch] ──── [ ??? ] one unknown host, somewhere in 10.0.0.0/24
        """,
        brief="""
          Before sending an IP packet on a LAN, a host must learn the destination's MAC
          address via ARP ("who has 10.0.0.7? tell 10.0.0.1"). Replies are cached:

            $ ip neigh                     # ARP / neighbour table (REACHABLE, STALE, FAILED...)
            $ ip neigh flush all
            $ arping -I eth0 10.0.0.7      # ARP-level ping

          A quick ping sweep (bash or nmap) populates the table:

            $ for i in $(seq 1 254); do ping -c1 -W1 10.0.0.$i >/dev/null & done; wait
            $ nmap -sn 10.0.0.0/24
        """,
        task="Work only from inside h1 (`ip netns exec h1 bash`). One other host is alive on "
             "10.0.0.0/24. Find it, and submit its MAC address with `lab answer <mac>`.",
        hints=["Sweep the subnet from h1, then read h1's neighbour table.",
               "`ip neigh` entries in state FAILED/INCOMPLETE did not answer; look for REACHABLE."],
        solution="ip netns exec h1 nmap -sn -n 10.0.0.0/24\nip netns exec h1 ip neigh | grep -v FAILED",
        explain="""
          ARP is L2 discovery within ONE broadcast domain. It never crosses a router: for
          off-subnet destinations, the host ARPs for its gateway instead. Gratuitous ARP,
          ARP spoofing and stale ARP caches after IP moves are all common interview topics.
        """)
def _(r):
    last = r.randrange(2, 255)
    m = rmac(r)
    s = f"""mkns h1 sw hidden
wire h1 eth0 sw p1; wire hidden eth0 sw p2; sw sw p1 p2
ipa h1 eth0 10.0.0.1/24
ip -n hidden link set eth0 down; mac hidden eth0 {m}; ip -n hidden link set eth0 up
ipa hidden eth0 10.0.0.{last}/24"""
    return s, {"answer": m}


@lesson(module="Hosts, cables & switches", title="Build a switch",
        topo="""
          [h1] 10.0.0.1 ──eth0~lab-h1──┐
          [h2] 10.0.0.2 ──eth0~lab-h2──┤   root namespace: three loose cable ends
          [h3] 10.0.0.3 ──eth0~lab-h3──┘
        """,
        brief="""
          A Linux bridge is a software L2 switch: it learns MACs per port and forwards
          frames between them.

            $ ip link add br0 type bridge
            $ ip link set br0 up
            $ ip link set PORT master br0      # plug a cable into the switch
            $ bridge link                      # ports and their state
            $ bridge fdb show br br0           # learned MAC table (forwarding database)
        """,
        task="Three hosts are cabled into the root namespace (lab-h1, lab-h2, lab-h3) but not "
             "connected to each other. Create a bridge `br0` in the root namespace so all "
             "three hosts can ping each other.",
        hints=["Every lab-hN interface must be a port (`master br0`) of the bridge.",
               "The bridge itself also has to be UP."],
        solution="ip link add br0 type bridge\nip link set br0 up\n"
                 "for p in lab-h1 lab-h2 lab-h3; do ip link set $p master br0; done\n"
                 "ip netns exec h1 ping -c1 10.0.0.3\nbridge fdb show br br0 | grep -v permanent",
        explain="""
          The bridge needs no IP address: it switches frames by MAC. Look at
          `bridge fdb show br br0` after pinging, that's the switch's learned MAC table,
          same concept as `show mac address-table` on a Cisco/Arista switch.
        """)
def _(r):
    s = "mkns h1 h2 h3\n"
    for i in (1, 2, 3):
        s += f"wire h{i} eth0 root lab-h{i}\nipa h{i} eth0 10.0.0.{i}/24\n"
    return s, {}


@checker(6)
def _(st, ans):
    good = True
    for a, b in [("h1", 2), ("h1", 3), ("h2", 3)]:
        p = ping(a, f"10.0.0.{b}")
        (ok if p else bad)(f"{a} → h{b} ping " + ("works" if p else "fails"))
        good &= p
    return good


# --- Module 3: routing ---------------------------------------------------------

ROUTED = """mkns h1 r1 h2
wire h1 eth0 r1 eth0; wire r1 eth1 h2 eth0
ipa h1 eth0 10.0.1.10/24; ipa r1 eth0 10.0.1.1/24
ipa r1 eth1 10.0.2.1/24;  ipa h2 eth0 10.0.2.10/24
"""
ROUTED_TOPO = """
          [h1] 10.0.1.10 ──── 10.0.1.1 [r1] 10.0.2.1 ──── 10.0.2.10 [h2]
               10.0.1.0/24                   10.0.2.0/24
        """


@lesson(module="Routing", title="Default gateway", topo=ROUTED_TOPO,
        brief="""
          A host only knows its connected subnets. For everything else it needs a route,
          usually a default route pointing at a gateway on a connected subnet.

            $ ip route add default via 10.0.1.1
            $ ip route add 10.9.0.0/16 via 10.0.1.1 dev eth0
            $ ip route del default
        """,
        task="r1 is a working router and h2 is configured, but h1 can't reach h2. "
             "Make `ip netns exec h1 ping 10.0.2.10` work by fixing h1 only.",
        hints=["`ip -n h1 route`: what does h1 do with a packet for 10.0.2.10?",
               "h1 needs a default route via its router's address on h1's subnet."],
        solution="ip -n h1 route\nip -n h1 route add default via 10.0.1.1\nip netns exec h1 ping -c1 10.0.2.10",
        explain="""
          "Network is unreachable" = no route at all. Timeouts with a route present
          usually mean the problem is further along (forwarding, return path, firewall).
        """)
def _(r):
    return ROUTED + "fwd r1\ngw h2 10.0.2.1", {}


@checker(7)
def _(st, ans):
    has = any(x.get("dst") == "default" for x in routes("h1"))
    (ok if has else bad)("h1 has a default route" if has else "h1 has no default route")
    p = ping("h1", "10.0.2.10")
    (ok if p else bad)("h1 → h2 ping " + ("works" if p else "fails"))
    return p


@lesson(module="Routing", title="The router that wouldn't route", topo=ROUTED_TOPO,
        brief="""
          By default a Linux box is a HOST: it drops packets not addressed to itself.
          Forwarding is a per-namespace kernel setting:

            $ sysctl net.ipv4.ip_forward
            $ sysctl -w net.ipv4.ip_forward=1     # (persist in /etc/sysctl.d/ on a real box)

          Troubleshoot hop by hop: can h1 reach its gateway? The far side of the router?
          tcpdump on each interface tells you where the packet dies.
        """,
        task="Both hosts have correct addresses and default routes. h1 can ping r1, "
             "but h1 can't ping h2 (10.0.2.10). Find and fix the problem.",
        hints=["Notice h1 CAN ping 10.0.2.1 (r1's far interface). Why? Linux answers for any "
               "local address on any interface: that ping never needed forwarding.",
               "Check r1's kernel forwarding setting with sysctl."],
        solution="ip netns exec h1 ping -c1 10.0.2.1     # works: local to r1\n"
                 "ip netns exec r1 sysctl net.ipv4.ip_forward\n"
                 "ip netns exec r1 sysctl -w net.ipv4.ip_forward=1",
        explain="""
          "I can ping the router's far interface but not past it" is the fingerprint of
          disabled forwarding (or a FORWARD-chain firewall drop). Good habit: run
          `tcpdump -ni eth1` on r1 while pinging: nothing leaves = r1 is the problem.
        """)
def _(r):
    return ROUTED + "gw h1 10.0.1.1\ngw h2 10.0.2.1", {}


@checker(8)
def _(st, ans):
    f = sysctl("r1", "net.ipv4.ip_forward") == "1"
    (ok if f else bad)("r1 forwards packets" if f else "r1 is not forwarding")
    p = ping("h1", "10.0.2.10")
    (ok if p else bad)("h1 → h2 ping " + ("works" if p else "fails"))
    return p


@lesson(module="Routing", title="Static routes & the return path",
        topo="""
          [h1]──10.0.1.0/24──[r1]──10.0.12.0/24──[r2]──10.0.2.0/24──[h2]
          .10               .1  .1            .2  .1               .10
          h1 default→r1     (no remote routes)    h2 default→r2
        """,
        brief="""
          Routers need a route to every destination AND traffic needs a path BACK.
          One-way routing is the #1 static-routing mistake: the ping gets there, the
          reply dies.

            $ ip -n r1 route add 10.0.2.0/24 via 10.0.12.2
            $ ip -n r1 route get 10.0.2.10      # verify the decision

          In real networks dynamic protocols (OSPF inside a site, BGP between them) install
          these routes automatically.
        """,
        task="Both routers forward but only know their connected subnets. Add static routes "
             "so h1 can ping h2 (10.0.2.10). Don't touch h1/h2.",
        hints=["r1 needs to reach 10.0.2.0/24. Which neighbour address does it send to?",
               "Did the request arrive? Then r2 needs a route back to 10.0.1.0/24."],
        solution="ip -n r1 route add 10.0.2.0/24 via 10.0.12.2\n"
                 "ip -n r2 route add 10.0.1.0/24 via 10.0.12.1\nip netns exec h1 ping -c1 10.0.2.10",
        explain="""
          Every hop makes an independent decision from its own table: no router knows the
          full path. That's why `ip route get` on each hop (or traceroute) is how you
          debug. OSPF floods link state so every router computes paths itself; BGP
          exchanges reachability between autonomous systems with policy.
        """)
def _(r):
    return """mkns h1 r1 r2 h2
wire h1 eth0 r1 eth0; wire r1 eth1 r2 eth0; wire r2 eth1 h2 eth0
ipa h1 eth0 10.0.1.10/24; ipa r1 eth0 10.0.1.1/24; ipa r1 eth1 10.0.12.1/24
ipa r2 eth0 10.0.12.2/24; ipa r2 eth1 10.0.2.1/24; ipa h2 eth0 10.0.2.10/24
fwd r1; fwd r2; gw h1 10.0.1.1; gw h2 10.0.2.1""", {}


@checker(9)
def _(st, ans):
    f = sh("ip -n r1 route get 10.0.2.10")[1]
    good1 = "via 10.0.12.2" in f
    (ok if good1 else bad)("r1 knows how to reach 10.0.2.0/24" if good1 else "r1 has no (correct) route to 10.0.2.0/24")
    b = sh("ip -n r2 route get 10.0.1.10")[1]
    good2 = "via 10.0.12.1" in b
    (ok if good2 else bad)("r2 knows the way back to 10.0.1.0/24" if good2 else "r2 has no (correct) route back to 10.0.1.0/24")
    p = ping("h1", "10.0.2.10")
    (ok if p else bad)("h1 → h2 ping " + ("works" if p else "fails"))
    return p


@lesson(module="Routing", title="Traceroute", kind="answer",
        topo="""
          [h1] ── [r1] ── [r2] ── [r3] ── [h2]
          addresses on the router-to-router links are unknown to you
        """,
        brief="""
          traceroute sends probes with TTL=1,2,3...; each router that decrements TTL to 0
          drops the packet and returns ICMP "time exceeded", revealing itself.

            $ traceroute -n 192.168.5.10        # -n: don't do reverse DNS (faster)
            $ tracepath -n 192.168.5.10         # also discovers path MTU
            $ mtr -n -r -c 5 192.168.5.10       # traceroute + continuous ping stats

          Each hop reports the address of the interface it RECEIVED the probe on (more
          precisely, the source address it uses to reply to you).
        """,
        task="From h1, trace the path to h2 (address shown by `lab show`). What IP address "
             "answers at hop 3? Submit with `lab answer <ip>`.",
        hints=["Run traceroute from inside h1: `ip netns exec h1 traceroute -n <h2>`."],
        solution="ip netns exec h1 traceroute -n -q1 {dest}",
        explain="""
          `* * *` at a hop means that router didn't answer (filtered / rate-limited ICMP),
          not necessarily that traffic is lost there. If later hops answer, the path is fine.
        """)
def _(r):
    a, b, c = r.sample(range(1, 255), 3)
    s = f"""mkns h1 r1 r2 r3 h2
wire h1 eth0 r1 eth0; wire r1 eth1 r2 eth0; wire r2 eth1 r3 eth0; wire r3 eth1 h2 eth0
ipa h1 eth0 10.0.1.10/24; ipa r1 eth0 10.0.1.1/24
ipa r1 eth1 172.16.{a}.1/24; ipa r2 eth0 172.16.{a}.2/24
ipa r2 eth1 172.16.{b}.1/24; ipa r3 eth0 172.16.{b}.2/24
ipa r3 eth1 192.168.{c}.1/24; ipa h2 eth0 192.168.{c}.10/24
fwd r1; fwd r2; fwd r3
gw h1 10.0.1.1; gw r1 172.16.{a}.2; gw r3 172.16.{b}.1; gw h2 192.168.{c}.1
ip -n r2 route add 192.168.{c}.0/24 via 172.16.{b}.2
ip -n r2 route add 10.0.1.0/24 via 172.16.{a}.1"""
    return s, {"answer": f"172.16.{b}.2", "dest": f"192.168.{c}.10"}


# --- Module 4: sockets & services ----------------------------------------------

PAIR = """mkns h1 h2
wire h1 eth0 h2 eth0
ipa h1 eth0 10.0.0.1/24; ipa h2 eth0 10.0.0.2/24
"""
PAIR_TOPO = """
          [h1] 10.0.0.1 ─────────────── 10.0.0.2 [h2]
        """


@lesson(module="Sockets & services", title="Who's listening?", kind="answer",
        topo="""
          [srv] a server running several daemons
        """,
        brief="""
          `ss` (socket statistics) replaced `netstat`. Memorise these flags:

            $ ss -tlnp     # -t TCP  -l listening  -n numeric  -p show process
            $ ss -ulnp     # -u UDP
            $ ss -tanp     # -a all states (LISTEN, ESTAB, TIME-WAIT...)
            $ ss -tn state established '( dport = :443 )'
            $ lsof -i :8080                 # which process has this port open

          0.0.0.0:PORT / *:PORT = listening on all addresses; 127.0.0.1:PORT = local only.
        """,
        task="On host `srv`, a process called `secretd` is listening on a TCP port. "
             "Which port? Submit with `lab answer <port>`.",
        hints=["Run ss inside the namespace: `ip netns exec srv ss -tlnp`.",
               "careful: secretd also has a UDP socket. The question asks for TCP."],
        solution="ip netns exec srv ss -tlnp | grep secretd",
        explain="""
          `ss -tlnp` is the first thing to run when "the service is up but I can't connect":
          is it listening, on which address (127.0.0.1 vs 0.0.0.0), and is it the process
          you think it is?
        """)
def _(r):
    p, q = r.randrange(20000, 60000), r.randrange(20000, 60000)
    s = f"""mkns srv
mkdir -p /run/netlab/bin
for n in secretd webd metricsd cached; do cp "$(command -v socat)" /run/netlab/bin/$n; done
B=/run/netlab/bin
bg srv $B/secretd TCP-LISTEN:{p},reuseaddr,fork SYSTEM:'echo hi'
bg srv $B/secretd -u UDP-RECV:{q} OPEN:/dev/null
bg srv $B/webd TCP-LISTEN:8080,reuseaddr,fork SYSTEM:'echo web'
bg srv $B/metricsd TCP-LISTEN:9100,bind=127.0.0.1,reuseaddr,fork SYSTEM:'echo m'
bg srv $B/cached -u UDP-RECV:11211 OPEN:/dev/null"""
    return s, {"answer": str(p)}


@lesson(module="Sockets & services", title="Talk to a UDP service", topo=PAIR_TOPO,
        brief="""
          netcat is the Swiss-army knife for raw TCP/UDP:

            $ nc 10.0.0.2 80                # TCP client (type, Ctrl-D/Ctrl-C to end)
            $ nc -l 9000                    # TCP server
            $ nc -u 10.0.0.2 7000           # UDP client
            $ echo hi | nc -u -w1 10.0.0.2 7000
            $ nc -zv 10.0.0.2 20-25         # TCP port scan

          UDP is connectionless: no handshake, so nc can't tell whether anyone received it.
        """,
        task="h2 runs a UDP logging service on port 7000. From h1, send it the exact text "
             "`hello netlab`.",
        hints=["Send from inside h1: `ip netns exec h1 ...`.",
               "Pipe with echo and use `-u` (UDP) and `-w1` so nc exits."],
        solution="echo 'hello netlab' | ip netns exec h1 nc -u -w1 10.0.0.2 7000",
        explain="""
          Logging (syslog), DNS, NTP, market data multicast and most streaming protocols are
          UDP: no retransmission, no ordering, but no handshake latency either. Trading
          firms care a lot about that trade-off.
        """)
def _(r):
    return PAIR + "touch /run/netlab/udp.log\nbg h2 socat -u UDP-RECV:7000 OPEN:/run/netlab/udp.log,append", {}


@checker(12)
def _(st, ans):
    try:
        data = open("/run/netlab/udp.log").read()
    except OSError:
        data = ""
    got = "hello netlab" in data
    if data and not got:
        bad(f"server received {data.strip()[-60:]!r}, not 'hello netlab'")
    (ok if got else bad)("h2's service logged 'hello netlab'" if got else "nothing correct received yet")
    return got


@lesson(module="Sockets & services", title="Port scan & banner grab", kind="answer", topo=PAIR_TOPO,
        brief="""
          Finding open TCP ports:

            $ nmap -p 9000-9100 10.0.0.2
            $ nc -zv -w1 10.0.0.2 9000-9100 2>&1 | grep succeeded

          A closed port answers your SYN with RST immediately ("connection refused"); a
          filtered (firewalled) port usually doesn't answer at all (timeout). That
          difference is diagnostic.
        """,
        task="h2 has several services listening between ports 9000 and 9100. One of them "
             "prints a token when you connect. Find it and submit with `lab answer <token>`.",
        hints=["Scan from h1, then connect to each open port with `nc 10.0.0.2 PORT`.",
               "The other services print something else, like 'nothing here'."],
        solution="ip netns exec h1 nmap -n -p 9000-9100 10.0.0.2\n"
                 "for p in $(ip netns exec h1 nmap -n -p 9000-9100 10.0.0.2 | awk -F/ '/open/{print $1}'); do\n"
                 "  echo \"port $p: $(ip netns exec h1 nc -w1 10.0.0.2 $p)\"; done",
        explain="""
          "Connection refused" vs "timed out" tells you whether you reached the host's
          kernel (RST, nothing listening) or something dropped your SYN (firewall, routing).
        """)
def _(r):
    t = token(r)
    ports = r.sample(range(9000, 9101), 3)
    s = PAIR + f"bg h2 socat TCP-LISTEN:{ports[0]},reuseaddr,fork SYSTEM:'echo token={t}'\n"
    for p in ports[1:]:
        s += f"bg h2 socat TCP-LISTEN:{p},reuseaddr,fork SYSTEM:'echo nothing here'\n"
    return s, {"answer": t}


@lesson(module="Sockets & services", title="Inspecting TCP state", kind="answer", topo=PAIR_TOPO,
        brief="""
          Every TCP connection is identified by its 4-tuple:
          (src IP, src port, dst IP, dst port). Clients get an ephemeral source port
          (see `sysctl net.ipv4.ip_local_port_range`).

            $ ss -tn                        # established connections
            $ ss -tni                       # + internals: rtt, cwnd, retransmits
            $ ss -tn state established '( dport = :5000 )'

          States to know: LISTEN, SYN-SENT, SYN-RECV, ESTAB, FIN-WAIT-1/2, CLOSE-WAIT,
          TIME-WAIT, LAST-ACK. Lots of CLOSE-WAIT = the app isn't closing its sockets.
        """,
        task="h1 has an established TCP connection to 10.0.0.2 port 5000 (and some others). "
             "What is h1's local (source) port for that connection? Submit with `lab answer <port>`.",
        hints=["`ip netns exec h1 ss -tn` lists Local Address:Port and Peer Address:Port.",
               "Filter: `ss -tn '( dport = :5000 )'`."],
        solution="ip netns exec h1 ss -tn '( dport = :5000 )'",
        explain="""
          `ss -tni` on a live connection is how you debug slow TCP: rtt, retrans, cwnd and
          send-q / recv-q (bytes queued) show whether the network or the app is the
          bottleneck. Recv-Q building up = the application isn't reading fast enough.
        """)
def _(r):
    p = r.randrange(40000, 60000)
    s = PAIR + """bg h2 socat TCP-LISTEN:5000,reuseaddr,fork SYSTEM:'sleep 1000000'
bg h2 socat TCP-LISTEN:6000,reuseaddr,fork SYSTEM:'sleep 1000000'
sleep 0.4
"""
    s += f"bg h1 socat TCP:10.0.0.2:5000,sourceport={p} SYSTEM:'sleep 1000000'\n"
    s += f"bg h1 socat TCP:10.0.0.2:6000,sourceport={p - 1} SYSTEM:'sleep 1000000'\n"
    s += "bg h1 socat TCP:10.0.0.2:6000 SYSTEM:'sleep 1000000'\n"
    return s, {"answer": str(p)}


# --- Module 5: packet capture ----------------------------------------------------

@lesson(module="Packet capture", title="tcpdump: find the traffic", kind="answer", topo=PAIR_TOPO,
        brief="""
          tcpdump shows the packets actually on the wire. Core usage:

            $ tcpdump -ni eth0                       # -n no DNS, -i interface
            $ tcpdump -ni eth0 -c 10 udp             # stop after 10 packets
            $ tcpdump -ni eth0 'tcp port 80 and host 10.0.0.1'
            $ tcpdump -ni eth0 -w cap.pcap           # save for Wireshark
            $ tcpdump -ni eth0 -e arp                # -e: show MAC addresses

          Filters (BPF): host, net, port, src/dst, tcp/udp/icmp/arp, and/or/not.
        """,
        task="Something on h1 is sending UDP datagrams to h2. Capture on h2 and find the "
             "destination UDP port. Submit with `lab answer <port>`.",
        hints=["`ip netns exec h2 tcpdump -ni eth0 -c 5 udp`",
               "There's also ICMP noise. A `udp` filter removes it."],
        solution="ip netns exec h2 timeout 5 tcpdump -ni eth0 -c 3 udp",
        explain="""
          You also saw (or will with no filter) ICMP "port unreachable" going back:
          nothing listens on that port, so h2's kernel complains. That's how UDP
          traceroute and UDP port scanning work.
        """)
def _(r):
    p = r.randrange(10000, 60000)
    s = PAIR + f"bg h1 bash -c 'while true; do echo beacon | socat -u - UDP-SENDTO:10.0.0.2:{p}; sleep 0.5; done'\n"
    s += "bg h1 ping -i 0.7 10.0.0.2\n"
    return s, {"answer": str(p)}


@lesson(module="Packet capture", title="Sniff the plaintext secret", kind="answer", topo=PAIR_TOPO,
        brief="""
          HTTP (not HTTPS) is plaintext, so anyone on the path can read it:

            $ tcpdump -ni eth0 -A 'tcp port 8080'      # -A: print payload as ASCII
            $ tcpdump -ni eth0 -X 'tcp port 8080'      # -X: hex + ASCII
            $ tcpdump -ni eth0 -A -s0 'tcp port 8080 and tcp[tcpflags] & tcp-push != 0'
        """,
        task="h1 regularly makes HTTP requests to a web server on h2, sending an "
             "`X-Secret` header. Capture it and submit the secret with `lab answer <secret>`.",
        hints=["Capture on either side, e.g. `ip netns exec h2 tcpdump -ni eth0 -A -c 20 'tcp port 8080'`.",
               "Pipe through `grep -i x-secret`. tcpdump needs `-l` (line buffered) when piped."],
        solution="ip netns exec h2 timeout 6 tcpdump -lni eth0 -A -c 30 'tcp port 8080' 2>/dev/null | grep -i x-secret",
        explain="""
          This is why everything sensitive goes over TLS. With TLS, tcpdump still shows the
          handshake (and SNI hostname) but not the content.
        """)
def _(r):
    t = token(r, 12)
    s = PAIR + "mkdir -p /run/netlab/www && echo ok > /run/netlab/www/index.html\n"
    s += "httpd h2 10.0.0.2 8080 /run/netlab/www\n"
    s += f"bg h1 bash -c 'sleep 1; while true; do curl -s -H \"X-Secret: {t}\" http://10.0.0.2:8080/ >/dev/null; sleep 1; done'\n"
    return s, {"answer": t}


# --- Module 6: firewalls & NAT -------------------------------------------------

@lesson(module="Firewalls & NAT", title="Block a port", topo=PAIR_TOPO,
        brief="""
          netfilter has tables (filter, nat, ...) with chains. For the filter table:
          INPUT (to this host), OUTPUT (from it), FORWARD (routed through it).

            $ iptables -L INPUT -n -v --line-numbers
            $ iptables -A INPUT -p tcp --dport 22 -j ACCEPT      # -A append, -I insert at top
            $ iptables -A INPUT -p tcp --dport 80 -j DROP        # silently discard
            $ iptables -A INPUT -p tcp --dport 80 -j REJECT      # discard + tell sender
            $ iptables -D INPUT 3                                # delete rule #3
            $ iptables -P INPUT DROP                             # chain default policy

          (Modern distros run nftables underneath; `nft list ruleset` shows the same rules.)
        """,
        task="h2 runs services on TCP 8080 and TCP 9090. On h2, add a firewall rule so h1 "
             "can no longer connect to port 8080. Port 9090 and ping must keep working.",
        hints=["Put the rule on h2's INPUT chain: `ip netns exec h2 iptables ...`.",
               "Match `-p tcp --dport 8080`."],
        solution="ip netns exec h2 iptables -A INPUT -p tcp --dport 8080 -j DROP\n"
                 "ip netns exec h2 iptables -L INPUT -n -v",
        explain="""
          DROP vs REJECT: DROP makes the client wait for a timeout; REJECT sends a TCP RST
          / ICMP unreachable so it fails fast. Internally REJECT is friendlier; at the
          internet edge DROP reveals less.
        """)
def _(r):
    return PAIR + "bg h2 socat TCP-LISTEN:8080,reuseaddr,fork SYSTEM:'echo a'\n" \
                  "bg h2 socat TCP-LISTEN:9090,reuseaddr,fork SYSTEM:'echo b'", {}


@checker(17)
def _(st, ans):
    a = not tcp_open("h1", "10.0.0.2", 8080)
    (ok if a else bad)("8080 is blocked from h1" if a else "h1 can still connect to 8080")
    b = tcp_open("h1", "10.0.0.2", 9090)
    (ok if b else bad)("9090 still reachable" if b else "9090 is blocked too: too broad")
    c = ping("h1", "10.0.0.2")
    (ok if c else bad)("ping still works" if c else "ping is broken: too broad")
    alive = "8080" in sh("ip netns exec h2 ss -tln")[1]
    if not alive:
        bad("the 8080 service was killed. Block it with a firewall, not by stopping it")
    return a and b and c and alive


@lesson(module="Firewalls & NAT", title="Debug a firewall", topo=PAIR_TOPO,
        brief="""
          iptables evaluates rules top to bottom; the FIRST matching terminal rule
          (ACCEPT/DROP/REJECT) wins. Packet counters show which rule is matching:

            $ iptables -L INPUT -n -v --line-numbers     # watch pkts/bytes change
            $ iptables -S INPUT                          # rules as commands
            $ iptables -D INPUT <num>
            $ iptables -I INPUT <num> <rule>             # insert at position
        """,
        task="h2's firewall has a default-DROP policy. h1 should be able to connect to the "
             "app on h2 port 5000, but can't. Fix it WITHOUT opening anything else: the "
             "policy stays DROP and port 6000 must stay blocked.",
        hints=["List the rules with line numbers and counters, then try connecting and list again.",
               "There IS an ACCEPT for 5000, but something above it matches first."],
        solution="ip netns exec h2 iptables -L INPUT -n -v --line-numbers\n"
                 "ip netns exec h2 iptables -D INPUT 5\n"
                 "ip netns exec h1 nc -zv -w1 10.0.0.2 5000",
        explain="""
          Rule order bugs are the most common firewall mistake. The safe production pattern:
          allow ESTABLISHED,RELATED first (stateful), then specific allows, then default
          deny. Never `iptables -F` with a DROP policy on a remote box: you lock yourself out.
        """)
def _(r):
    s = PAIR + """bg h2 socat TCP-LISTEN:5000,reuseaddr,fork SYSTEM:'echo app'
bg h2 socat TCP-LISTEN:6000,reuseaddr,fork SYSTEM:'echo admin'
I="ip netns exec h2 iptables"
$I -A INPUT -i lo -j ACCEPT
$I -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
$I -A INPUT -p icmp -j ACCEPT
$I -A INPUT -p tcp --dport 22 -j ACCEPT
$I -A INPUT -s 10.0.0.0/24 -p tcp -m comment --comment "temporary lockdown" -j REJECT --reject-with tcp-reset
$I -A INPUT -p tcp --dport 5000 -m comment --comment "app" -j ACCEPT
$I -P INPUT DROP"""
    return s, {}


@checker(18)
def _(st, ans):
    a = tcp_open("h1", "10.0.0.2", 5000)
    (ok if a else bad)("h1 can reach port 5000" if a else "port 5000 still unreachable")
    b = not tcp_open("h1", "10.0.0.2", 6000)
    (ok if b else bad)("port 6000 still blocked" if b else "port 6000 is now open: too permissive")
    pol = "-P INPUT DROP" in sh("ip netns exec h2 iptables -S INPUT")[1]
    (ok if pol else bad)("INPUT policy is still DROP" if pol else "INPUT policy is no longer DROP")
    return a and b and pol


@lesson(module="Firewalls & NAT", title="NAT to the internet",
        topo="""
          [h1] 10.0.1.10 ── 10.0.1.1 [r1] 203.0.113.1 ── 203.0.113.10 [inet]
               private LAN                 "public" link        web server, NO route to 10.0.0.0/8
        """,
        brief="""
          Private addresses (10/8, 172.16/12, 192.168/16) aren't routed on the internet.
          The edge router rewrites the source address to its own public IP (source NAT)
          and tracks connections so replies get translated back.

            $ iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE
            $ iptables -t nat -L -n -v
            $ conntrack -L                    # the NAT/state table
        """,
        task="h1 can't reach the internet host 203.0.113.10: the internet has no route back "
             "to 10.0.1.0/24. Configure r1 so h1 can ping 203.0.113.10 and fetch "
             "http://203.0.113.10/. Don't add routes on `inet`.",
        hints=["On r1, NAT in the `nat` table, POSTROUTING chain, out the public interface.",
               "Run `ip netns exec inet tcpdump -ni eth0 icmp` while pinging: what source IP does inet see?"],
        solution="ip netns exec r1 iptables -t nat -A POSTROUTING -o eth1 -j MASQUERADE\n"
                 "ip netns exec h1 curl -s http://203.0.113.10/\nip netns exec r1 conntrack -L",
        explain="""
          Run `conntrack -L` on r1: each flow shows original and reply tuples, that's how
          replies to 203.0.113.1 get mapped back to 10.0.1.10. MASQUERADE picks the outgoing
          interface's address; SNAT --to-source does the same with a fixed IP; DNAT
          (PREROUTING) is port forwarding the other way.
        """)
def _(r):
    return """mkns h1 r1 inet
wire h1 eth0 r1 eth0; wire r1 eth1 inet eth0
ipa h1 eth0 10.0.1.10/24; ipa r1 eth0 10.0.1.1/24
ipa r1 eth1 203.0.113.1/24; ipa inet eth0 203.0.113.10/24
fwd r1; gw h1 10.0.1.1
mkdir -p /run/netlab/www && echo 'hello from the internet' > /run/netlab/www/index.html
httpd inet 203.0.113.10 80 /run/netlab/www""", {}


@checker(19)
def _(st, ans):
    cheat = sh("ip -n inet route get 10.0.1.10")[0] == 0
    if cheat:
        bad("inet has a route back to 10.0.1.0/24; remove it, the internet won't have one")
    p = ping("h1", "203.0.113.10")
    (ok if p else bad)("h1 → 203.0.113.10 ping " + ("works" if p else "fails"))
    c = "hello from the internet" in sh("ip netns exec h1 curl -s -m 3 http://203.0.113.10/")[1]
    (ok if c else bad)("h1 can fetch the web page" if c else "HTTP to 203.0.113.10 fails")
    return p and c and not cheat


# --- Module 7: troubleshooting -------------------------------------------------

@lesson(module="Troubleshooting", title="The black hole (MTU)", topo=ROUTED_TOPO,
        brief="""
          Symptom: ping works, SSH connects, but large transfers hang. Classic MTU problem:
          some link can't carry full-size packets and drops them without telling anyone.

            $ ping -M do -s 1472 HOST       # 1472 + 8 ICMP + 20 IP = 1500, DF bit set
            $ tracepath -n HOST             # probes path MTU hop by hop
            $ ip link set DEV mtu 1500

          Every interface on one L2 segment must agree on MTU.
        """,
        task="h1 ↔ h2 works for small packets but full-size ones (1500 bytes) vanish. "
             "Find the misconfigured link and fix it so `ping -M do -s 1472 10.0.2.10` works from h1.",
        hints=["Binary-search the size: 1472, 1300, 1400... find the threshold.",
               "Compare the `mtu` of every interface on the path: `ip -n r1 link`, `ip -n h2 link`."],
        solution="ip netns exec h1 ping -c1 -W1 -M do -s 1472 10.0.2.10   # fails\n"
                 "for n in h1 r1 h2; do echo \"== $n\"; ip -n $n -br link; ip -n $n link | grep -o 'mtu [0-9]*'; done\n"
                 "ip -n h2 link set eth0 mtu 1500",
        explain="""
          Packets above the MTU of h2's interface were dropped on receive. Nothing sent an
          ICMP "fragmentation needed", so path-MTU discovery couldn't adapt: a black hole.
          The same happens when a firewall drops ICMP type 3 code 4. Tunnels (GRE,
          VXLAN, IPsec) shrink the effective MTU and are the usual suspects.
        """)
def _(r):
    return ROUTED + "fwd r1; gw h1 10.0.1.1; gw h2 10.0.2.1\nip -n h2 link set eth0 mtu 1380", {}


@checker(20)
def _(st, ans):
    p = ping("h1", "10.0.2.10", "-M do -s 1472")
    (ok if p else bad)("1500-byte packets h1 → h2 " + ("pass" if p else "still vanish"))
    s = ping("h1", "10.0.2.10")
    (ok if s else bad)("normal ping " + ("works" if s else "fails"))
    return p and s


@lesson(module="Troubleshooting", title="Same subnet, can't talk",
        topo="""
          [h1] 10.0.0.10 ──── [sw] ──── 10.0.0.200 [h2]
          the LAN is 10.0.0.0/24, no routers, no default routes
        """,
        brief="""
          Useful method for "A can't reach B" on a LAN:
            1. Does A have the right address/prefix?   ip -br addr
            2. Does ARP resolve?                        ip neigh
            3. Does the request arrive at B?            tcpdump -ni eth0 icmp   (on B)
            4. Does B send a reply? Out which way?      ip route get <A>        (on B)
        """,
        task="h1 can't ping h2 (10.0.0.200) though they're on the same switch. Find the "
             "misconfiguration and fix it.",
        hints=["Run tcpdump on h2 while h1 pings. The requests arrive. Do replies leave?",
               "On h2: `ip route get 10.0.0.10`. Compare h1 and h2's prefix lengths."],
        solution="ip netns exec h2 ip route get 10.0.0.10   # Network is unreachable\nip -n h2 -br addr\n"
                 "ip -n h2 addr del 10.0.0.200/25 dev eth0\nip -n h2 addr add 10.0.0.200/24 dev eth0",
        explain="""
          With /25, h2 believes its LAN is 10.0.0.128-255, so 10.0.0.10 is "remote" and it
          has no gateway: h1's requests arrive and replies never leave. Subnet-mask
          mismatches give one-way symptoms like this. Do the subnet math (`lab subnet` drills it).
        """)
def _(r):
    return """mkns h1 h2 sw
wire h1 eth0 sw p1; wire h2 eth0 sw p2; sw sw p1 p2
ipa h1 eth0 10.0.0.10/24; ipa h2 eth0 10.0.0.200/25""", {}


@checker(21)
def _(st, ans):
    has = any(a.startswith("10.0.0.200/") for a in addrs("h2"))
    if not has:
        bad("h2 should keep the address 10.0.0.200")
    p = ping("h1", "10.0.0.200")
    (ok if p else bad)("h1 → h2 ping " + ("works" if p else "fails"))
    return p and has


@lesson(module="Troubleshooting", title="DNS lookups", kind="answer",
        topo="""
          [h1] 10.0.0.1 ──── [sw] ──── 10.0.0.53 [dns] (dnsmasq, authoritative for lab.internal)
        """,
        brief="""
          dig is the DNS debugging tool:

            $ dig lab.internal                    # uses /etc/resolv.conf
            $ dig @10.0.0.53 www.lab.internal     # ask a specific server
            $ dig +short A www.lab.internal
            $ dig MX / TXT / AAAA / NS name
            $ dig -x 10.0.0.53                    # reverse lookup (PTR)
            $ getent hosts name                   # what applications actually see

          Per-namespace resolver config lives in /etc/netns/<ns>/resolv.conf.
        """,
        task="From h1, find the IPv4 address that `api.lab.internal` ultimately resolves to. "
             "Submit with `lab answer <ip>`.",
        hints=["`ip netns exec h1 dig api.lab.internal`: read the ANSWER section.",
               "api is a CNAME (an alias). Follow the chain to the A record."],
        solution="ip netns exec h1 dig api.lab.internal\nip netns exec h1 dig +short api.lab.internal",
        explain="""
          "It's always DNS": when a name doesn't work but the IP does, check resolv.conf,
          dig @the-server directly, and TTLs/caching. CNAMEs add a lookup per hop.
        """)
def _(r):
    ip = f"10.{r.randrange(1, 250)}.{r.randrange(256)}.{r.randrange(2, 254)}"
    s = f"""mkns h1 dns sw
wire h1 eth0 sw p1; wire dns eth0 sw p2; sw sw p1 p2
ipa h1 eth0 10.0.0.1/24; ipa dns eth0 10.0.0.53/24
echo 'nameserver 10.0.0.53' > /etc/netns/h1/resolv.conf
bg dns dnsmasq -k -C /dev/null --no-resolv --no-hosts --user=root --pid-file \\
   --listen-address=10.0.0.53 --bind-interfaces \\
   --host-record=db.lab.internal,{ip} --host-record=www.lab.internal,10.0.0.80 \\
   --cname=api.lab.internal,db.lab.internal --txt-record=lab.internal,"welcome to netlab\""""
    return s, {"answer": ip}


@lesson(module="Troubleshooting", title="Final boss: outage", topo=ROUTED_TOPO,
        brief="""
          Real outages rarely have one cause. Work methodically, bottom-up:

            L1/L2:  ip -br link       (state UP? LOWER_UP?)
            L3:     ip -br addr, ip route, ip route get, ping hop by hop
            fwd:    sysctl net.ipv4.ip_forward, iptables -L FORWARD -n -v
            L4:     ss -tlnp, nc -zv
            evidence: tcpdump on each interface along the path

          Say what you're testing and why. In an interview that matters as much as the fix.
        """,
        task="Users on h1 report that the web server at http://10.0.2.10/ (h2) is down. "
             "The server process is fine. Restore service. (There's more than one fault.)",
        hints=["Walk the path: h1 → r1 eth0 → r1 eth1 → h2. Check each interface's state.",
               "Once packets reach h2: how does h2 send the reply back? Check its routes.",
               "Still nothing? Look at r1's FORWARD chain policy."],
        solution="ip -n r1 -br link\nip -n r1 link set eth1 up\nip -n h2 route\n"
                 "ip -n h2 route replace default via 10.0.2.1\n"
                 "ip netns exec r1 iptables -L FORWARD -n -v\nip netns exec r1 iptables -P FORWARD ACCEPT\n"
                 "ip netns exec h1 curl -s http://10.0.2.10/",
        explain="""
          Three stacked faults: an admin-down interface, a wrong gateway, and a firewall
          dropping forwarded traffic. Each fix alone looks like it "didn't work". That's why
          you verify each layer independently rather than only retrying the end-to-end test.
        """)
def _(r):
    return ROUTED + """fwd r1; gw h1 10.0.1.1; gw h2 10.0.2.254
ip -n r1 link set eth1 down
ip netns exec r1 iptables -P FORWARD DROP
mkdir -p /run/netlab/www && echo 'service restored' > /run/netlab/www/index.html
httpd h2 10.0.2.10 80 /run/netlab/www""", {}


@checker(23)
def _(st, ans):
    a = ping("h1", "10.0.1.1")
    (ok if a else bad)("h1 → r1 " + ("reachable" if a else "unreachable"))
    p = ping("h1", "10.0.2.10")
    (ok if p else bad)("h1 → h2 ping " + ("works" if p else "fails"))
    c = "service restored" in sh("ip netns exec h1 curl -s -m 3 http://10.0.2.10/")[1]
    (ok if c else bad)("website loads from h1" if c else "website still down")
    return c


# ---------------------------------------------------------------- quiz content

QUIZ = [
    ("A host wants to send to 10.0.5.9 but its own address is 10.0.1.4/24. Whose MAC does it ARP for?",
     ["10.0.5.9's", "its default gateway's", "the broadcast address ff:ff:ff:ff:ff:ff", "nobody: ARP isn't used"], 1,
     "Off-subnet destinations go to the gateway, so you ARP for the gateway's IP. The IP header still says 10.0.5.9."),
    ("Which TCP flags make up the three-way handshake?",
     ["SYN, ACK, FIN", "SYN, SYN-ACK, ACK", "SYN, PSH, ACK", "ACK, SYN-ACK, SYN"], 1, "Client SYN → server SYN+ACK → client ACK."),
    ("Which TCP state does the side that closes FIRST end up in for 2×MSL?",
     ["CLOSE-WAIT", "LAST-ACK", "TIME-WAIT", "FIN-WAIT-2"], 2,
     "The active closer sits in TIME-WAIT to absorb delayed segments. CLOSE-WAIT is the passive side waiting for its app to close()."),
    ("What does a router do to the IP TTL field?",
     ["Nothing", "Decrements it by 1, drops at 0 and sends ICMP time-exceeded", "Resets it to 64", "Increments it"], 1,
     "That behaviour is exactly what traceroute exploits."),
    ("How many usable host addresses in a /27?", ["32", "30", "62", "14"], 1, "2^(32-27) = 32, minus network and broadcast = 30."),
    ("OSPF is best described as:", ["distance-vector", "link-state", "path-vector", "a transport protocol"], 1,
     "Routers flood LSAs, build the same link-state database, and each runs Dijkstra (SPF)."),
    ("BGP is best described as:", ["link-state IGP", "distance-vector IGP", "path-vector EGP", "a layer-2 protocol"], 2,
     "BGP carries the AS_PATH and is policy-driven. Used between autonomous systems (eBGP) and within large networks (iBGP)."),
    ("BGP runs over:", ["UDP 179", "TCP 179", "IP protocol 89", "UDP 520"], 1, "TCP port 179. OSPF is IP protocol 89; RIP is UDP 520."),
    ("In BGP best-path selection (Cisco-style), which is compared FIRST of these?",
     ["MED", "AS_PATH length", "LOCAL_PREF", "router ID"], 2,
     "Weight (Cisco-only) > LOCAL_PREF > locally originated > AS_PATH > origin > MED > eBGP over iBGP > IGP cost > ... > router ID."),
    ("OSPF's metric (cost) is derived from:", ["hop count", "interface bandwidth", "delay and load", "AS_PATH"], 1,
     "Cost = reference bandwidth / interface bandwidth by default."),
    ("Which command shows listening TCP sockets with the owning process?",
     ["ip -s link", "ss -tlnp", "netstat -r", "ip neigh"], 1, "-t tcp, -l listening, -n numeric, -p process."),
    ("A connection attempt returns 'Connection refused' immediately. Most likely:",
     ["a firewall silently DROPs the SYN", "the host is down", "the host is reachable but nothing listens on that port", "DNS failed"], 2,
     "A RST means the target's kernel answered. Silent drops cause timeouts instead."),
    ("Which iptables chain sees packets that a Linux router forwards (not addressed to itself)?",
     ["INPUT", "OUTPUT", "FORWARD", "PREROUTING only"], 2, "nat PREROUTING → routing decision → filter FORWARD → nat POSTROUTING."),
    ("Source NAT for outbound internet traffic is configured in which table/chain?",
     ["filter/INPUT", "nat/PREROUTING", "nat/POSTROUTING", "mangle/OUTPUT"], 2,
     "SNAT/MASQUERADE happen after routing, in POSTROUTING. DNAT (port forwarding) is in PREROUTING."),
    ("`ping -M do -s 1472` succeeds but `-s 1473` fails. The path MTU is:", ["1472", "1473", "1500", "1480"], 2,
     "1472 payload + 8 ICMP header + 20 IP header = 1500."),
    ("Which multicast routing protocol is common in trading networks for market data?",
     ["RIP", "PIM sparse mode (with IGMP on the LAN)", "STP", "VRRP"], 1,
     "Hosts join groups with IGMP; routers build distribution trees with PIM-SM (rendezvous point, then SPT)."),
    ("What's the purpose of the Spanning Tree Protocol?",
     ["route between VLANs", "prevent layer-2 loops", "assign IPs", "encrypt frames"], 1,
     "Ethernet frames have no TTL; a bridging loop would broadcast-storm forever. STP blocks redundant links."),
    ("Longest prefix match: routes 10.0.0.0/8 via A, 10.1.0.0/16 via B, 10.1.2.0/24 via C. Next hop for 10.1.3.4?",
     ["A", "B", "C", "default"], 1, "10.1.3.4 isn't in 10.1.2.0/24; the most specific match is 10.1.0.0/16 → B."),
    ("Which UDP port does DNS use by default (and which transport for large responses/zone transfers)?",
     ["53, falls back to TCP 53", "53 only", "153", "853 always"], 0, "UDP 53, TCP 53 for truncated/large answers and AXFR. 853 is DNS-over-TLS."),
    ("A VLAN is primarily:",
     ["a layer-3 subnet", "a separate layer-2 broadcast domain on shared switches", "an encrypted tunnel", "a routing protocol"], 1,
     "802.1Q tags frames with a 12-bit VLAN ID. Usually one subnet per VLAN, with inter-VLAN routing at L3."),
    ("Which tool would you use to see the kernel's per-connection RTT and retransmits?",
     ["ss -ti", "ip route", "arp -a", "ethtool -i"], 0, "`ss -ti` exposes tcp_info: rtt, rto, cwnd, retrans, delivery rate."),
    ("ARP operates at which layer and is carried in what?",
     ["L3, inside IP", "L2, directly in Ethernet frames (EtherType 0x0806)", "L4, in UDP", "L7"], 1,
     "ARP is its own EtherType, not IP. That's why it can't cross routers."),
    ("ECMP means:", ["one path only", "multiple equal-cost next hops with flows hashed across them",
                     "encrypted multipath", "a BGP attribute"], 1,
     "Usually hashed on the 5-tuple so a single flow stays on one path (no reordering)."),
    ("Why is UDP often preferred over TCP for market data?",
     ["it's encrypted", "no handshake/retransmit/head-of-line blocking; multicast-friendly", "it guarantees ordering", "it's faster on the wire per byte"], 1,
     "Low latency and multicast fan-out; gap detection/recovery is handled by the application protocol."),
]

# ---------------------------------------------------------------- persistence


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1)


def progress():
    return load(PROGRESS, {"done": [], "revealed": [], "current": None, "quiz_best": 0})


def fill(s, st):
    return re.sub(r"\{(\w+)\}", lambda m: str(st.get(m.group(1), m.group(0))), s)


# ---------------------------------------------------------------- commands


def in_netns():
    return sh("ip netns identify")[1].strip()


def require_root_ns():
    n = in_netns()
    if n:
        say(col(f"You're inside namespace '{n}'. Type `exit` to return to the main shell first.", "y"))
        sys.exit(1)


def show(n, st):
    L = LESSONS[n - 1]
    pr = progress()
    mark = col(" ✔ completed", "g") if n in pr["done"] else ""
    header(f"Lab {n}/{len(LESSONS)} · {L['module']} · {L['title']}")
    say(col("  topology", "d"))
    block(fill(L["topo"], st), "    ")
    say()
    block(fill(L["brief"], st))
    say()
    say(col("  ▶ YOUR TASK", "b", "y") + mark)
    for line in textwrap.wrap(fill(L["task"], st), 76):
        say("    " + re.sub(r"`([^`]+)`", lambda m: col(m.group(1), "c"), line))
    if "dest" in st:
        say("    destination: " + col(st["dest"], "b", "c"))
    say()
    act = "lab answer <value>" if L["kind"] == "answer" else "lab check"
    say(col(f"  {act}  ·  lab hint  ·  lab show  ·  lab reset  ·  lab solution", "d"))


def cmd_start(n, quiet=False):
    require_root_ns()
    if not 1 <= n <= len(LESSONS):
        say(f"No lab {n}. Labs are 1–{len(LESSONS)}; see `lab list`."); return
    L = LESSONS[n - 1]
    r = random.Random()
    script, st = L["setup"](r)
    if not quiet:
        say(col(f"  building lab {n}…", "d"))
    run_setup(script)
    st["lesson"], st["hint"] = n, 0
    save(STATE, st)
    pr = progress()
    pr["current"] = n
    save(PROGRESS, pr)
    if not quiet:
        show(n, st)


def current():
    st = load(STATE, None)
    if not st or "lesson" not in st:
        say("No lab running. Try `lab list` then `lab start 1` (or just `lab next`).")
        sys.exit(1)
    return st


def mark_done(n):
    pr = progress()
    if n not in pr["done"]:
        pr["done"].append(n)
    save(PROGRESS, pr)
    L = LESSONS[n - 1]
    say()
    say(col("  ★ Lab complete!", "b", "g"))
    say(col("  why it matters", "d"))
    block(L["explain"], "    ")
    say()
    nxt = next_lesson()
    say(col(f"  → `lab next` for lab {nxt}: {LESSONS[nxt - 1]['title']}" if nxt else
            "  → All labs complete! Keep sharp with `lab quiz` and `lab subnet`.", "c"))


def next_lesson():
    pr = progress()
    cur = pr.get("current") or 0
    for i in list(range(cur + 1, len(LESSONS) + 1)) + list(range(1, cur + 1)):
        if i not in pr["done"]:
            return i
    return None


def cmd_check():
    st = current()
    n = st["lesson"]
    L = LESSONS[n - 1]
    header(f"Checking lab {n}: {L['title']}")
    if L["kind"] == "answer":
        say("  This lab wants an answer: `lab answer <value>`."); return
    if L["check"](st, None):
        mark_done(n)
    else:
        say(col("\n  Not yet. `lab hint` if you're stuck.", "y"))


def norm(s):
    s = s.strip().lower().strip("'\"")
    if re.fullmatch(r"([0-9a-f]{1,2}[:-]){5}[0-9a-f]{1,2}", s):
        s = ":".join(f"{int(x, 16):02x}" for x in re.split("[:-]", s))
    s = re.sub(r"^token=", "", s)
    return s


def cmd_answer(val):
    st = current()
    n = st["lesson"]
    L = LESSONS[n - 1]
    if L["kind"] != "answer":
        say("  This lab is graded on live state; run `lab check`."); return
    if norm(val) == norm(st["answer"]):
        say(col(f"\n  ✔ {val} is correct.", "g"))
        mark_done(n)
    else:
        say(col(f"\n  ✘ {val} isn't it. Try again, or `lab hint`.", "r"))


def cmd_hint():
    st = current()
    L = LESSONS[st["lesson"] - 1]
    i = st.get("hint", 0)
    hints = L["hints"]
    say()
    for k in range(min(i + 1, len(hints))):
        say(col(f"  hint {k + 1}/{len(hints)}: ", "y") + re.sub(r"`([^`]+)`", lambda m: col(m.group(1), "c"), fill(hints[k], st)))
    if i + 1 >= len(hints):
        say(col("  (no more hints; `lab solution` shows a full walkthrough)", "d"))
    st["hint"] = i + 1
    save(STATE, st)


def cmd_solution():
    st = current()
    n = st["lesson"]
    L = LESSONS[n - 1]
    pr = progress()
    if n not in pr["revealed"] and n not in pr["done"]:
        try:
            yn = input(col("  Reveal the solution? Try `lab hint` first. [y/N] ", "y"))
        except EOFError:
            yn = ""
        if yn.strip().lower() != "y":
            return
        pr["revealed"].append(n)
        save(PROGRESS, pr)
    header(f"Solution · lab {n}")
    for line in fill(L["solution"], st).splitlines():
        say("  " + col("$ " + line if not line.startswith(" ") else "  " + line, "c"))
    say()
    act = "Run these, then `lab check`." if L["kind"] == "state" else "Run these to find the answer, then `lab answer`."
    say(col("  " + act, "d"))


def cmd_list():
    pr = progress()
    header(f"netlab · {len(pr['done'])}/{len(LESSONS)} labs complete")
    mod = None
    for i, L in enumerate(LESSONS, 1):
        if L["module"] != mod:
            mod = L["module"]
            say("\n  " + col(mod, "b"))
        tag = col("✔", "g") if i in pr["done"] else (col("▶", "y") if i == pr.get("current") else col("·", "d"))
        peek = col(" (solution viewed)", "d") if i in pr["revealed"] and i not in pr["done"] else ""
        say(f"   {tag} {i:2d}  {L['title']}{peek}")
    say()
    say(col("  lab start N · lab next · lab quiz · lab subnet · lab help", "d"))


def cmd_quiz(count=10):
    r = random.Random()
    qs = r.sample(QUIZ, min(count, len(QUIZ)))
    header(f"Concept quiz · {len(qs)} questions (q to quit)")
    score = 0
    for k, (q, opts, ans, why) in enumerate(qs, 1):
        say()
        say(col(f"  Q{k}. ", "b") + q)
        order = list(range(len(opts)))
        r.shuffle(order)
        for j, o in enumerate(order):
            say(f"     {'abcd'[j]}) {opts[o]}")
        try:
            a = input(col("  > ", "c")).strip().lower()
        except EOFError:
            break
        if a == "q":
            break
        if a in ("a", "b", "c", "d")[:len(opts)] and order["abcd".index(a)] == ans:
            score += 1
            say(col("  ✔ correct. ", "g") + col(why, "d"))
        else:
            right = "abcd"[order.index(ans)]
            say(col(f"  ✘ answer: {right}) {opts[ans]}. ", "r") + why)
    say()
    say(col(f"  Score: {score}/{len(qs)}", "b"))
    pr = progress()
    pr["quiz_best"] = max(pr.get("quiz_best", 0), score)
    save(PROGRESS, pr)


def cmd_subnet(count=5):
    r = random.Random()
    header("Subnetting drill (q to quit)")
    say(col("  Answer each prompt; a blank answer reveals it.", "d"))
    score = total = 0
    for _ in range(count):
        plen = r.choice([19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30])
        ip = ipaddress.ip_address(r.choice([10 << 24, 172 << 24 | 16 << 16, 192 << 24 | 168 << 16]) + r.randrange(1 << 16))
        net = ipaddress.ip_network(f"{ip}/{plen}", strict=False)
        say()
        say(col(f"  {ip}/{plen}", "b", "c"))
        qs = [("network address", str(net.network_address)), ("broadcast address", str(net.broadcast_address)),
              ("subnet mask", str(net.netmask)), ("usable hosts", str(net.num_addresses - 2)),
              ("first usable host", str(net.network_address + 1)), ("last usable host", str(net.broadcast_address - 1))]
        for label, right in r.sample(qs, 3):
            try:
                a = input(f"    {label}? ").strip()
            except EOFError:
                return
            if a == "q":
                say(col(f"\n  Score: {score}/{total}", "b")); return
            total += 1
            if a == right:
                score += 1
                say(col("      ✔", "g"))
            else:
                say(col(f"      ✘ {right}", "r"))
        size = net.num_addresses
        say(col(f"    block size {size}: subnets start at multiples of {size if size < 256 else size // 256} "
                f"in the {'last' if size < 256 else 'third'} octet", "d"))
    say(col(f"\n  Score: {score}/{total}", "b"))


def cmd_help():
    header("netlab help")
    block("""
      Each lab builds a small network out of namespaces. Hosts like h1, r1, h2 are
      network namespaces with their own interfaces, routes and firewall.

        lab list              all labs and your progress
        lab next              start the next unfinished lab
        lab start N           start (or restart) lab N with fresh random values
        lab show              re-print the current task
        lab check             grade a hands-on lab (checks live kernel state)
        lab answer VALUE      submit an answer for a question lab
        lab hint              progressive hints
        lab solution          full walkthrough
        lab reset             rebuild the current lab from scratch
        lab quiz [N]          multiple-choice concepts: ARP, TCP, OSPF, BGP, NAT...
        lab subnet [N]        subnetting drill
        lab progress-reset    forget all progress

      Working inside a host:
        $ ip netns exec h1 ping 10.0.0.2      run one command as h1
        $ ip -n h1 addr                       shorthand for ip commands
        $ ip netns exec h1 bash               open a shell "on" h1 (prompt shows [h1]); exit to leave

      Open more terminals into the same lab: run ./play.sh again from your Mac.
    """)


def selftest():
    """Developer check: every lab fails before its solution and passes after."""
    failures = 0
    for n, L in enumerate(LESSONS, 1):
        cmd_start(n, quiet=True)
        st = load(STATE, {})
        devnull = open(os.devnull, "w")
        old = sys.stdout
        sys.stdout = devnull
        try:
            before = L["check"](st, None) if L["kind"] == "state" else False
        finally:
            sys.stdout = old
        rc, out = sh(fill(L["solution"], st), timeout=30)
        sys.stdout = devnull
        try:
            after = L["check"](st, None) if L["kind"] == "state" else norm(st["answer"]) in out.lower()
        finally:
            sys.stdout = old
        good = (not before) and after
        failures += not good
        print(f"{'PASS' if good else 'FAIL'} {n:2d} {L['title']}  (before={before} after={after})")
        if not good:
            print(textwrap.indent(out[-800:], "      "))
    teardown()
    save(STATE, {})
    print("selftest:", "ALL PASS" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)


def boot():
    """Called from .bashrc on container start: rebuild the lab you were on."""
    pr = progress()
    n = pr.get("current")
    done = len(pr["done"])
    say(col(r"""
   _   _      _   _           _
  | \ | | ___| |_| |    __ _ | |__
  |  \| |/ _ \ __| |   / _` || '_ \
  | |\  |  __/ |_| |__| (_| || |_) |
  |_| \_|\___|\__|_____\__,_||_.__/   Linux networking, hands-on""", "c"))
    say()
    say(f"  Real kernel, real packets: every lab host is a network namespace in this container.")
    say(f"  Progress: {done}/{len(LESSONS)} labs.  Quiz best: {pr.get('quiz_best', 0)}/10")
    say()
    if n:
        cmd_start(n, quiet=True)
        say(col(f"  Resumed lab {n}: {LESSONS[n - 1]['title']} (rebuilt with fresh values). `lab show` to see it.", "y"))
    else:
        say(col("  Start with `lab next` (or `lab list`, `lab help`).", "y"))
    say()


def main(argv):
    if os.geteuid() != 0:
        say("netlab needs root (run it inside the netlab container)."); sys.exit(1)
    cmd, args = (argv[0] if argv else "help"), argv[1:]
    if cmd in ("start", "go") and args and args[0].isdigit():
        cmd_start(int(args[0]))
    elif cmd == "next":
        n = next_lesson()
        cmd_start(n) if n else say("All labs complete! Try `lab quiz` or `lab subnet`.")
    elif cmd in ("show", "task", "status"):
        st = current()
        show(st["lesson"], st)
    elif cmd == "reset":
        cmd_start(current()["lesson"])
    elif cmd == "check":
        cmd_check()
    elif cmd == "answer" and args:
        cmd_answer(" ".join(args))
    elif cmd == "hint":
        cmd_hint()
    elif cmd == "solution":
        cmd_solution()
    elif cmd in ("list", "ls"):
        cmd_list()
    elif cmd == "quiz":
        cmd_quiz(int(args[0]) if args and args[0].isdigit() else 10)
    elif cmd == "subnet":
        cmd_subnet(int(args[0]) if args and args[0].isdigit() else 5)
    elif cmd == "progress-reset":
        save(PROGRESS, {"done": [], "revealed": [], "current": None, "quiz_best": 0})
        say("Progress cleared.")
    elif cmd == "_boot":
        boot()
    elif cmd == "_selftest":
        selftest()
    else:
        cmd_help()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        print()
