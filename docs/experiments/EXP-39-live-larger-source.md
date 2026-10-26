# EXP-39: On a larger source, can v1 use more workers on a busy primary?

Status: **pre-registered October 26, 2026, before any run.** T10b's larger-source block, lean. Three VMs (`PAIR=t10c`: a 16-vCPU source, `n2-standard-16`; dest `n2-standard-8`; client `n2-standard-4`; 28 vCPUs), about 45 minutes with set-up.

## Question

Live mode caps the default source workers at 2 (EXP-28, EXP-30), from tests on 8-vCPU sources: with BLAKE2b, 4 workers missed O4 in one of two sessions, and with BLAKE3, 4 were slower than 2. On a 16-vCPU source under the same relative load, do 4 or 8 workers meet O4, and are they faster? Should the cap scale with the source's CPUs?

## Design

EXP-28's driver (`lab_exp28.sh`), unchanged, with `BLAKE=1` (pgdelta from a venv with `blake3` on both ends):
- **The source:** a 16-vCPU `n2-standard-16`.
- **The load:** the bank load from 128 clients on the client VM, at 0.6 × C, with C measured at set-up on this source.
- **Arms:** `none`, `full` (one `pg_basebackup` stream), and v1 with `--hash blake3` at 2, 4 and 8 source workers (`v1b3_j2`, `v1b3_j4`, `v1b3_j8`).
- **Runs:** unthrottled only (EXP-28's harder case), 2 runs each, order seed 390. O4's two numbers and the bar as EXP-28's. An arm whose excess lies within 20% of its δ gets two more runs.

## Predictions

1. v1 at 2 workers meets O4, as on 8 vCPUs.
2. **Deciding: v1 at 4 workers meets O4 on 16 vCPUs.** The workers' share of the source's CPU halves.
3. v1 at 4 workers is faster than at 2 here, unlike on 8 vCPUs with BLAKE3 (EXP-30: 3.05 against 2.73 s). More cores leave room for the reads and the sender beside the load.

## What follows

- **If 2 and 3 hold:** live mode's default scales with the source: at most CPUs ÷ 4 workers (2 on 8 vCPUs, 4 on 16), capped at 4. That is a change to `pgdelta/cli.py`, tested.
- **If 4 workers meet O4 but are no faster:** the cap stays at 2, since the workers buy nothing.
- **If 4 workers miss:** the cap stays at 2 on any size measured.

## Limitations

One session, one load level, unthrottled only, 2 repeats. EXP-28's limitations apply.
