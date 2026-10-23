# GCP benchmark machines (EXP-04)

Two identical VMs in one zone:
- `pgd-source` holds the live database and the fresh backup.
- `pgd-dest` holds the old copy, receives the refresh and runs the EXP-04 orchestrator.

Traffic between them stays inside one zone on internal addresses, so there is no egress charge. `throttle.sh` limits each VM's egress with `tc` to simulate slower links. EXP-04 applies it on both VMs, so both directions are shaped.

| Setting | Value | Why |
|---|---|---|
| Machine | `n2-standard-8` (8 vCPUs, 32 GB, Cascade Lake) | Enough CPU for hashing and PostgreSQL; up to 16 Gbps egress |
| Data disks | 4 local NVMe SSDs, RAID 0, ext4 at `/data` | Measured 1.6 GB/s writes and 2.0 GB/s reads with direct I/O, so disks aren't the bottleneck. On 2026-10-22 no zone in us-central1 had `n2-standard-8` with 2 local SSDs; us-central1-b had 4. |
| Zone | `us-central1-b` | Stockout in -a, -c and -f |
| OS | Debian 12 (kernel 6.1), PostgreSQL 17.11 and pgBackRest from PGDG, rsync 3.2.7, Python 3.11 | Same major version as the local experiments |
| Access | User `bench`, key `~/.ssh/pgd-bench/id_ed25519` on the Mac | The key is in instance metadata with `block-project-ssh-keys`, so the project's metadata is untouched |
| Service account | none | The VMs need no Google APIs |

```bash
./create.sh       # creates both VMs; the startup script installs PostgreSQL and tools and builds /data
python3 ../../experiments/network/remote.py wait-ready
./teardown.sh     # deletes both VMs; fetch results first, /data is lost
```

`create.sh` never changes project settings. It checks that the Compute Engine API is enabled, creates the Mac-side key if it's missing, creates the two VMs labelled `purpose=pg-delta-benchmark`, and forgets the old host keys. `config.sh` holds every setting, and environment variables override them: `ZONE=us-central1-c LOCAL_SSDS=2 ./create.sh`.

`startup.sh` runs on every boot and is idempotent:
1. It installs mdadm, rsync, Python, GNU time (EXP-04 measures each tool's peak memory with it) and PostgreSQL 17 from PGDG, with `--no-install-recommends` and without a default cluster. It also installs pgBackRest from PGDG (EXP-04's T11 methods) and xfsprogs (for `reformat_dest_xfs.sh`). A VM made before GNU time, pgBackRest or xfsprogs was on the list gets them at its next boot.
2. It builds the named array `/dev/md/pgdata` from all local NVMe SSDs, or reassembles it after a reboot.
3. It formats the array as ext4 if it holds no filesystem yet, with inode tables initialised up front so no background zeroing runs during measurements, and mounts it at `/data`.
4. It writes `/data/.startup-done`.

It took about 45 seconds on first boot.

`reformat_dest_xfs.sh --yes` (as root, on `pgd-dest` only) turns dest's `/data` into XFS with reflinks, for T3 (d), ext4 against XFS clones:
- **It destroys everything on dest's `/data`:** B0, every output, and the results under `/data/exp04-results`. Fetch results first.
- It unmounts `/data`, runs `mkfs.xfs -f -m reflink=1` on the existing array `/dev/md/pgdata`, and mounts it again at `/data` with `noatime`, as `startup.sh` does. `/data` gets back its old owner and mode (root, `1777`).
- It recreates `/data/.startup-done` and prints the filesystem's reflink setting, `reflink=1`. It exits non-zero if that isn't what XFS reports.
- It refuses to run without `--yes`, without root, on `pgd-source` or any host other than `pgd-dest`, when `/data` is not the array, or while an EXP-04 run holds its lock. `pgd-source` stays ext4.
- XFS survives a reboot: `startup.sh` formats the array only when it has no filesystem. Recreate the VM to go back to ext4.
- Every EXP-04 run records `/data`'s filesystem (`data_mount`) and, on XFS, its reflink setting (`data_reflink`) for both VMs in `run.json`.

```bash
python3 ../../experiments/network/remote.py ssh dest sudo ~/pgd/infra/gcp/reformat_dest_xfs.sh --yes
```

`throttle.sh RATE|off|show [DEV]` puts a token-bucket filter on the interface:
- **Burst:** 10 ms of tokens, at least 128 KiB. The old fixed 1 MB burst was too small for multi-Gbit/s rates.
- **Latency:** 50 ms.

Measured on these VMs with a single TCP stream, from 0.1 to 10 Gbit/s, the achieved payload rate was 94 to 96% of the target in both directions. Unthrottled (`off`, the default `mq` qdisc), the link ran at 12 to 15 Gbit/s.

Costs accrue only while the VMs exist, about 1 USD/hour for both. Every EXP-04 run records the machine types, kernel, versions and timestamps in its `run.json`. See [experiments/network/](../../experiments/network/README.md).

Status: created and used for the EXP-04 smoke sweep on 2026-10-22.
