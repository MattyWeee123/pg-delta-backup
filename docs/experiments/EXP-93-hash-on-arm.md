# EXP-93: On an ARM server, is SHA-256 faster than BLAKE3 per page, as on the Mac?

Status: **pre-registered November 13, 2026, before any run.**

## Question

EXP-90's hash benchmark gave opposite answers on two machines. On the Mac (Apple M5 Pro, arm64, Python 3.14), keyed SHA-256 hashed 8 KiB pages at 1.33x keyed BLAKE3's rate. On the VMs (AMD EPYC 7B13, x86, with SHA-NI, Python 3.11) it ran at 0.474x. Per process, BLAKE3's rate was similar on both (275,456 and 368,299 pages a second); SHA-256's was not (366,336 and 174,592).

The team's industry mentor suggested SHA-2 on October 30 because CPUs implement it in instructions. Whether the Mac's answer holds on ARM servers, or belongs to Apple's chip, is untested. The owner asked to test it on a cloud ARM VM.

**What it decides:** whether "SHA-256 is slower than BLAKE3" (EXP-90's D3) holds only on x86, and so what the idea archive's "A faster hash" row says about ARM. It decides no default: no hashing run has been bound by CPU (EXP-90), so neither result changes pgdelta's speed on the settings measured.

## Hypothesis and predictions

The sign follows the instruction set: ARM cores with SHA-2 instructions beat BLAKE3's 128-bit vector code (NEON), while x86 cores run BLAKE3 on 256-bit vectors (AVX2).

| # | Prediction | Bar |
|---|---|---|
| G1 (deciding) | On Google Axion (`c4a-standard-8`, Arm Neoverse V2), keyed SHA-256's median pages per second, one process, is at least BLAKE3's | ratio SHA-256 / BLAKE3 ≥ 1.00 |
| G2 | The same holds on Ampere Altra (`t2a-standard-8`, Neoverse N1), if quota allows a second VM | ratio ≥ 1.00 |
| G3 | BLAKE3 is still at least 1.5x BLAKE2b on ARM (EXP-29's bar, met on Intel and AMD) | ratio ≥ 1.5 |

**If G1 holds:** the Mac's result is an ARM result, not only Apple's; the idea archive records that SHA-256 would be the faster hash on ARM servers, and stays irrelevant while no run is bound by CPU.
**If G1 fails:** the Mac's result is specific to Apple's chip; EXP-90's D3 holds on both server families tested.

## Design

- **Machines:** one `c4a-standard-8` (8 vCPUs, Google Axion) and, if the region's quota allows, one `t2a-standard-8` (8 vCPUs, Ampere Altra), each alone, labelled `purpose=pg-delta-benchmark`, deleted after the run.
- **Software:** Debian 12 for arm64, its Python 3.11 (the version the x86 VMs ran, removing EXP-90's Python-version confound) and `blake3` 1.0.11 from PyPI (the version every earlier run used).
- **The benchmark:** `experiments/network/hashbench.py` unchanged, as EXP-90 ran it: `--hashes blake2b,blake3,sha256 --seconds 3 --repeats 5 --procs 1,4`. It hashes 8 KiB pages keyed as pgdelta keys a leaf and prints pages per second per process, the medians and the ratios, with the CPU model and flags.
- **Decided on:** the one-process medians. The 4-process run is reported, not deciding.
- **Noise:** EXP-90's repeats agreed within 0.3% on the VMs and 4% on the Mac. A ratio within 3% of 1.00 is reported as a tie.
- **Rule 16:** `experiments/network/hashbench.py` is recorded at registration (`analysis/integrity.py register EXP-93`).

## Limitations, known before running

- Two ARM server cores (if both run), one VM each, no repeat session: a microbenchmark of the hash function alone, not of pgdelta.
- The keyed SHA-256 construction (a key-padded block absorbed first) is hashbench's, not an HMAC; it matches what pgdelta would do if it used SHA-256.
