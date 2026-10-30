# Production-equivalent rehearsal host: Ubuntu 24.04 LTS with systemd as PID 1 (a typical VPS image).
# Used only to rehearse deployment/install.sh as root, the system-level units with User=ute and full sandboxing,
# boot start, and isolation from a mock Gold Miner installation. Nothing here talks to eToro.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv python3 python3-venv python3-pip rsync sqlite3 \
      ca-certificates procps dbus curl iproute2 git util-linux nginx openssl apache2-utils \
      sudo openssh-server openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && (cd /lib/systemd/system/sysinit.target.wants && ls | grep -v systemd-tmpfiles-setup | xargs rm -f) \
 && rm -f /lib/systemd/system/multi-user.target.wants/* /etc/systemd/system/*.wants/* \
      /lib/systemd/system/local-fs.target.wants/* /lib/systemd/system/sockets.target.wants/*udev* \
      /lib/systemd/system/sockets.target.wants/*initctl*
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
