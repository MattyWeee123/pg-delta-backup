# bench: run a benchmark, record everything, publish it as a pull request

One command runs a benchmark on your laptop or on two Google Cloud VMs, records everything about it, and (with `--publish`) commits it to its own branch and opens a pull request. Or use the launcher page instead of a terminal: `python -m viz serve`, then click **New run →**.

```bash
python -m bench run --where laptop --publish                           # free, about a minute
python -m bench estimate --where gcp --preset quick --link 1gbit       # cost and checks, creates nothing
python -m bench run --where gcp --preset quick --link 1gbit --publish  # asks before creating VMs
python -m bench cleanup                                                # delete benchmark VMs past their expiry
```

## What a run records

Everything goes to `runs/<name>/`, which isn't committed, until you publish:
- `config.json`: every setting, so `python -m bench run --config runs/<name>/config.json` repeats the run;
- `events-<lane>.jsonl`: one per method and repeat, which `python -m viz serve` shows live or replays;
- `stats-<lane>.json`: pgdelta's full statistics;
- `raw/<lane>/`, for cloud runs: the 0.1-second link, disk and CPU samples from both VMs;
- `report.md` and `charts/`: a results table (time, bytes, share of S, round trips, ratio to `pg_basebackup`), the correctness check, link and disk over time, the cost estimate and how to reproduce.

## Settings

Flags or a JSON config; anything left out comes from the preset.

| Setting | Default | Meaning |
|---|---|---|
| `where` | `laptop` | `laptop` or `gcp` |
| `methods` | `pg_basebackup,pgdelta` | which methods to compare, each a lane |
| `scale` | 50 | pgbench scale, about 15 MB each |
| `change` | `hot` | 1% of rows: `hot` (one range, few pages) or `uniform` (every 100th row, most pages) |
| `repeats` | 1 | runs of each method |
| `chunk_size` | 8192 | pgdelta's chunk size |
| `link` | `full` | cloud: `full`, or a cap such as `1gbit` or `500mbit` (applied to both VMs) |
| `machine`, `disk_type`, `disk_gb` | preset | cloud VM type, and its data disk at `/data` |
| `zones` | `us-central1-a`, `-b` | cloud: source zone, destination zone |

**Presets:**
- **`quick`:** two `n2-standard-4` VMs with 50 GB balanced disks, scale 50 (750 MB). About 12 minutes and $0.10.
- **`baseline`:** the first phase's EXP-90 to EXP-92 setup. Two `n2d-highcpu-8` VMs (8 GB memory) with 150 GB balanced disks, and scale 600 (18 GB, bigger than memory), so results compare with that phase's.

## How a cloud run works

1. **Two Debian 13 VMs** are created in two zones, each with PostgreSQL 17 and a data disk at `/data`.
2. **The source builds the database,** the destination takes the old copy with `pg_basebackup`, then the source changes 1% of the rows.
3. **Each lane** runs with both VMs' caches dropped, the link capped if asked, and the sampler on both VMs. pgdelta's progress streams back over ssh, so you can watch it live.
4. **The refreshed copy is started,** and its rows compared with the source.
5. **The VMs are deleted,** whatever happens.

**Guardrails:**
- **Before anything is created:** an estimate and a quota check.
- **One run at a time:** a run refuses to start while other benchmark VMs exist.
- **Expiry:** every VM is labelled `purpose=pg-delta-benchmark` with an expiry time, and `bench cleanup` deletes expired ones.
- **Prices:** the dollar figures are rough list prices, a guide, not billing.

You need `gcloud`, signed in (`gcloud auth login`), with the project set (`gcloud config set project …`).

## Publishing

`--publish` (or the launcher's checkbox):
1. **Scrubs the run:** IP addresses, email addresses and home-directory paths are masked, and anything that looks like a key or token stops the publish.
2. **Commits it** to branch `runs/<name>` from a temporary worktree, so your own working copy is untouched.
3. **Pushes and opens a pull request** with the report. Merge it to keep the result.

`python -m bench publish <name>` publishes a run you've already made.
