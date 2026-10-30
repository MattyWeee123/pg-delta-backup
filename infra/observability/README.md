# Latency observability: Prometheus + Grafana

Two dashboards over the latency work: a **live** view of what the source's load feels while a backup runs, and
an **evidence** view of what the slowdown experiments have already measured.

This is an instrument and a reading aid. It is **not** the evidence path: a committed analysis script producing
a committed figure is (`analysis/exp28.py` → `docs/figures/exp28-slowdown.svg`). A dashboard rendered from a
volume on a laptop cannot be refereed. Use Grafana to watch a session and to show people the shape of the
problem; cite the write-ups.

## The rule this stack must not break

**Nothing here may scrape the source.** O4's bar is 0.05 s of the load's work and a worst drop of 0.03 — small
enough that an exporter's own CPU could spend it. EXP-28 moved the load's client onto its own VM precisely
because a client sharing the source's CPU made EXP-12's verdict fragile; adding a source-side agent would
reintroduce that confound in the instrument meant to measure it.

So `pgbench_exporter.py` runs on the **client** VM and reads a log pgbench already writes to the client's own
tmpfs. It costs the source nothing. `prometheus/targets/node.json` is for the dest and client only, and is
empty by default.

If you ever decide a source-side exporter is worth it, it needs an arm that measures what the exporter itself
costs the load, pre-registered like any other change. Do not just add it to the targets file.

## Exposure

Both ports bind to **loopback only**, because the obvious place to run this is a session's client VM and those
VMs have external IPs. Published on `0.0.0.0` this would be a world-readable dashboard with an unauthenticated
Prometheus beside it.

Reach a VM's stack over the SSH connection the harness already uses:

```sh
ssh -i ~/.ssh/exp04_ed25519 -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 CLIENT_IP
```

Anonymous access is **Viewer**, not Admin: enough to open a dashboard in a demo, and nothing more. The
dashboards are provisioned read-only, so Admin bought nothing. Basic auth stays enabled as the way in to
administer — set `GF_ADMIN_PASSWORD` rather than leaving the default if you change the binding.

Prometheus runs without `--web.enable-admin-api` (its `delete_series` endpoint needs no credentials and would
let any reader destroy the backfilled evidence) and without `--web.enable-lifecycle`.

To expose it deliberately on a trusted network: `BIND_ADDR=0.0.0.0 GF_ADMIN_PASSWORD=... docker compose up -d`,
and restrict the source range with a firewall rule first.

## Quick start: see it working with no VM

```sh
cd infra/observability
docker compose up -d       # Grafana 127.0.0.1:3000, Prometheus 127.0.0.1:9090

# real measured results from the committed CSVs
python backfill.py
docker compose --profile backfill run --rm backfill
docker compose restart prometheus
```

Open <http://127.0.0.1:3000> → *pgdelta: measured latency evidence*, and set the time range to
**2026-10-24 18:00 → 2026-10-27 14:00 UTC** (the dashboard already defaults to it). That is EXP-12, EXP-28 and
EXP-57 as measured.

To exercise the **live** dashboard without a VM, generate a synthetic load:

```sh
python simulate_load.py --out /tmp/sim --seconds 200 --rate 300 --backup-after 25 --backup-seconds 50
python pgbench_exporter.py --log-dir /tmp/sim --pattern 'load.*' --window-file /tmp/sim/backup-window.json
```

`simulate_load.py` writes made-up numbers. It exists to check the exporter, the scraping and the panels before
a session spends VM time — never cite it.

## Live, in a session

1. Copy `pgbench_exporter.py` to the client VM (it needs only the standard library).
2. Start it against the directory pgbench logs into, after the load starts:

   ```sh
   python3 pgbench_exporter.py --log-dir /dev/shm/exp28 --pattern 'load.*' \
       --window-file /dev/shm/exp28/backup-window.json --port 9187
   ```

3. Put `CLIENT_IP:9187` in `prometheus/targets/pgbench.json` and `docker compose restart prometheus`.
   (`--web.enable-lifecycle` is deliberately off, so there is no unauthenticated `/-/reload` endpoint.)
4. Have the lab driver write the timed arm's boundaries to the window file, so the dashboard shades the real
   window instead of inferring it from a spike:

   ```sh
   printf '{"t0_utc": %s}\n' "$(date +%s.%N)" > /dev/shm/exp28/backup-window.json
   # ... the arm runs ...
   printf '{"t0_utc": %s, "t1_utc": %s}\n' "$T0" "$(date +%s.%N)" > /dev/shm/exp28/backup-window.json
   ```

   One caveat, and it is the same one the analysis handles: `t0`/`t1` are on **dest's** clock while the log is
   on the **client's**. `analysis/exp28.py` maps between them using chrony's offsets. A dashboard showing both
   without that mapping is off by the skew — fine for watching, not for a number.

## What the metrics mean

Live (`pgbench_exporter.py`, from pgbench's `-l` logs):

| Metric | Meaning |
|---|---|
| `pgdelta_txn_latency_ms{quantile}` | exact p50/p95/p99 over the last `--window-seconds` of completed transactions |
| `pgdelta_txn_schedule_lag_ms{quantile}` | how long a transaction waited past its scheduled start (open-loop queueing) |
| `pgdelta_txn_rate_per_second` | completed transactions per second over the window |
| `pgdelta_txn_failed_total` | transactions pgbench logged `failed` or `skipped` |
| `pgdelta_backup_active` | 1 inside the driver's `t0..t1` |

Backfilled (`backfill.py`, from `docs/experiments/data/*.csv`), prefixed `pgdelta_evidence_` so an aggregate can
never be mistaken for a live sample: `latency_ms{quantile}`, `work_lost_seconds`, `worst_drop_ratio`,
`refresh_seconds`, `source_cpu_seconds`, `dirty_gb`, `start_blocked_seconds`.

Backfilled series are **flat lines across the session's wall-clock window**. The committed files hold per-arm
and per-cell aggregates, not per-second samples — the per-transaction logs stay on the VMs — so the time axis
carries only *when the session ran*, not a curve. A real latency-over-time curve comes from the live path.

## Why quantiles are gauges, not histograms

pgbench records every transaction's exact latency. A Prometheus histogram would re-bucket numbers we already
hold exactly and then interpolate them back, so p99 would depend on bucket edges. The exporter computes exact
quantiles over its window instead, by the same linear-interpolation rule `numpy.percentile` uses, so a panel
and a write-up cannot disagree about what p95 means. `tests/test_observability.py` asserts that agreement.

The cost is that these gauges **cannot be averaged or summed across targets**. With one client per session that
is not a limitation; with several, read each separately.

## Dashboards

- `grafana/dashboards/pgdelta-live-slowdown.json` — latency p50/p95/p99, the backup window as a state timeline,
  throughput, schedule lag, failed transactions.
- `grafana/dashboards/pgdelta-evidence.json` — EXP-28's worker-count cliff, its two O4 numbers against their
  tolerances, EXP-57's open conflict, EXP-12's cells by load block and tier.

They are provisioned **read-only** (`allowUiUpdates: false`). Edit the JSON and commit it; a dashboard changed
in the browser and not committed is a figure nobody can reproduce.

## Tests

```sh
python -m unittest discover -s tests -t tests -p 'test_observability.py'
```

Covers the parser's agreement with `measure.parse_pgbench_line`, the quantile rule against `numpy.percentile`,
partial-line and replaced-file handling in the tailer, window trimming by the sample's own clock, the backup
window's edges, and that every rendered metric is declared.
