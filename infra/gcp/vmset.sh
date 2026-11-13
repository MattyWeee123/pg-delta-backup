# Helpers for create.sh and teardown.sh, sourced after config.sh: gcloud with a
# dry run, which VMs and disks belong to the set, and the checks before creating.
#
# DRY_RUN=1 prints every gcloud command, read-only ones included, as "+ gcloud ..."
# on standard output, and runs none of them. The reads then stand in what the
# script assumes: create.sh that nothing of the set exists yet, teardown.sh that
# each role's VM exists in its zone and no disk is left over. Nothing on the Mac
# changes either: no key is made and no known_hosts is removed.
DRY_RUN=${DRY_RUN:-0}
PURPOSE_LABEL=purpose=pg-delta-benchmark
# The format of every listing the scripts read: name, zone, then the labels that
# say whose it is (and, for disks, the VMs using it).
INSTANCE_FIELDS='csv[no-heading](name,zone.basename(),labels.pgd-set,labels.purpose)'
DISK_FIELDS='csv[no-heading](name,zone.basename(),labels.pgd-set,labels.purpose,users)'
exec 3>&1  # the dry run's lines, even from inside $(...)

# One word as a POSIX shell reads it: bare when it is safe, else in single quotes.
pgd_quote() {
  case $1 in
    '') printf "''" ;;
    *[!A-Za-z0-9_./=:,@%+-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}

pgd_dry() {
  local line=+ word
  for word in "$@"; do line="$line $(pgd_quote "$word")"; done
  printf '%s\n' "$line" >&3
}

# gcloud for this project; with DRY_RUN=1, printed instead.
gc() {
  if [ "$DRY_RUN" = 1 ]; then
    pgd_dry "$GCLOUD" --project "$PROJECT" "$@"
    return 0
  fi
  "$GCLOUD" --project "$PROJECT" "$@"
}

# gc_read ASSUMED ARGS...: a read-only gcloud call's output; with DRY_RUN=1, the
# command printed and ASSUMED in its place.
gc_read() {
  local assumed=$1
  shift
  if [ "$DRY_RUN" = 1 ]; then
    pgd_dry "$GCLOUD" --project "$PROJECT" "$@"
    [ -z "$assumed" ] || printf '%s\n' "$assumed"
    return 0
  fi
  "$GCLOUD" --project "$PROJECT" "$@"
}

# The set's labels for ROLE: LABELS plus pgd-set and pgd-role.
pgd_labels() {
  printf '%s\n' "$LABELS,pgd-set=$SET_NAME,pgd-role=$1"
}

