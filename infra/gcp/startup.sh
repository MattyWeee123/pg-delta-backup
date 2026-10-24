#!/bin/bash
# VM startup script: install the benchmark tools, then stripe the local SSDs
# into /data. Runs as root on every boot; each step is idempotent. Progress is
# in the serial console and `journalctl -u google-startup-scripts`; success
# writes /data/.startup-done.
set -euo pipefail
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
# VMs provisioned before pgbackrest and xfsprogs were (PGDG is configured above by then).
if [ ! -x /usr/bin/pgbackrest ] || [ ! -x /usr/sbin/mkfs.xfs ]; then
  "${APT[@]}" update
  "${APT[@]}" install --no-install-recommends pgbackrest xfsprogs
fi
# In case a default cluster was created anyway (an older postgresql-common).
if [ -d /etc/postgresql/17/main ]; then pg_dropcluster --stop 17 main || true; fi
systemctl disable --now postgresql >/dev/null 2>&1 || true

if ! mountpoint -q /data; then
  shopt -s nullglob
  devices=(/dev/disk/by-id/google-local-nvme-ssd-*)
  shopt -u nullglob
  if [ ${#devices[@]} -eq 0 ]; then
    echo "no local NVMe SSDs found" >&2
    exit 1
  fi
  if [ ${#devices[@]} -gt 1 ]; then
    # A reboot keeps local SSD data; the array then comes back under its name.
    [ -e /dev/md/pgdata ] || mdadm --assemble --scan >/dev/null 2>&1 || true
    if [ ! -e /dev/md/pgdata ]; then
      mdadm --create /dev/md/pgdata --name=pgdata --level=0 --chunk=256 \
        --raid-devices=${#devices[@]} "${devices[@]}" --run
    fi
    target=/dev/md/pgdata
  else
    target=${devices[0]}
  fi
  # Initialise inode tables and journal now, so no background zeroing runs
  # during measurements.
  blkid "$target" >/dev/null 2>&1 || mkfs.ext4 -q -F -m 0 \
    -E lazy_itable_init=0,lazy_journal_init=0 "$target"
  mkdir -p /data
  mount -o noatime "$target" /data
  chmod 1777 /data
fi
touch /data/.startup-done
echo "pgd startup done: $(date -u +%FT%TZ)"
