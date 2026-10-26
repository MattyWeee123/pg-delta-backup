#!/bin/bash
# EXP-44 (OMNI-2, lean), run from the Mac (docs/experiments/EXP-44-omni-gke.md): AlloyDB Omni's Kubernetes operator
# on GKE. (a) After an unplanned failover (the primary's node VM deleted), does the old primary's disk survive, and
# what seeds the new standby? (b) Does a Sidecar run on standbys too, read PGDATA, and get a cgroup CPU quota?
# Plus reads: Omni 18's settings on Kubernetes, the node's TCP and dirty-page sysctls, the seeding's pg_basebackup.
# K4 allows GKE; the owner accepted Omni's license terms for this research use on October 26, 2026.
#     bash experiments/omni/omni_gke.sh OUT_DIR setup|inventory|sidecar|failover|teardown
# Phases run in that order; each appends JSON lines to OUT_DIR/steps.jsonl and keeps raw kubectl JSON in OUT_DIR.
set -u
OUT=${1:?usage: omni_gke.sh OUT_DIR PHASE}
PHASE=${2:?usage: omni_gke.sh OUT_DIR PHASE}
mkdir -p "$OUT"
export PATH="$PATH:/opt/homebrew/share/google-cloud-sdk/bin:/usr/sbin"
export KUBECONFIG=$OUT/kubeconfig  # its own: the user's ~/.kube/config is never touched
PROJECT=postgres-delta-backup
ZONE=${ZONE:-us-central1-a}
CLUSTER=pgd-omni2
NS=omni
DB=omni2
OPERATOR_VERSION=1.9.0
CERT_MANAGER_VERSION=v1.16.3
DB_VERSION=${DB_VERSION:-18.4.0}
k() { kubectl -n $NS "$@"; }
step() { python3 -c "import json,sys; print(json.dumps({'step': sys.argv[1], **json.loads(sys.argv[2])}))" "$1" "$2" | tee -a "$OUT/steps.jsonl"; }
db_pods() { k get pods -o json | python3 -c "
import json, sys
for p in json.load(sys.stdin)['items']:
    names = [c['name'] for c in p['spec']['containers']]
    if any('alloydbomni' in c['image'] or c['name'] == 'database' for c in p['spec']['containers']):
        print(p['metadata']['name'], p['spec'].get('nodeName', ''), p['status'].get('phase'), ','.join(names))"; }
db_container() { k get pod "$1" -o json | python3 -c "
import json, sys
p = json.load(sys.stdin)
print(next((c['name'] for c in p['spec']['containers'] if c['name'] == 'database'),
           next(c['name'] for c in p['spec']['containers'] if 'alloydbomni' in c['image'])))"; }
pw() { k get secret db-pw-$DB -o "jsonpath={.data.$DB}" | base64 -d; }
sql() {  # POD SQL: psql inside the pod's database container, over TCP with the admin password
  k exec "$1" -c "$(db_container "$1")" -- env PGPASSWORD="$(pw)" psql -h localhost -U postgres -d postgres -AtXqc "$2"; }
snapshot() {  # NAME: raw JSON of what decides the disks' fate, and a one-line summary
  for kind in pods pvc; do k get $kind -o json > "$OUT/$1-$kind.json"; done
  kubectl get pv -o json > "$OUT/$1-pv.json"; kubectl get nodes -o json > "$OUT/$1-nodes.json"
  kubectl get storageclass -o json > "$OUT/$1-storageclass.json"
  k get dbcluster $DB -o json > "$OUT/$1-dbcluster.json" 2> /dev/null
  gcloud compute disks list --project $PROJECT --filter="zone:($ZONE)" --format=json > "$OUT/$1-disks.json" 2> /dev/null
  python3 - "$OUT" "$1" <<'PY'
import json, sys, os
out, name = sys.argv[1], sys.argv[2]
load = lambda kind: json.load(open(os.path.join(out, f'{name}-{kind}.json')))
pods = [{'pod': p['metadata']['name'], 'node': p['spec'].get('nodeName'), 'phase': p['status'].get('phase'),
         'containers': [c['name'] for c in p['spec']['containers']],
         'claims': [v['persistentVolumeClaim']['claimName'] for v in p['spec'].get('volumes', []) if 'persistentVolumeClaim' in v]}
        for p in load('pods')['items']]
pvcs = [{'pvc': c['metadata']['name'], 'uid': c['metadata']['uid'], 'volume': c['spec'].get('volumeName'),
         'phase': c['status'].get('phase'), 'created': c['metadata']['creationTimestamp'],
         'owners': [o['kind'] + '/' + o['name'] for o in c['metadata'].get('ownerReferences', [])],
         'deleting': 'deletionTimestamp' in c['metadata']} for c in load('pvc')['items']]
pvs = [{'pv': v['metadata']['name'], 'reclaim': v['spec'].get('persistentVolumeReclaimPolicy'), 'phase': v['status'].get('phase'),
        'claim': (v['spec'].get('claimRef') or {}).get('name'),
        'disk': (v['spec'].get('csi') or {}).get('volumeHandle', '').split('/')[-1]} for v in load('pv')['items']]
classes = [{'class': s['metadata']['name'], 'reclaim': s.get('reclaimPolicy'),
            'default': s['metadata'].get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true'}
           for s in load('storageclass')['items']]
disks = [{'disk': d['name'], 'users': len(d.get('users', [])), 'size_gb': d.get('sizeGb')}
         for d in load('disks') if d['name'].startswith('pvc-')]
status = {}
try:
    status = load('dbcluster').get('status', {})
except Exception:
    pass
print(json.dumps({'snapshot': name, 'pods': pods, 'pvcs': pvcs, 'pvs': pvs, 'storage_classes': classes,
                  'pvc_disks': disks, 'dbcluster_status': {k: status[k] for k in list(status)[:12]}}))
PY
}

case $PHASE in
setup)
  gcloud services enable container.googleapis.com --project $PROJECT
  # One zone, 3 nodes of 4 vCPUs (12 of the 32-vCPU quota); standard boot disks keep the SSD quota for the PVCs.
  gcloud container clusters create $CLUSTER --project $PROJECT --zone $ZONE --num-nodes 3 \
    --machine-type e2-standard-4 --disk-type pd-standard --disk-size 30 \
    --labels purpose=pg-delta-benchmark,pgd-set=omni2 --quiet
  gcloud container clusters get-credentials $CLUSTER --project $PROJECT --zone $ZONE
  helm repo add jetstack https://charts.jetstack.io --force-update > /dev/null
  helm install cert-manager jetstack/cert-manager --namespace cert-manager --create-namespace \
    --version $CERT_MANAGER_VERSION --set crds.enabled=true --wait --timeout 10m
  helm install alloydbomni-operator oci://gcr.io/alloydb-omni/alloydbomni-operator --version $OPERATOR_VERSION \
    --create-namespace --namespace alloydb-omni-system --wait --timeout 10m
  kubectl create namespace $NS
  password=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)  # a test password, for this cluster only
  k create secret generic db-pw-$DB --from-literal=$DB="$password"
  cat <<EOF | k apply -f -
apiVersion: alloydbomni.dbadmin.goog/v1
kind: DBCluster
metadata:
  name: $DB
spec:
  databaseVersion: "$DB_VERSION"
  availability:
    numberOfStandbys: 1
  primarySpec:
    adminUser:
      passwordRef:
        name: db-pw-$DB
    resources:
      cpu: 2
      memory: 8Gi
      disks:
      - name: DataDisk
        size: 10Gi
EOF
  for i in $(seq 120); do [ "$(db_pods | awk '$3 == "Running"' | wc -l)" -ge 2 ] && break; sleep 15; done
  db_pods
  step setup "{\"versions\": {\"operator\": \"$OPERATOR_VERSION\", \"cert_manager\": \"$CERT_MANAGER_VERSION\", \"database\": \"$DB_VERSION\"}, \"kubernetes\": \"$(kubectl version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')\"}"
  ;;
inventory)
  step inventory "$(snapshot inventory)"
  for pod in $(db_pods | awk '{print $1}'); do
    role=$(sql $pod 'select case when pg_is_in_recovery() then $$standby$$ else $$primary$$ end')
    settings=$(sql $pod "select json_object_agg(name, setting) from pg_settings where name in ('server_version','data_checksums','wal_log_hints','summarize_wal','full_page_writes','data_directory','shared_preload_libraries','synchronous_standby_names','wal_level','ssl')")
    step instance "{\"pod\": \"$pod\", \"role\": \"$role\", \"settings\": $settings}"
  done
  primary=$(for pod in $(db_pods | awk '{print $1}'); do [ "$(sql $pod 'select pg_is_in_recovery()')" = f ] && echo $pod; done)
  sql $primary "create table if not exists marker (id int primary key, note text); insert into marker values (1, 'before the failover') on conflict do nothing"
  sql $primary "select json_agg(row_to_json(r)) from (select application_name, state, sync_state, client_addr::text from pg_stat_replication) r" > "$OUT/replication.json"
  k logs $(db_pods | awk -v p=$primary '$1 != p {print $1; exit}') -c database > "$OUT/standby-seed.log" 2>&1
  kubectl -n alloydb-omni-system logs -l control-plane=controller-manager --all-containers --tail=-1 > "$OUT/operator-setup.log" 2>&1
  step seeding "{\"replication\": $(cat $OUT/replication.json), \"standby_log_basebackup_lines\": $(grep -i 'basebackup' $OUT/standby-seed.log | head -20 | python3 -c 'import json,sys; print(json.dumps([l.rstrip()[:300] for l in sys.stdin]))'), \"operator_log_basebackup_lines\": $(grep -i 'basebackup' $OUT/operator-setup.log | head -20 | python3 -c 'import json,sys; print(json.dumps([l.rstrip()[:300] for l in sys.stdin]))')}"
  ;;
