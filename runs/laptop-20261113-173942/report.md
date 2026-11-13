# Run laptop-20261113-173942


**Where:** laptop. **Database:** pgbench scale 50 (1.4 GB on disk). **Change:** 1% of rows, hot. **Repeats:** 1.

**Machine:** this laptop (arm64, 15 CPUs), PostgreSQL 18.6. No network: judge pgdelta by bytes and correctness here.

## Results

| Lane | Time | Sent | Of S's files | Round trips | vs pg_basebackup |
|---|---|---|---|---|---|
| pg_basebackup (copies everything) | 1.35 s | 816.6 MB | 56.8% | – | 1.00× |
| pgdelta (sends what differs) | 11.93 s | 21.8 MB | 1.5% | 168340 | 8.85× |

`pg_basebackup` skips old WAL and temporary files, so it sends less than S's whole directory.

![Time per lane](charts/times.svg)

## Correctness

The refreshed copy was started; its rows and balance (5000000|350000) match the source (5000000|350000). pgdelta also checked every file against the source's digest.

## Reproduce

```bash
python -m bench run --config runs/laptop-20261113-173942/config.json
```

Watch it: `python -m viz serve`, then pick "laptop-20261113-173942".
