#!/bin/bash
# VM startup script: install the benchmark tools, then make the data disk /data:
# the local NVMe SSDs striped (RAID 0), or the pd-balanced disk create.sh attaches
# as pgd-data, ext4 either way. Runs as root on every boot; each step is
# idempotent. Progress is in the serial console and `journalctl -u
# google-startup-scripts`; success writes /data/.startup-done.
set -euo pipefail

# data_disks DIR: the data disk among the block devices DIR (/dev/disk/by-id)
# names. Prints "pd DEVICE" for a pd-balanced disk attached as pgd-data, "nvme
# DEVICE" for one local NVMe SSD, or "raid DEVICE..." for several, to be striped.
# Fails if there are neither, or both.
data_disks() {
  local pd=$1/google-pgd-data devices
  shopt -s nullglob
  devices=("$1"/google-local-nvme-ssd-*)
  shopt -u nullglob
  if [ -e "$pd" ] && [ ${#devices[@]} -gt 0 ]; then
    echo "both a pd-balanced data disk ($pd) and local NVMe SSDs; refusing to choose" >&2
    return 1
  elif [ -e "$pd" ]; then
    echo "pd $pd"
  elif [ ${#devices[@]} -eq 0 ]; then
    echo "no data disk: no local NVMe SSDs and no $pd" >&2
    return 1
  elif [ ${#devices[@]} -eq 1 ]; then
    echo "nvme ${devices[0]}"
  else
    echo "raid ${devices[*]}"
  fi
}

# Sourced (by the tests), it stops here, with only the function defined.
if [ "${BASH_SOURCE[0]:-$0}" != "$0" ]; then return 0; fi

export DEBIAN_FRONTEND=noninteractive
APT=(apt-get -y -q -o DPkg::Lock::Timeout=600)

# Packages first: mdadm is needed to build the array.
if [ ! -x /usr/lib/postgresql/17/bin/postgres ] || ! command -v mdadm >/dev/null; then
  "${APT[@]}" update
  # --no-install-recommends keeps mdadm from pulling in a mail server.
  # time: GNU time, which EXP-04 runs every tool under for its own peak RSS.
  # strace: EXP-11's syscall audit of each arm's flushes.
  "${APT[@]}" install --no-install-recommends curl ca-certificates gnupg mdadm rsync \
    python3 sysstat iproute2 time strace postgresql-common
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  # The benchmark creates its own disposable clusters under /data; no default one.
  install -d /etc/postgresql-common
  if grep -q '^#\?\s*create_main_cluster' /etc/postgresql-common/createcluster.conf 2>/dev/null; then
    sed -i 's/^#\?\s*create_main_cluster.*/create_main_cluster = false/' \
      /etc/postgresql-common/createcluster.conf
  else
    echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
  fi
  "${APT[@]}" update
  # pgbackrest (PGDG's build) for EXP-04's T11; xfsprogs for reformat_dest_xfs.sh (T3 (d)).
  "${APT[@]}" install --no-install-recommends postgresql-17 postgresql-client-17 pgbackrest xfsprogs
fi
# VMs provisioned before GNU time was added to the list above.
if [ ! -x /usr/bin/time ]; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends time
fi
# VMs provisioned before strace was added to the list above (EXP-11).
if ! command -v strace >/dev/null; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends strace
fi
# EXP-12: zstd compresses the load's per-transaction logs before they are fetched; fincore (util-linux)
# records what the source's page cache holds before every window, and where Debian's util-linux lacks
# it, util-linux-extra has it.
if ! command -v zstd >/dev/null; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends zstd
fi
if ! command -v fincore >/dev/null; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends util-linux-extra
fi
# EXP-12's smoke item 4 reads both clocks' offsets with chronyc, and steps them with it. Debian's GCE
# image keeps time with systemd-timesyncd instead, so chrony replaces it, on Google's metadata server.
if ! command -v chronyc >/dev/null; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends chrony
  echo 'server metadata.google.internal prefer iburst' > /etc/chrony/sources.d/gce.sources
  systemctl restart chrony
fi
# EXP-13 (T12): stunnel4 and openssl for v1_tls's proxy pair and its per-session CA; ping for each visit's
# round-trip check (as root, 0.05 s apart).
if ! command -v stunnel4 >/dev/null || ! command -v openssl >/dev/null || ! command -v ping >/dev/null; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends stunnel4 openssl iputils-ping
fi
# EXP-13: sshd also listens on the harness's control port, which the emulated WAN (wan.sh) leaves neither
# delayed nor shaped; port 22 stays as it is, for the Mac, pg_probackup and the SSH probe, all delayed.
# The zone's default-allow-internal firewall rule lets the other VM reach it on the internal address.
CONTROL_PORT=2222
control_conf=/etc/ssh/sshd_config.d/pgd-control-port.conf
if ! grep -qx "Port $CONTROL_PORT" "$control_conf" 2>/dev/null; then
  printf '# EXP-13 (T12): the harness control path, outside the emulated WAN (infra/gcp/wan.sh)\nPort 22\nPort %s\n' \
    "$CONTROL_PORT" > "$control_conf"
  sshd -t
  systemctl reload ssh
fi
# VMs provisioned before pgbackrest and xfsprogs were (PGDG is configured above by then).
if [ ! -x /usr/bin/pgbackrest ] || [ ! -x /usr/sbin/mkfs.xfs ]; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends pgbackrest xfsprogs
fi
# In case a default cluster was created anyway (an older postgresql-common).
if [ -d /etc/postgresql/17/main ]; then pg_dropcluster --stop 17 main || true; fi
systemctl disable --now postgresql >/dev/null 2>&1 || true

if ! mountpoint -q /data; then
  disks=$(data_disks /dev/disk/by-id)
  kind=${disks%% *}
  read -r -a devices <<< "${disks#* }"
  if [ "$kind" = raid ]; then
    # A reboot keeps local SSD data; the array then comes back under its name.
    [ -e /dev/md/pgdata ] || mdadm --assemble --scan >/dev/null 2>&1 || true
    if [ ! -e /dev/md/pgdata ]; then
      mdadm --create /dev/md/pgdata --name=pgdata --level=0 --chunk=256 \
        --raid-devices=${#devices[@]} "${devices[@]}" --run
    fi
    target=/dev/md/pgdata
  else
    # One local SSD, or the pd-balanced disk, which keeps its filesystem across
    # reboots and stops.
    target=${devices[0]}
  fi
  # The same filesystem on either kind. Initialise inode tables and journal now,
  # so no background zeroing runs during measurements.
  blkid "$target" >/dev/null 2>&1 || mkfs.ext4 -q -F -m 0 \
    -E lazy_itable_init=0,lazy_journal_init=0 "$target"
  mkdir -p /data
  mount -o noatime "$target" /data
  chmod 1777 /data
fi
touch /data/.startup-done
echo "pgd startup done: $(date -u +%FT%TZ)"
