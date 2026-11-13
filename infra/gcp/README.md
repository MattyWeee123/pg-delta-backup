# GCP benchmark machines (EXP-04 and later)

The experiments run on **sets** of VMs. A set has a source and a dest, and may have more roles, such as a client for the load. Several sets can exist at once, each with its own names, zones, machine types, data disks and SSH host keys. The default set is the original pair, in one zone:
- `pgd-source` holds the live database and the fresh backup.
- `pgd-dest` holds the old copy, receives the refresh and runs the EXP-04 orchestrator.

Traffic between VMs in one zone stays on internal addresses, so there is no egress charge. A set may put its VMs in different zones or regions; that traffic is charged. `throttle.sh` limits each VM's egress with `tc` to simulate slower links. EXP-04 applies it on both VMs, so both directions are shaped.

The default set:

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
./teardown.sh     # deletes both VMs and their disks, then checks nothing is left; fetch results first, /data is lost
```

`config.sh` holds every setting, and environment variables override them: `ZONE=us-central1-c LOCAL_SSDS=2 ./create.sh`. Give `remote.py` and `teardown.sh` the same settings as `create.sh`, or they look for the VMs elsewhere; a set file (below) keeps them in one place.

## VM sets

`PAIR=NAME` picks a set. With `PAIR` empty or unset, every script and `remote.py` act on the default set exactly as before sets existed. A set's name is 1 to 12 lowercase letters, digits or inner hyphens, starting with a letter; `default` is reserved for the unnamed set.

| | Default set (no `PAIR`) | Set `a` (`PAIR=a`) |
|---|---|---|
| VMs | `pgd-source`, `pgd-dest` | `pgd-a-source`, `pgd-a-dest`, and `pgd-a-ROLE` for any further role |
| Settings | `config.sh`, then the environment | `config.sh`, then `sets/a.sh` if it exists, then the environment |
| Host keys | `~/.ssh/pgd-bench/known_hosts` | `~/.ssh/pgd-bench/sets/a/known_hosts` |
| SSH config (`remote.py ssh-config`) | `~/.ssh/pgd-bench/ssh_config` | `~/.ssh/pgd-bench/sets/a/ssh_config` |
| Labels | `purpose=pg-delta-benchmark`, `owner=pg-delta-backup`, `pgd-set=default`, `pgd-role=ROLE` | the same with `pgd-set=a` |
| `remote.py fetch NAME` | `results/exp04/NAME` | `results/exp04/sets/a/NAME` |

The SSH key is the same for every set. Host keys and the SSH config are per set, so creating or deleting one set never touches another's.

```bash
# Two pairs at once, in two zones
PAIR=a ZONE=us-central1-a ./create.sh
PAIR=b ZONE=us-central1-c ./create.sh
python3 ../../experiments/network/remote.py --pair a wait-ready
PAIR=b python3 ../../experiments/network/remote.py wait-ready
PAIR=a ZONE=us-central1-a ./teardown.sh   # set a only; set b keeps running
```

**Set files.** `sets/NAME.sh` holds a set's settings, so every script and `remote.py` read the same ones. Write each line as `NAME=${NAME:-value}`, so the environment still overrides it (a fallback zone after a stockout, say). Commit it: `remote.py deploy` ships tracked files, and the reformat scripts read it on the VM. Two examples, not registered designs:

```bash
# sets/pd.sh: source and dest in two zones on 100 GB balanced persistent disks
ZONE=${ZONE:-us-central1-a}
DEST_ZONE=${DEST_ZONE:-us-central1-c}
DATA_DISK=${DATA_DISK:-pd-balanced}
PD_SIZE_GB=${PD_SIZE_GB:-100}

