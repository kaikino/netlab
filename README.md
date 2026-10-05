# netlab: Linux networking playground

Hands-on labs with real commands on a real Linux kernel. Each lab builds a small network
inside one Docker container: hosts, routers and switches are network namespaces, cables are veth
pairs. `lab check` grades you by inspecting the live kernel state.

```bash
./play.sh            # start (run it again in another terminal for a second shell)
```

Inside the container:

```
lab next          start the next lab        lab hint       progressive hints
lab list          all labs + progress       lab solution   walkthrough
lab check         grade a hands-on lab      lab reset      rebuild current lab
lab answer X      answer a question lab     lab quiz       ARP/TCP/OSPF/BGP/NAT concepts
lab show          re-print the task         lab subnet     subnetting drill
```

Hosts: `ip netns exec h1 <cmd>`, `ip -n h1 addr`, or `ip netns exec h1 bash` (the prompt shows `[h1]`).

## Labs (23)
1. **Interfaces & addressing**: bring an interface up, interface forensics (MTU/MAC), longest prefix match
2. **Hosts, cables & switches**: veth pairs, ARP discovery, building a bridge
3. **Routing**: default gateway, IP forwarding, static routes + return path, traceroute
4. **Sockets & services**: `ss`, netcat UDP, port scan + banner grab, TCP connection state
5. **Packet capture**: tcpdump filters, sniffing plaintext HTTP
6. **Firewalls & NAT**: iptables block, rule-order debugging, MASQUERADE
7. **Troubleshooting**: MTU black hole, subnet-mask mismatch, DNS/CNAME, multi-fault outage

Values (ports, MACs, addresses, tokens) are randomised each time a lab starts. Progress is kept
in the `netlab-data` Docker volume (`./play.sh --reset` wipes it).
Maintainers: `docker run --rm --privileged netlab lab _selftest` builds every lab, confirms it
fails, applies the solution and confirms it passes.