sidecar)
  primary=$(for pod in $(db_pods | awk '{print $1}'); do [ "$(sql $pod 'select pg_is_in_recovery()')" = f ] && echo $pod; done)
  # The data disk's volume and mount path, read from the database container itself.
  read volume mount < <(k get pod $primary -o json | python3 -c "
import json, sys
p = json.load(sys.stdin)
db = next(c for c in p['spec']['containers'] if c['name'] == 'database' or 'alloydbomni' in c['image'])
claims = {v['name'] for v in p['spec']['volumes'] if 'persistentVolumeClaim' in v}
m = next(m for m in db['volumeMounts'] if m['name'] in claims and 'data' in m['name'].lower())
print(m['name'], m['mountPath'])")
  step data_volume "{\"volume\": \"$volume\", \"mount_path_in_database_container\": \"$mount\"}"
  cat <<EOF | k apply -f -
apiVersion: alloydbomni.dbadmin.goog/v1
kind: Sidecar
metadata:
  name: probe
spec:
  sidecars:
  - name: probe
    image: debian:12-slim
    command: ["sleep", "infinity"]
    resources:
      limits:
        cpu: 500m
        memory: 256Mi
    volumeMounts:
    - name: $volume
      mountPath: /pgdata
      readOnly: true
EOF
  k patch dbclusters.alloydbomni.dbadmin.goog $DB --type=merge -p '{"spec":{"primarySpec":{"sidecarRef":{"name":"probe"}}}}'
  for i in $(seq 80); do [ "$(db_pods | awk '$3 == "Running" && $4 ~ /probe/' | wc -l)" -ge 2 ] && break; sleep 15; done
  db_pods > "$OUT/pods-with-sidecar.txt"
  for pod in $(db_pods | awk '{print $1}'); do
    has=$(k get pod $pod -o jsonpath='{.spec.containers[*].name}' | grep -qw probe && echo true || echo false)
    if [ $has = true ]; then
      reads=$(k exec $pod -c probe -- sh -c 'printf "{\"uid\": \"%s\", \"cpu_max\": \"%s\", \"pg_version_files\": \"%s\", \"read_pg_version\": \"%s\", \"write_refused\": %s, \"vm_dirty\": \"%s\", \"tcp\": \"%s\"}" "$(id -u)" "$(cat /sys/fs/cgroup/cpu.max 2>&1)" "$(find /pgdata -maxdepth 4 -name PG_VERSION 2>&1 | head -3 | tr "\n" " ")" "$(cat $(find /pgdata -maxdepth 4 -name PG_VERSION 2>/dev/null | head -1) 2>&1 | head -1)" "$(touch /pgdata/.probe 2>/dev/null && echo false || echo true)" "$(for f in dirty_ratio dirty_background_ratio dirty_bytes dirty_background_bytes dirty_expire_centisecs dirty_writeback_centisecs; do printf "%s=%s " $f $(cat /proc/sys/vm/$f); done)" "$(for f in tcp_congestion_control tcp_rmem tcp_wmem; do printf "%s=%s " $f "$(cat /proc/sys/net/ipv4/$f | tr "\t" " ")"; done)"')
    else
      reads=null
    fi
    step sidecar_on "{\"pod\": \"$pod\", \"role\": \"$(sql $pod 'select case when pg_is_in_recovery() then $$standby$$ else $$primary$$ end')\", \"has_sidecar\": $has, \"reads\": $reads}"
  done
  ;;
failover)
  primary=$(for pod in $(db_pods | awk '{print $1}'); do [ "$(sql $pod 'select pg_is_in_recovery()')" = f ] && echo $pod; done)
  node=$(k get pod $primary -o jsonpath='{.spec.nodeName}')
  step before_failover "$(snapshot before)"
  step failover_start "{\"primary\": \"$primary\", \"node\": \"$node\", \"at\": $(date +%s)}"
  # An unplanned failover: the primary's node VM is deleted outright (GKE's instance group makes a new one).
  gcloud compute instances delete $node --project $PROJECT --zone $ZONE --quiet > "$OUT/node-delete.log" 2>&1 &
  t0=$(date +%s)
  for i in $(seq 100); do  # up to 25 minutes, a line every 15 s
    sleep 15
    line=$(db_pods | tr '\n' ';')
    newp=$(for pod in $(db_pods | awk '$3 == "Running" {print $1}'); do [ "$(sql $pod 'select pg_is_in_recovery()' 2> /dev/null)" = f ] && echo $pod; done | head -1)
    pvcs=$(k get pvc -o jsonpath='{range .items[*]}{.metadata.name}:{.status.phase}{" "}{end}')
    echo "{\"t\": $(( $(date +%s) - t0 )), \"pods\": \"$line\", \"primary\": \"$newp\", \"pvcs\": \"$pvcs\"}" >> "$OUT/failover-timeline.jsonl"
    [ -n "$newp" ] && [ "$newp" != "$primary" ] && [ "$(db_pods | awk '$3 == "Running"' | wc -l)" -ge 2 ] && \
      [ "$(sql $(db_pods | awk -v p=$newp '$1 != p && $3 == "Running" {print $1; exit}') 'select pg_is_in_recovery()' 2> /dev/null)" = t ] && break
  done
  step failover_end "{\"seconds\": $(( $(date +%s) - t0 )), \"new_primary\": \"$newp\", \"marker\": \"$(sql $newp 'select note from marker where id = 1' 2>&1)\"}"
  step after_failover "$(snapshot after)"
  standby=$(db_pods | awk -v p=$newp '$1 != p {print $1; exit}')
  k logs $standby -c database > "$OUT/new-standby.log" 2>&1
  kubectl -n alloydb-omni-system logs -l control-plane=controller-manager --all-containers --tail=-1 > "$OUT/operator-failover.log" 2>&1
  step reseed "{\"new_standby\": \"$standby\", \"standby_log_basebackup_lines\": $(grep -i 'basebackup' $OUT/new-standby.log | head -20 | python3 -c 'import json,sys; print(json.dumps([l.rstrip()[:300] for l in sys.stdin]))'), \"operator_log_lines\": $(grep -iE 'basebackup|pvc|persistentvolumeclaim|delete' $OUT/operator-failover.log | tail -30 | python3 -c 'import json,sys; print(json.dumps([l.rstrip()[:300] for l in sys.stdin]))')}"
  for pod in $newp $standby; do
    step sidecar_after_failover "{\"pod\": \"$pod\", \"has_sidecar\": $(k get pod $pod -o jsonpath='{.spec.containers[*].name}' | grep -qw probe && echo true || echo false)}"
  done
  ;;
