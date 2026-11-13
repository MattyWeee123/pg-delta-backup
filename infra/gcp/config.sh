# Settings for the benchmark VMs. Sourced by the other scripts and read by
# experiments/network/remote.py. Every value can be overridden from the
# environment.
#
# VM sets. PAIR names a set of VMs that can exist beside other sets. Its VMs are
# pgd-PAIR-ROLE (pgd-a-source, pgd-a-dest), its SSH host keys and SSH config are
# its own (sets/PAIR/ beside SSH_KEY), and teardown.sh deletes only it. If
# infra/gcp/sets/PAIR.sh exists, it holds the set's settings, written as
# NAME=${NAME:-value} so that the environment still overrides them. With PAIR
# empty or unset, the default set is pgd-source and pgd-dest, as before sets.
#
# Roles. ROLES lists the set's VMs: source and dest, and optionally more, such as
# client (the load's client on its own VM, for T10b and T8). Every role gets the
# same image, labels and startup script. A role takes the set's ZONE, MACHINE,
# DATA_DISK, LOCAL_SSDS and PD_SIZE_GB unless <ROLE>_ZONE, <ROLE>_MACHINE and so
# on say otherwise (DEST_ZONE=us-east1-b, CLIENT_MACHINE=n2-standard-4), and is
# named <ROLE>_VM if that is set. `pgd_role ROLE KEY` prints what a role gets.
PAIR=${PAIR:-}
pgd_name_re='^[a-z]([a-z0-9-]{0,10}[a-z0-9])?$'
if [ -n "$PAIR" ] && { ! [[ $PAIR =~ $pgd_name_re ]] || [ "$PAIR" = default ]; }; then
  echo "config.sh: bad PAIR '$PAIR': 1 to 12 lowercase letters, digits or inner hyphens, starting with a" \
    "letter ('default' is the unnamed set)" >&2
  return 2 2>/dev/null || exit 2
fi
# The set's name in labels, metadata and messages.
SET_NAME=${PAIR:-default}
PGD_SETS_DIR=${PGD_SETS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sets}
SET_FILE=
if [ -n "$PAIR" ] && [ -f "$PGD_SETS_DIR/$PAIR.sh" ]; then
  SET_FILE=$PGD_SETS_DIR/$PAIR.sh
  source "$SET_FILE"
fi

PROJECT=${PROJECT:-postgres-delta-backup}
# us-central1-a, -c and -f had no n2-standard-8 with local SSDs on 2026-10-22
# (stockout); -b had them with 4 SSDs but not with 2.
ZONE=${ZONE:-us-central1-b}
# n2-standard-8: 8 vCPUs, 32 GB RAM, up to 16 Gbps egress.
MACHINE=${MACHINE:-n2-standard-8}
# The data disk at /data, ext4 either way (startup.sh):
#   local-nvme   LOCAL_SSDS local NVMe SSDs (375 GB each), striped (RAID 0) when
#                more than one, so disks are not the bottleneck when the network is
#                throttled. The design asked for 2; 4 were what the zone could
#                supply. Local SSD data is lost when a VM stops, which is fine for
#                disposable benchmark clusters.
#   pd-balanced  one balanced persistent disk of PD_SIZE_GB GB, attached as
#                pgd-data and deleted with its VM. Its throughput grows with its
#                size, so the size has no default.
DATA_DISK=${DATA_DISK:-local-nvme}
LOCAL_SSDS=${LOCAL_SSDS:-4}
PD_SIZE_GB=${PD_SIZE_GB:-}
IMAGE_FAMILY=${IMAGE_FAMILY:-debian-12}
IMAGE_PROJECT=${IMAGE_PROJECT:-debian-cloud}
ROLES=${ROLES:-source dest}
NAME_PREFIX=${NAME_PREFIX:-pgd${PAIR:+-$PAIR}}
SOURCE_VM=${SOURCE_VM:-$NAME_PREFIX-source}
DEST_VM=${DEST_VM:-$NAME_PREFIX-dest}
# The client role, when ROLES has it: an n2-standard-4 in the set's zone, with a
# small balanced persistent disk at /data (no local SSDs, which stock out).
CLIENT_MACHINE=${CLIENT_MACHINE:-n2-standard-4}
CLIENT_DATA_DISK=${CLIENT_DATA_DISK:-pd-balanced}
CLIENT_PD_SIZE_GB=${CLIENT_PD_SIZE_GB:-50}
# Every VM also gets pgd-set=SET_NAME and pgd-role=ROLE. create.sh refuses labels
# without purpose=pg-delta-benchmark.
LABELS=${LABELS:-purpose=pg-delta-benchmark,owner=pg-delta-backup}
GCLOUD=${GCLOUD:-$(command -v gcloud || echo /opt/homebrew/share/google-cloud-sdk/bin/gcloud)}
# Login user created on every VM from instance metadata, and the Mac-side key
# for it. The key is put in instance metadata only (block-project-ssh-keys), so
# the project's metadata is never touched. One key serves every set; the host
# keys (SSH_KNOWN_HOSTS) and remote.py's ssh-config (SSH_CONFIG) are per set.
SSH_USER=${SSH_USER:-bench}
SSH_KEY=${SSH_KEY:-$HOME/.ssh/pgd-bench/id_ed25519}
SSH_SET_DIR=${SSH_SET_DIR:-$(dirname "$SSH_KEY")${PAIR:+/sets/$PAIR}}
SSH_KNOWN_HOSTS=${SSH_KNOWN_HOSTS:-$SSH_SET_DIR/known_hosts}
SSH_CONFIG=${SSH_CONFIG:-$SSH_SET_DIR/ssh_config}

# pgd_role ROLE KEY: ROLE's VM, ZONE, MACHINE, DATA_DISK, LOCAL_SSDS or PD_SIZE_GB.
pgd_role() {
  local var
  var=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')_$2
  if [ -n "${!var:-}" ]; then
    printf '%s\n' "${!var}"
  elif [ "$2" = VM ]; then
    printf '%s\n' "$NAME_PREFIX-$1"
  else
    printf '%s\n' "${!2:-}"
  fi
}

# The roles: each a short lowercase word, source and dest among them, none twice.
pgd_roles_seen=' '
pgd_role_re='^[a-z][a-z0-9]{0,9}$'
for pgd_r in $ROLES; do
  if ! [[ $pgd_r =~ $pgd_role_re ]] || [[ "$pgd_roles_seen" == *" $pgd_r "* ]]; then
    echo "config.sh: bad ROLES '$ROLES': '$pgd_r' is not a short lowercase word, or is there twice" >&2
    return 2 2>/dev/null || exit 2
  fi
  pgd_roles_seen="$pgd_roles_seen$pgd_r "
done
if [[ "$pgd_roles_seen" != *" source "* ]] || [[ "$pgd_roles_seen" != *" dest "* ]]; then
  echo "config.sh: bad ROLES '$ROLES': a set has a source and a dest" >&2
  return 2 2>/dev/null || exit 2
fi
# The set's VM names, in ROLES's order.
VMS=()
for pgd_r in $ROLES; do VMS+=("$(pgd_role "$pgd_r" VM)"); done
unset pgd_r pgd_roles_seen