# Every VM and disk name the set may have: each role's VM and its data disk (the
# boot disk has the VM's name), and the names given as arguments.
pgd_disk_names() {
  local name names=' '
  for name in "${VMS[@]}" "$@"; do
    [[ "$names" == *" $name "* ]] || names="$names$name $name-data "
  done
  names=${names# }
  printf '%s' "${names% }"
}

# The server-side filters. They only narrow the listing: which rows belong to the
# set is decided below, on exact names and labels.
pgd_instance_filter() {
  printf 'labels.pgd-set=%s OR name=(%s)' "$SET_NAME" "${VMS[*]}"
}
pgd_disk_filter() {
  printf 'labels.pgd-set=%s OR name=(%s)' "$SET_NAME" "$(pgd_disk_names "$@")"
}

# Rows "name,zone,set,purpose" (stdin) of the VMs that belong to the set, as
# "name,zone": labelled purpose=pg-delta-benchmark and either labelled
# pgd-set=SET_NAME, or labelled with no set and named as one of the set's VMs (a
# VM made before sets had labels). A VM of another set is never the set's.
pgd_owned_instances() {
  local name zone set purpose rest vm
  while IFS=, read -r name zone set purpose rest; do
    [ -n "$name" ] && [ "$purpose" = pg-delta-benchmark ] || continue
    if [ "$set" = "$SET_NAME" ]; then
      printf '%s,%s\n' "$name" "$zone"
    elif [ -z "$set" ]; then
      for vm in "${VMS[@]}"; do
        if [ "$vm" = "$name" ]; then printf '%s,%s\n' "$name" "$zone"; break; fi
      done
    fi
  done
  return 0
}

# pgd_owned_disks "SET_VMS" NAMES...: rows "name,zone,set,purpose,users" (stdin)
# of the disks that belong to the set, as "name,zone,users": labelled
# pgd-set=SET_NAME and purpose=pg-delta-benchmark; or labelled with no set (a boot
# disk carries no labels), named as one of NAMES, and used by no VM outside
# SET_VMS (the set's VMs), so another set's boot disk of the same name is not it.
pgd_owned_disks() {
  local vms=" $1 " name zone set purpose users user mine
  shift
  local wanted=" $* "
  while IFS=, read -r name zone set purpose users; do
    [ -n "$name" ] || continue
    if [ "$set" = "$SET_NAME" ] && [ "$purpose" = pg-delta-benchmark ]; then
      printf '%s,%s,%s\n' "$name" "$zone" "$users"
      continue
    fi
    [ -z "$set" ] && [[ "$wanted" == *" $name "* ]] || continue
    mine=1
    for user in ${users//;/ }; do
      [[ "$vms" == *" ${user##*/} "* ]] || mine=0
    done
    if [ "$mine" = 1 ]; then printf '%s,%s,%s\n' "$name" "$zone" "$users"; fi
  done
  return 0
}

# What a dry run of teardown.sh assumes the listing returns: each role's VM in its zone.
pgd_assumed_instances() {
  local role
  for role in $ROLES; do
    printf '%s,%s,%s,pg-delta-benchmark\n' "$(pgd_role "$role" VM)" "$(pgd_role "$role" ZONE)" "$SET_NAME"
  done
}

pgd_list_instances() {
  gc_read "${1:-}" compute instances list --filter="$(pgd_instance_filter)" --format="$INSTANCE_FIELDS"
}
pgd_list_disks() {
  local assumed=$1
  shift
  gc_read "$assumed" compute disks list --filter="$(pgd_disk_filter "$@")" --format="$DISK_FIELDS"
}

# The set's VMs, one line per role.
pgd_print_set() {
  local role disk
  echo "set $SET_NAME${SET_FILE:+ ($SET_FILE)}, project $PROJECT:"
  for role in $ROLES; do
    disk=$(pgd_role "$role" DATA_DISK)
    case $disk in
      local-nvme) disk="$disk, $(pgd_role "$role" LOCAL_SSDS) SSDs" ;;
      pd-balanced|pd-ssd) disk="$disk, $(pgd_role "$role" PD_SIZE_GB) GB" ;;
    esac
    printf '  %-7s %-22s %-16s %-16s %s\n' "$role" "$(pgd_role "$role" VM)" "$(pgd_role "$role" ZONE)" \
      "$(pgd_role "$role" MACHINE)" "$disk"
  done
}

# What create.sh needs before it makes anything: the purpose label, valid names,
# zones and disks. Exits 2 with every problem found.
pgd_check_create() {
  local role vm zone disk ssds size problems= seen=' '
  local vm_re='^[a-z]([a-z0-9-]{0,56}[a-z0-9])?$' zone_re='^[a-z]+-[a-z]+[0-9]+-[a-z]$'
  case ",$LABELS," in
    *",$PURPOSE_LABEL,"*) ;;
    *) problems="$problems
  LABELS '$LABELS' lacks $PURPOSE_LABEL" ;;
  esac
  for role in $ROLES; do
    vm=$(pgd_role "$role" VM)
    zone=$(pgd_role "$role" ZONE)
    disk=$(pgd_role "$role" DATA_DISK)
    ssds=$(pgd_role "$role" LOCAL_SSDS)
    size=$(pgd_role "$role" PD_SIZE_GB)
    if ! [[ $vm =~ $vm_re ]]; then
      problems="$problems
  $role: bad VM name '$vm' (lowercase letters, digits and hyphens, at most 58)"
    fi
    if [[ "$seen" == *" $vm "* ]]; then
      problems="$problems
  $role: VM name '$vm' is another role's too"
    fi
    seen="$seen$vm "
    if ! [[ $zone =~ $zone_re ]]; then
      problems="$problems
  $role: bad zone '$zone'"
    fi
    case $disk in
      local-nvme)
        [[ $ssds =~ ^[1-9][0-9]*$ ]] || problems="$problems
  $role: local-nvme needs LOCAL_SSDS of 1 or more, not '$ssds'" ;;
      pd-balanced|pd-ssd)
        [[ $size =~ ^[1-9][0-9]*$ ]] || problems="$problems
  $role: $disk needs a size in GB (PD_SIZE_GB, or $(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_PD_SIZE_GB), not '$size'" ;;
      *) problems="$problems
  $role: DATA_DISK '$disk' is none of local-nvme, pd-balanced and pd-ssd" ;;
    esac
  done
  if [ -n "$problems" ]; then
    echo "create.sh: set $SET_NAME can't be created:$problems" >&2
    exit 2
  fi
}

