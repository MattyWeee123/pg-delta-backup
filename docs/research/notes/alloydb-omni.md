# AlloyDB Omni: how it copies, rebuilds and protects data

*Research notes, October 23, 2026. A six-agent sweep of Google's public AlloyDB and AlloyDB Omni docs, the `alloydb-omni-samples` repository and GCP's network docs, followed by an independent check that re-fetched the sources and corrected 19 overstatements. Only public sources are cited. The confidence of each finding is given as high (stated in the cited page) or medium (inferred from it).*

These notes back the decisions of October 23 in [DECISIONS.md](../../DECISIONS.md#decisions-of-october-23-2026-t1-closed).

## How Omni builds and rebuilds standbys today

| Finding | Confidence | Source |
|---|---|---|
| On Kubernetes, a new standby is seeded with `pg_basebackup`. In the one seeding log Google publishes, PostgreSQL is stopped on the new instance, `pg_basebackup` writes into a side directory (`/mnt/disks/pgsql/pg_basebackup_data`), and that directory then replaces the data directory. The flags have no rate limit and no compression. Google marks this section as internal and subject to change. | high (log); the side-directory detail comes from one example | [Troubleshoot the operator (18.3.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.3.0/docs/troubleshoot-kubernetes-operator) |
| Initializing a standby is the slowest HA step and can take several hours on a large database. No other rebuild-time figure is published. | high | same page |
| On an unplanned failover, the operator deletes the previous primary instance and creates a new standby. A planned switchover converts the old primary to a standby instead. | high | [Kubernetes HA (18.3.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.3.0/docs/kubernetes-ha) |
| Auto-heal, on by default, deletes an unhealthy standby and creates a new one. It does not repair the existing data. | high | same page |
| **Not documented:** whether the deleted instance's disk (its PersistentVolumeClaim) is kept or deleted. | not found | |
| A failed `pg_basebackup` during standby setup has its own error code, DBSE2414. The documented remedy is to delete the instance and restart replication, which means another full copy. | high | [Error codes (18.1.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.1.0/docs/error-codes) |
| Cross-data-center DR secondaries are managed by a Replication resource whose `control` is `setup`, `promote` or `rewind`. After an unplanned DR failover, Google's procedure is to try `rewind` on the former primary and to fall back to `setup` (a full copy, documented as slow on large databases) if that has issues. | high | [Cross-data-center replication (18.3.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.3.0/docs/cross-data-center-replication/work-with-cross-data-center-replication) |
| `rewind` almost certainly runs `pg_rewind`: error DBSE1082 "Failed to rewind the database" occurs when turning a primary into a standby, and its remedy is rollback, manual repair, or re-creating the cluster. Google never names `pg_rewind`. | medium | [Error codes (18.1.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.1.0/docs/error-codes) |
| Major-version upgrades end all physical replication. Every standby, read-pool replica and DR secondary is rebuilt afterwards. | high | [Major version upgrade (17.5.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/17.5.0/docs/upgrade-kubernetes-database-major-version) |
| Standalone (container or VM) read replicas are built with `pg_basebackup` into an empty directory. | high | [Set up a read replica](https://docs.cloud.google.com/alloydb/omni/containers/current/docs/set-up-read-replica) |
| Managed AlloyDB (the cloud service, not Omni) backs up the original primary before re-creating it as a secondary, because it may hold writes the new primary never received. | high | [Cross-region replication](https://docs.cloud.google.com/alloydb/docs/cross-region-replication/work-with-cross-region-replication) |

**What a failed rebuild leaves behind today:**
- `pg_basebackup` deletes the directories it created when it fails ([docs, `--no-clean`](https://www.postgresql.org/docs/17/app-pgbasebackup.html)), and its target must be empty to start with.
- A `pg_rewind` that fails partway leaves the target "likely not in a state that can be recovered"; the docs recommend a fresh backup ([docs](https://www.postgresql.org/docs/17/app-pgrewind.html)).
- Omni's remedies for both failures (DBSE2414 and DBSE1082) are a delete and a full re-copy, or a re-created cluster.

So no Omni rebuild path guarantees a usable copy after a failed run. The one exception in Google's products is managed AlloyDB keeping a backup of an ex-primary, which may hold unreplicated writes.

## Storage, filesystems and sizing

| Finding | Confidence | Source |
|---|---|---|
| HA needs a full second copy of the data per standby, on the same disk configuration as the primary. Google's sizing table recommends a 2,000 GiB data disk for 1 TiB of data. | high | [Plan storage (18.3.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/18.3.0/docs/plan-storage-kubernetes) |
| A complete new copy written beside the old one needs about twice the data plus WAL, so at these sizes it probably doesn't fit on the same disk. | medium (inference from the table) | |
| On GKE, the default storage class is a balanced persistent disk formatted ext4, which has no reflinks. Omni's Kubernetes docs name no filesystem, so "mostly ext4" rests on GKE's default only. | high (GKE default); medium (Omni) | [GKE persistent volumes](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/persistent-volumes) |
| Non-Kubernetes installs (RHEL 9, Rocky 9) require ext4 or XFS, sized at twice the data. | high | [Plan a Linux installation](https://docs.cloud.google.com/alloydb/omni/linux/current/docs/plan-installation-linux) |
| Balanced persistent disk throughput is 0.28 MiB/s per GiB plus a 140 MiB/s baseline, up to 1,200 MiB/s per VM: about 1.4 Gbit/s on a 100 GiB disk and 5.9 Gbit/s on a 2,000 GiB disk. Persistent-disk writes get 60% of the VM's network bandwidth when they compete with other traffic. So an in-region rebuild can be limited by the disk, not the link. | high | [Disk performance](https://docs.cloud.google.com/compute/docs/disks/performance), [network bandwidth](https://docs.cloud.google.com/compute/docs/network-bandwidth) |
| Omni stores ordinary PostgreSQL files in fixed-size pages. Its caches live on separate volumes. | high | [Omni overview](https://docs.cloud.google.com/alloydb/omni/containers/current/docs/overview) |
| Transparent data encryption (GA in operator 1.8.0) encrypts data files with AES-256-XTS. A page comparison then compares ciphertext, so an old copy is reusable only if it shares the source's data keys. | high (TDE exists); medium (the consequence) | [TDE](https://docs.cloud.google.com/alloydb/omni/kubernetes/current/docs/transparent-data-encryption-omni) |

## Backups and pgBackRest

| Finding | Confidence | Source |
|---|---|---|
| The operator's BackupPlan is pgBackRest underneath (the operator's CRD schema says so; the docs page doesn't name the tool). Backups can go to a local disk, Cloud Storage or S3, as full, differential or incremental. | high | [Backups on Kubernetes](https://docs.cloud.google.com/alloydb/omni/kubernetes/current/docs/backup-kubernetes), [fleet CRDs](https://github.com/GoogleCloudPlatform/alloydb-omni-samples/blob/1.7.0/crd/fleet/fleet-crds.yaml) |
| Google's cross-cluster clone runs `pgbackrest restore --delta` into a **newly created** cluster. `--delta` is needed only because the new data directory isn't empty. It isn't a refresh of an older copy of the same data. | high | [DR with Cloud Storage](https://docs.cloud.google.com/alloydb/omni/kubernetes/current/docs/kubernetes-dr-backup-k8-gcs) |
| pgBackRest's delta restore reuses files whose checksums match. It works at file level unless the repository uses block incremental (pgBackRest 2.46+), which Omni's samples don't enable. | high | [pgBackRest command reference](https://pgbackrest.org/command.html) |
| HA and DR seeding bypass pgBackRest: they use `pg_basebackup` or `pg_rewind`. | high | sections above |

## Running a program next to the database

| Finding | Confidence | Source |
|---|---|---|
| The operator supports sidecar containers through a Sidecar resource. Each sidecar has its own CPU and memory limits and can mount the pod's existing disks, including the data disk. | high | [Sidecar containers](https://docs.cloud.google.com/alloydb/omni/kubernetes/current/docs/kubernetes-sidecar-container) |
| Google's own sample runs a third-party backup agent (Commvault) as a sidecar with the data disk mounted. Sidecars for backup or monitoring agents are a listed product capability. | high | [Commvault sample](https://github.com/GoogleCloudPlatform/alloydb-omni-samples/blob/main/samples/v1_sidecar_commvault.yaml) |
| Changing an active sidecar restarts the pod. | high | Sidecar docs |
| **Not documented:** whether a sidecar on the primary is also injected into standby pods. | not found | |

## Network and cost

| Finding | Confidence | Source |
|---|---|---|
| GCP VM egress inside a VPC is "generally 2 Gbps per vCPU", capped by machine series (N2: 32 Gbit/s). Traffic over external IPs is capped at 3 Gbit/s per flow. Inter-region traffic is also limited by a per-project quota. | high | [Network bandwidth](https://docs.cloud.google.com/compute/docs/network-bandwidth), [VPC quotas](https://docs.cloud.google.com/vpc/docs/quota) |
| In-region standbys use synchronous replication across zones. DR replicas in another region use asynchronous replication. | high | [Zonal and regional replication (16.3.0)](https://docs.cloud.google.com/alloydb/omni/kubernetes/16.3.0/docs/protect-data-using-zonal-regional-replication) |
| Moving 1 TiB once costs about $10 between zones, $20 between North American regions, $51 from North America to Europe, and nothing within one zone on internal IPs. | high (list prices, fetched October 23) | [Network pricing](https://cloud.google.com/vpc/network-pricing) |
| TLS is recommended for all connections. The operator generates certificates with cert-manager, a required install. No doc says TLS is enforced, or that replication traffic is encrypted. | high (recommended); not found (enforced) | [Connect](https://docs.cloud.google.com/alloydb/omni/kubernetes/current/docs/run-connect) |

## Licensing

| Finding | Confidence | Source |
|---|---|---|
| `alloydb-omni-samples` is Apache-2.0, needs a Google CLA, and has an `experimental/` folder for community tools, with earlier student projects in it. | high | [Repository](https://github.com/GoogleCloudPlatform/alloydb-omni-samples) |
| Google's policy lists the GPL as "restricted": such code must not be part of products delivered to outside customers. rsync is GPLv3-or-later. | high | [Google third-party licenses](https://opensource.google/documentation/reference/thirdparty/licenses), [rsync COPYING](https://github.com/RsyncProject/rsync/blob/master/COPYING) |
| Whether merely running rsync as a separate program counts as "part of" a product isn't settled by Google's public docs. | not found | |

## What these notes don't settle

- Whether the operator keeps the old primary's or old standby's disk when it deletes the instance.
- How a cross-data-center secondary is first seeded (probably `pg_basebackup`, via the same internal path).
- Whether Omni clusters enable data checksums or `wal_log_hints`, which `pg_rewind` needs.
- How often failovers, heals and DR re-seeds happen in real deployments.
