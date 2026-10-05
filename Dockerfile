FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive TERM=xterm-256color LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping iputils-tracepath iputils-arping traceroute mtr-tiny \
        tcpdump iptables nftables conntrack ethtool bridge-utils net-tools \
        dnsutils dnsmasq-base netcat-openbsd socat curl nmap iperf3 \
        python3 procps psmisc lsof less vim-tiny nano bash-completion \
    && rm -rf /var/lib/apt/lists/*

COPY lab/lab.py /usr/local/bin/lab
COPY lab/bashrc /root/.bashrc
RUN chmod +x /usr/local/bin/lab && mkdir -p /root/.netlab

WORKDIR /root
CMD ["bash", "-l"]