# pgd_quota_check ROLE...: the quota the new VMs of these roles need, against what
# is free now (read-only). Exits 1 before anything is made if one falls short.
# Checked: vCPUs across all regions; per region, the machine family's vCPUs where
# the region lists a quota for it (N2_CPUS), else CPUS; instances; in-use
# addresses (each VM gets an external one); local SSD; and SSD-backed persistent
# disk (pd-balanced, the boot disks included). QUOTA_CHECK=0 skips it. A quota
# that can't be read is reported, and the check skipped.
pgd_quota_check() {
  local role zone region machine family cpus needs= regions= quotas report known=' '
  [ $# -gt 0 ] || return 0
  if [ "${QUOTA_CHECK:-1}" = 0 ]; then
    echo "quota check skipped (QUOTA_CHECK=0)"
    return 0
  fi
  for role in "$@"; do
    zone=$(pgd_role "$role" ZONE)
    region=${zone%-*}
    machine=$(pgd_role "$role" MACHINE)
    family=$(printf '%s' "${machine%%-*}" | tr '[:lower:]' '[:upper:]')
    # Each machine type's vCPUs, read once ("known" holds machine=cpus pairs).
    if [[ "$known" == *" $machine="* ]]; then
      cpus=${known#* "$machine"=}
      cpus=${cpus%% *}
    elif cpus=$(gc_read 0 compute machine-types describe "$machine" --zone "$zone" --format='value(guestCpus)'); then
      known="$known$machine=$cpus "
    else
      echo "quota check skipped: can't read $machine in $zone" >&2
      return 0
    fi
    # C: vCPUs, counted under the family's quota or CPUS; N: everything else.
    needs="$needs
C $region $family $cpus
N project CPUS_ALL_REGIONS $cpus
N $region INSTANCES 1
N $region IN_USE_ADDRESSES 1
N $region SSD_TOTAL_GB 50"
    case $(pgd_role "$role" DATA_DISK) in
      local-nvme) needs="$needs
N $region LOCAL_SSD_TOTAL_GB $(( $(pgd_role "$role" LOCAL_SSDS) * 375 ))" ;;
      pd-balanced|pd-ssd) needs="$needs
N $region SSD_TOTAL_GB $(pgd_role "$role" PD_SIZE_GB)" ;;  # both count toward SSD_TOTAL_GB
    esac
    [[ "$regions " == *" $region "* ]] || regions="$regions $region"
  done
  if ! quotas=$(gc_read '' compute project-info describe --flatten=quotas \
      --format='csv[no-heading](quotas.metric,quotas.limit,quotas.usage)' | sed 's/^/project,/'); then
    echo "quota check skipped: can't read the project's quotas" >&2
    return 0
  fi
  for region in $regions; do
    if ! quotas="$quotas
$(gc_read '' compute regions describe "$region" --flatten=quotas \
      --format='csv[no-heading](quotas.metric,quotas.limit,quotas.usage)' | sed "s/^/$region,/")"; then
      echo "quota check skipped: can't read $region's quotas" >&2
      return 0
    fi
  done
  if [ "$DRY_RUN" = 1 ]; then
    echo "quota check: not computed in a dry run"
    return 0
  fi
  report=$({ printf '%s\n' "$needs" | sed '/^$/d'; printf '%s\n' "$quotas" | sed '/^$/d; s/^/Q /'; } \
    | tr , ' ' | awk '
      $1 == "C" { cpus[$2 " " $3] += $4; next }
      $1 == "N" { need[$2 " " $3] += $4; next }
      $1 == "Q" { limit[$2 " " $3] = $4; usage[$2 " " $3] = $5 }
      END {
        for (pair in cpus) {
          split(pair, part, " ")
          key = part[1] " " part[2] "_CPUS"
          if (!(key in limit)) key = part[1] " CPUS"
          need[key] += cpus[pair]
        }
        for (key in need) {
          if (!(key in limit)) continue
          free = limit[key] - usage[key]
          printf "  %-32s needs %6g, %6g free of %6g%s\n", key, need[key], free, limit[key],
            (need[key] > free ? "  SHORT" : "")
        }
      }' | sort)
  echo "quota (what the new VMs need, what is free now):"
  printf '%s\n' "$report"
  if [[ "$report" == *SHORT* ]]; then
    echo "create.sh: set $SET_NAME doesn't fit the quota; nothing was created (QUOTA_CHECK=0 skips this check)" >&2
    exit 1
  fi
}

# Forget the set's host keys and SSH config: fresh VMs have fresh host keys, and
# may have new addresses. Only this set's files; other sets keep theirs.
pgd_forget_hosts() {
  local file
  for file in "$SSH_KNOWN_HOSTS" "$SSH_CONFIG"; do
    if [ "$DRY_RUN" = 1 ]; then
      echo "dry run: would remove $file"
    else
      rm -f "$file"
    fi
  done
}
