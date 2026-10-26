# EXP-44: On Kubernetes, does Omni leave an old copy to refresh, and can pgdelta run beside it?

Status: **pre-registered October 26, 2026, before any run.** OMNI-2, lean, allowed by K4 (GKE), on the owner's go-ahead the same day. About an hour, mostly waiting for the cluster. The owner accepted Omni's license terms for this research use on October 26.

## Question

pgdelta's HA pitch assumes something the Omni notes mark "not documented": that a rebuilt standby's old disk still exists. Its deployment on Omni assumes a sidecar can read the data directory on the instance it refreshes. On the AlloyDB Omni operator for Kubernetes:
- **(a)** After an unplanned failover, does the old primary's disk survive (its PersistentVolumeClaim, its volume, its persistent disk)? Is it reused, and how is the new standby seeded?
- **(b)** Does a Sidecar attached to the cluster run on the standby too, and on a promoted primary? Can it read PGDATA, mounted read-only? Is its CPU limit a cgroup quota, as T10b's source cap assumes?

## Design

`experiments/omni/omni_gke.sh`, from the Mac, in phases.
1. **The cluster:** a zonal GKE cluster, 3 `e2-standard-4` nodes (12 vCPUs), labelled `purpose=pg-delta-benchmark`.
   - cert-manager v1.16.3, then the Omni operator 1.9.0 (Helm, Google's chart registry).
   - A `DBCluster` on Omni 18.4.0 with one standby: 2 CPUs, 8 GiB and a 10 GiB data disk per instance.
2. **Inventory:** pods, nodes, claims, volumes, storage classes and their reclaim policies, and the persistent disks. Each instance's settings (checksums, `wal_log_hints`, `summarize_wal`, TLS). Whatever the standby's and the operator's logs show of its seeding (`pg_basebackup` and its flags). A marker row on the primary.
3. **(b) The sidecar:**
   - the data disk's volume name is read from the database container;
   - a `Sidecar` (Debian 12, `sleep`, CPU limit 500m) mounts it read-only and is attached through `primarySpec.sidecarRef`;
   - on each instance pod: is the sidecar there, can it find and read `PG_VERSION`, is a write refused, what is `cpu.max`; and, through it, the node's `vm.dirty_*` and TCP settings.
4. **(a) The failover:**
   - the primary's node VM is deleted outright;
   - every 15 s for up to 25 minutes: the pods, the primary, the claims;
   - then the same inventory again, the marker read on the new primary, the new standby's and the operator's logs, and whether the sidecar is on both instances.
5. **Teardown:** the cluster, its claims and any disk left behind, then a check that nothing of it remains.
6. **The analysis,** committed before the data: `analysis/exp44.py`.

**Deviations from FIRMNESS.md's OMNI-2, decided now:**
- one zone, not three, and `e2-standard-4`, not `n2-standard-4` (us-central1 is short of small N2 VMs today);
- one failover, not three;
- TDE (c) and a blocked B (f) not run.

What the operator does with a disk shouldn't depend on zones or machine types. A second failover runs only if the first leaves the disk's fate unclear.

## Predictions

1. **Recorded, the deciding question (O1's HA case):** what becomes of the old primary's claim and disk, and how the new standby is seeded. No direction is predicted; the notes have no evidence either way.
2. **The sidecar runs on the standby as well as the primary, reads PGDATA, is refused a write, and has a cgroup quota of 500m (`cpu.max` = `50000 100000`).**
3. After the failover, the promoted primary and the new standby both have the sidecar.
4. Recorded: Omni 18.4's settings on Kubernetes, the node's dirty-page and TCP settings, and the seeding's `pg_basebackup` flags.

## What follows

- **(a) The old disk survives with its data:** the HA case's B exists on Kubernetes. pgdelta could refresh it instead of a full `pg_basebackup`, if the operator reused it. That is a change for the integrator, which the positioning names.
- **(a) The claim is reused, and the new standby is seeded into it:** B exists at exactly the step pgdelta would replace. This is the strongest case for HA.
- **(a) The claim and disk are deleted:** on an unplanned failover no B exists on Kubernetes. The HA pitch narrows to rebuilds that keep the disk, and DR, pgdelta's strongest case, leads.
- **(b) The sidecar runs on every instance, reads PGDATA and has a quota:** pgdelta can be deployed as the operator ships, and the source cap is a cgroup quota. If it is missing on standbys, pgdelta on the destination needs another route, recorded as a limit.

## Limitations

One cluster, one failover, one zone; Omni 18.4 under operator 1.9.0. A real deployment's storage class or the operator's settings may keep or delete disks differently, and are recorded as found.