teardown)
  snapshot final > "$OUT/final-summary.json"
  k delete dbcluster $DB --wait=true --timeout=10m > /dev/null 2>&1
  k delete pvc --all --wait=true --timeout=10m > /dev/null 2>&1
  gcloud container clusters delete $CLUSTER --project $PROJECT --zone $ZONE --quiet
  # Disks a deleted claim left behind (a Retain policy, or claims deleted with the cluster) are deleted by name.
  python3 -c "
import json
pvs = json.load(open('$OUT/final-pv.json'))['items']
print(' '.join(v['spec']['csi']['volumeHandle'].split('/')[-1] for v in pvs if v['spec'].get('csi')))" > "$OUT/pv-disks.txt"
  for d in $(cat "$OUT/pv-disks.txt") $(gcloud compute disks list --project $PROJECT --filter="zone:($ZONE) AND name~^pvc- AND -users:*" --format='value(name)'); do
    gcloud compute disks delete $d --project $PROJECT --zone $ZONE --quiet 2> /dev/null
  done
  step teardown "{\"vms_left\": $(gcloud compute instances list --project $PROJECT --format='value(name)' | grep -c pgd-omni2 || true), \"pvc_disks_left\": $(gcloud compute disks list --project $PROJECT --filter='name~^pvc-' --format='value(name)' | wc -l)}"
  ;;
*) echo "unknown phase $PHASE" >&2; exit 2 ;;
esac
