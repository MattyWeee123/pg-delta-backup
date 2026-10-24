#!/bin/bash
# Re-create pgd-dest's /data as ext4 exactly as startup.sh first made it, after
# reformat_dest_xfs.sh: EXP-10's last sweep repeats ext4 after XFS, so session drift can be
# told apart from the filesystem. DESTROYS EVERYTHING ON DEST'S /data: fetch results first.
# pgd-source is never touched, and the script refuses to run there.
#   sudo ./reformat_dest_ext4.sh --yes      # on pgd-dest, from ~/pgd/infra/gcp
# It unmounts /data, runs startup.sh's mkfs.ext4 (inode tables and journal initialised up
# front) on the existing md array, mounts it again with startup.sh's options and /data's old
# owner and mode, recreates /data/.startup-done, and prints the filesystem type.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.sh
target=/dev/md/pgdata

if [ "${1:-}" != "--yes" ]; then
  echo "usage: sudo $0 --yes   (destroys everything on /data of $DEST_VM)" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo)" >&2
  exit 2
fi
host=$(hostname -s)
if [ "$host" = "$SOURCE_VM" ]; then
  echo "refusing to run on $SOURCE_VM: only $DEST_VM's /data is reformatted" >&2
  exit 2
fi
if [ "$host" != "$DEST_VM" ]; then
  echo "refusing to run on $host: this script is for $DEST_VM only" >&2
  exit 2
fi
if [ ! -e "$target" ]; then
  echo "no md array at $target (startup.sh builds it from the local SSDs)" >&2
  exit 2
fi
if ! mountpoint -q /data; then
  echo "/data is not mounted; run startup.sh first" >&2
  exit 2
fi
mounted=$(findmnt -n -o SOURCE /data)
if [ "$(readlink -f "$mounted")" != "$(readlink -f "$target")" ]; then
  echo "/data is $mounted, not the array $target; refusing" >&2
  exit 2
fi
if [ -e /data/exp04/.exp04.lock ] && ! flock -n /data/exp04/.exp04.lock true; then
  echo "an EXP-04 run holds /data/exp04/.exp04.lock; stop it first" >&2
  exit 2
fi

owner=$(stat -c '%u:%g' /data)
mode=$(stat -c '%a' /data)
echo "before: $(findmnt -n -o SOURCE,FSTYPE,OPTIONS /data), owner $owner, mode $mode"
umount /data
wipefs -q -a "$target"
mkfs.ext4 -q -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0 "$target"
mount -o noatime "$target" /data
chown "$owner" /data
chmod "$mode" /data
touch /data/.startup-done
echo "after: $(findmnt -n -o SOURCE,FSTYPE,OPTIONS /data), owner $(stat -c '%u:%g' /data), mode $(stat -c '%a' /data)"
[ "$(findmnt -n -o FSTYPE /data)" = ext4 ]