# sets/load.sh: a pair on local NVMe with the load's client on a third VM in the same zone
ROLES=${ROLES:-source dest client}
ZONE=${ZONE:-us-central1-a}
```

`python3 ../../experiments/network/remote.py --pair NAME config` prints what a set resolves to, from `config.sh` alone, with no gcloud call.

**Settings.** Every one can be set in the environment or in a set file.

| Variable | Default | Meaning |
|---|---|---|
| `PAIR` | empty | The set; empty is the default set |
| `ROLES` | `source dest` | The set's VMs. Each role is a lowercase word of up to 10 letters or digits; `source` and `dest` are required |
| `ZONE` | `us-central1-b` | The set's zone |
| `MACHINE` | `n2-standard-8` | The set's machine type |
| `DATA_DISK` | `local-nvme` | `local-nvme` or `pd-balanced` (Data disks, below) |
| `LOCAL_SSDS` | `4` | Local NVMe SSDs per VM with `local-nvme` |
| `PD_SIZE_GB` | none | The data disk's size with `pd-balanced`; required, since the disk's throughput depends on it |
| `ROLE_ZONE`, `ROLE_MACHINE`, `ROLE_DATA_DISK`, `ROLE_LOCAL_SSDS`, `ROLE_PD_SIZE_GB` | the set's | One role's own setting, upper case: `DEST_ZONE=us-east1-b`, `CLIENT_MACHINE=n2-standard-4` |
| `ROLE_VM` | `NAME_PREFIX-role` | One role's VM name (`SOURCE_VM`, `DEST_VM` as before) |
| `NAME_PREFIX` | `pgd`, or `pgd-PAIR` | The start of every VM name |
| `CLIENT_MACHINE`, `CLIENT_DATA_DISK`, `CLIENT_PD_SIZE_GB` | `n2-standard-4`, `pd-balanced`, `50` | The client role's own defaults: a smaller VM with a small balanced persistent disk, no local SSDs (which stocked out on October 22 and 23) |
| `LABELS` | `purpose=pg-delta-benchmark,owner=pg-delta-backup` | Labels on every VM and data disk, besides `pgd-set` and `pgd-role`. `create.sh` refuses labels without `purpose=pg-delta-benchmark` |
| `SSH_USER`, `SSH_KEY` | `bench`, `~/.ssh/pgd-bench/id_ed25519` | The login and the Mac-side key, for every set |
| `SSH_SET_DIR` | the key's folder, plus `sets/PAIR` for a named set | Where the set's `known_hosts` and `ssh_config` are (`SSH_KNOWN_HOSTS`, `SSH_CONFIG` override each) |
| `PGD_SETS_DIR` | `infra/gcp/sets` | Where set files are |
| `PROJECT`, `IMAGE_FAMILY`, `IMAGE_PROJECT`, `GCLOUD` | `postgres-delta-backup`, `debian-12`, `debian-cloud`, the `gcloud` on `PATH` | As before |
| `DRY_RUN` | `0` | `1`: print every gcloud command, run none |
| `QUOTA_CHECK` | `1` | `0`: skip `create.sh`'s quota check |

## Data disks

Both kinds are ext4 at `/data`, made with the same options (inode tables and journal initialised up front, so no background zeroing runs during measurements) and mounted with `noatime`. Every VM's boot disk is a 50 GB balanced persistent disk.
- **`local-nvme`:** `LOCAL_SSDS` local NVMe SSDs of 375 GB, striped as the named array `/dev/md/pgdata` when there are more than one, or the single SSD itself. Their data survives a reboot or a reset, and is lost when the VM stops.
- **`pd-balanced`:** one balanced persistent disk of `PD_SIZE_GB` GB, created with the VM, labelled like it, attached with the device name `pgd-data` (so at `/dev/disk/by-id/google-pgd-data`) and deleted with it. Its data survives a reboot, a reset and a stop. Balanced PD's throughput is 140 MiB/s plus 0.28 MiB/s per GiB, up to 1,200 MiB/s per VM ([Omni notes](../../docs/research/notes/alloydb-omni.md)): about 168 MiB/s at 100 GiB and 700 MiB/s at 2,000 GiB. So the size is a factor of every result on it, and each run should record it (`agent.py env`'s `data_mount` has the size).

## create.sh

`create.sh` never changes project settings. In order:
1. It checks the set's settings: the purpose label, VM names (lowercase, at most 58 characters, one per role), zones, and each role's disk. On a problem it exits 2 and makes nothing.
2. It prints each role's VM, zone, machine type and disk.
3. It checks that the Compute Engine API is enabled (read-only), and creates the Mac-side key if it's missing.
4. It lists the set's VMs in every zone. A role's VM that exists in its zone and belongs to the set is kept. If one is in another zone, or doesn't belong to the set (another set's, or without the purpose label), it exits 1 and makes nothing.
5. It checks the quota (below) for the VMs it will make.
6. It creates the missing VMs, labelled, with `pgd-set`, `pgd-role` and `pgd-data-disk` in their metadata (`agent.py env` records them in every run's `run.json`, under `vm_set`).
7. If it made any, it removes the set's own `known_hosts` and `ssh_config`: fresh VMs have fresh host keys and addresses.
8. It lists every benchmark VM in the project, with its set.

**The quota check** reads the project's and each region's quotas (read-only) and compares what is free now with what the new VMs need: vCPUs across all regions; per region, the machine family's vCPUs where the region lists them (`N2_CPUS`), else `CPUS`; instances; in-use addresses; local SSD; and SSD-backed persistent disk (boot disks and `pd-balanced`). If one falls short, it prints them all and exits 1 before making anything, so a set is never half made. A quota it can't read is reported and the check skipped. On October 24, 2026 the binding limit was 32 vCPUs across all regions, with 24 instances and 8 external addresses in us-central1. Every VM has an external address, since the Mac reaches the VMs by it, so at most 8 VMs of all sets can exist in us-central1 at once. In vCPUs: a pair of n2-standard-8 is 16, a pair with an n2-standard-4 client 20, two pairs 32.

## teardown.sh

`teardown.sh` deletes one set and checks that nothing of it is left. Other sets are never touched.
- **What is the set's:** a VM labelled `purpose=pg-delta-benchmark` and `pgd-set=NAME`, in any zone; or, made before sets had labels, one with the purpose label, no set label and one of the set's names. A VM with one of the set's names that belongs to another set, or has no purpose label, is left alone and reported. A VM whose role was dropped from `ROLES` still carries the set's label, so it goes too.
- **Its VMs** are deleted zone by zone, with `--delete-disks=all`, so the boot disk, the local SSDs and the data disk go with them.
- **Disks left over** (a data disk whose VM failed to start, a boot disk kept by hand) are deleted too: those labelled with the set, and unlabelled ones named after one of its VMs (`VM` or `VM-data`) that no VM outside the set uses.
- **Then it lists again.** If any VM or disk of the set is still there, it names each and exits 1. Otherwise it says none is left, and lists the benchmark VMs that remain, of every set.

It doesn't touch the set's `known_hosts`; `create.sh` removes it when it next makes the set's VMs.

## Dry run

`DRY_RUN=1 ./create.sh` and `DRY_RUN=1 ./teardown.sh` print every gcloud command, read-only ones included, as `+ gcloud ...` lines a shell can run, and run none. The reads then stand in what the script assumes: `create.sh` that nothing of the set exists yet (so it prints every create), `teardown.sh` that each role's VM exists in its zone and no disk is left over (so it prints every delete). Nothing on the Mac changes either: no key is made and no `known_hosts` is removed. The quota isn't computed.

## remote.py

`remote.py --pair NAME COMMAND`, or `PAIR=NAME` in the environment, acts on a set; without either, on the default set as before ([experiments/network/](../../experiments/network/README.md#running)). `--pair` goes before the command.
- `wait-ready`, `provision`, `deploy` and `ips` cover every VM of the set, each described in its own zone.
- `setup-ssh` authorizes dest's key on every other role (the source, and a client) and checks dest reaches each.
- `ssh ROLE` takes any of the set's roles.
- `fetch NAME` puts a named set's runs under `results/exp04/sets/NAME/`, so two sets' runs of one name stay apart.
- `config` prints the set's resolved settings, with no gcloud call.
- `ssh-config` writes the set's SSH config: one `Host` per VM, by role and by name, with the set's own `known_hosts`. Then `ssh -F ~/.ssh/pgd-bench/sets/a/ssh_config dest`.

## On the VMs

`startup.sh` runs on every boot and is idempotent:
1. It installs mdadm, rsync, Python, GNU time (EXP-04 measures each tool's peak memory with it) and PostgreSQL 17 from PGDG, with `--no-install-recommends` and without a default cluster. It also installs pgBackRest from PGDG (EXP-04's T11 methods) and xfsprogs (for `reformat_dest_xfs.sh`). A VM made before GNU time, pgBackRest or xfsprogs was on the list gets them at its next boot.
2. It finds the data disk: the `pd-balanced` disk at `/dev/disk/by-id/google-pgd-data`, or the local NVMe SSDs. It stops with an error if there are neither, or both. Several SSDs become the named array `/dev/md/pgdata`, or the array is reassembled after a reboot.
3. It formats the disk or array as ext4 if it holds no filesystem yet, with inode tables initialised up front so no background zeroing runs during measurements, and mounts it at `/data`.
4. It writes `/data/.startup-done`.

It took about 45 seconds on first boot, with local NVMe.

`reformat_dest_xfs.sh --yes` (as root, on dest only) turns dest's `/data` into XFS with reflinks, for T3 (d), ext4 against XFS clones:
- **It destroys everything on dest's `/data`:** B0, every output, and the results under `/data/exp04-results`. Fetch results first.
- It unmounts `/data`, runs `mkfs.xfs -f -m reflink=1` on the existing array `/dev/md/pgdata` (or on a `pd-balanced` dest, its data disk), and mounts it again at `/data` with `noatime`, as `startup.sh` does. `/data` gets back its old owner and mode (root, `1777`).
- It recreates `/data/.startup-done` and prints the filesystem's reflink setting, `reflink=1`. It exits non-zero if that isn't what XFS reports.
- It refuses to run without `--yes`, without root, on the source or any host other than the set's dest, when `/data` is not the data disk, or while an EXP-04 run holds its lock. The source stays ext4.
- On a named set's dest, give it the set: `sudo PAIR=a ./reformat_dest_xfs.sh --yes`.
- XFS survives a reboot: `startup.sh` formats the disk only when it has no filesystem. `reformat_dest_ext4.sh --yes` makes it ext4 again, with `startup.sh`'s options.
- Every EXP-04 run records `/data`'s filesystem (`data_mount`) and, on XFS, its reflink setting (`data_reflink`) for both VMs in `run.json`.

```bash
python3 ../../experiments/network/remote.py ssh dest sudo ~/pgd/infra/gcp/reformat_dest_xfs.sh --yes
python3 ../../experiments/network/remote.py --pair a ssh dest sudo PAIR=a ~/pgd/infra/gcp/reformat_dest_xfs.sh --yes
```

`throttle.sh RATE|off|show [DEV]` puts a token-bucket filter on the interface:
- **Burst:** 10 ms of tokens, at least 128 KiB. The old fixed 1 MB burst was too small for multi-Gbit/s rates.
- **Latency:** 50 ms.

Measured on these VMs with a single TCP stream, from 0.1 to 10 Gbit/s, the achieved payload rate was 94 to 96% of the target in both directions. Unthrottled (`off`, the default `mq` qdisc), the link ran at 12 to 15 Gbit/s.

`wan.sh RATE DELAY_ONE_WAY_MS PEER_IP CONTROL_PORT [DEV]|off|show [--json]` is EXP-13's emulated WAN (`throttle.sh` is unchanged): a `prio` root whose delayed band, for traffic to the other VM only, is `netem` with the one-way delay and `limit 1000000`, then a `tbf` at the rate with `throttle.sh`'s burst and a queue of max(50 ms, the round trip). TCP to or from `CONTROL_PORT` stays in the plain band, neither delayed nor shaped: `startup.sh` has sshd listen on 2222 as well as 22, for the harness's control path. Only traffic to `PEER_IP` is shaped, so a client VM in the set talks to the source on an unshaped link. `DRY_RUN=1` prints the `tc` commands. See [experiments/network/](../../experiments/network/README.md#methods-for-t12).

`install_incumbents.sh` installs the incumbent refresh tools that T11 runs ([experiments/incumbents/](../../experiments/incumbents/README.md)). Run it as root on a VM after `startup.sh`: `sudo ./install_incumbents.sh`. It is idempotent and prints both versions.
- **pg_probackup for PostgreSQL 17:** the `pg-probackup-17` package from Postgres Pro's repository (`repo.postgrespro.ru`, with its signing key in `/etc/apt/keyrings`). On October 23, 2026 the repository had version 2.5.16 for bookworm. Without such a package, the script builds the official source at tag 2.5.16. The build uses PGDG's `postgresql-server-dev-17` and the PostgreSQL source of the installed version, checked against its published SHA-256. That fallback hasn't been run. Either way, `/usr/local/bin/pg_probackup-17` runs it afterwards: the build installs there, and the package's `/usr/bin/pg_probackup-17` is linked there. EXP-11's harness runs that path on both VMs (`--pg-probackup`'s default, and catchup's `--remote-path`).
- **WAL-G v3.0.9:** the PostgreSQL release binary `wal-g-pg-22.04-amd64.tar.gz` from github.com/wal-g/wal-g. Its SHA-256 must match the release's published `.sha256` file and the value pinned in the script. The Ubuntu 22.04 build needs glibc 2.35, and Debian 12 has 2.36. It is installed as `/usr/local/bin/wal-g`.

`/usr/local/share/pgd-incumbents.txt` records which way each tool was installed.

Costs accrue only while the VMs and their disks exist: about 1 USD/hour for a pair of n2-standard-8 with local NVMe. Every EXP-04 run records the machine types, kernel, versions and timestamps in its `run.json`. See [experiments/network/](../../experiments/network/README.md).

## Tests

`python -m unittest discover -s tests -p test_infra_sets.py` covers the sets without a cloud call: `config.sh`'s resolution (the default set, a named set, set files, roles and per-role settings, the names it refuses); both scripts' dry runs, command by command; both scripts against a fake gcloud that keeps a project's VMs and disks in a file (each set's own host keys, a VM of another set or in another zone refused, the quota check, teardown deleting only its set and failing when something is left); `remote.py`'s set selection; `startup.sh`'s choice of data disk; the reformat scripts on a named set's dest; and `agent.py env`'s record of the set.

Status: the default pair was created and used from EXP-04 (2026-10-22) to EXP-12. VM sets were added on 2026-10-24 and tested against the fake gcloud only: the first real `create.sh` of a named set, of a `pd-balanced` disk and of a client is their smoke test.
