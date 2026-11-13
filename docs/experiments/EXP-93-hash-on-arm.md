# EXP-93: On an ARM server, is SHA-256 faster than BLAKE3 per page, as on the Mac?

Status: **done November 13, 2026 (VMs created, run and deleted the same afternoon). G1 and G2 held, G3 failed.** On both ARM servers, keyed SHA-256 hashed 8 KiB pages about 3x as fast as keyed BLAKE3 from the PyPI package (2.97 on Google Axion, 3.09 on Ampere Altra), so the Mac's sign is an ARM result, not only Apple's. But BLAKE3 from that package was slower than BLAKE2b on both (0.683 and 0.789; bar 1.5). An exploratory arm, not registered, found why: the package is built without BLAKE3's NEON code. Built with it, BLAKE3 doubled on the Altra (2.09x, now 1.65x BLAKE2b), and SHA-256 still led it by 1.48x. No default changes: no hashing run has been bound by CPU.

*Pre-registered November 13, 2026, before any run (bb11c76); the design below is as registered.*

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

## Results

**Where it ran:** `us-central1`. The Axion VM was refused in `us-central1-a` for lack of capacity and ran in `us-central1-b`; the Altra ran in `us-central1-a`. Each VM ran Debian 12 for arm64 and its Python 3.11.2, with `blake3` 1.0.11, and was deleted after its run. `hashbench.py` was unchanged since registration (`analysis/integrity.py check EXP-93`: 1 of 1 files unchanged). The script is `experiments/network/exp93_arm.sh`; the data is in `docs/experiments/data/exp93-hashbench-*.json`.

**Pages per second per process, one process (medians of 5; every set of repeats within 0.3%):**

| Machine | CPU | BLAKE2b | BLAKE3 | SHA-256 | SHA-256 / BLAKE3 | BLAKE3 / BLAKE2b |
|---|---|---|---|---|---|---|
| `c4a-standard-8` | Google Axion, Neoverse V2 | 102,656 | 70,144 | 208,299 | **2.970** | 0.683 |
| `t2a-standard-8` | Ampere Altra, Neoverse N1 | 78,507 | 61,952 | 191,573 | **3.092** | 0.789 |
| `t2a-standard-8`, BLAKE3 built with NEON (exploratory) | Ampere Altra, Neoverse N1 | 78,421 | 129,365 | 191,829 | **1.483** | 1.650 |
| EXP-90's VMs, for comparison | AMD EPYC 7B13 (x86) | 77,739 | 368,299 | 174,592 | 0.474 | 4.738 |
| EXP-90's Mac, for comparison | Apple M5 Pro (Python 3.14) | 176,725 | 275,456 | 366,336 | 1.330 | 1.559 |

The 4-process medians were within 0.3% of these on every machine.

| # | Prediction | Result | Verdict |
|---|---|---|---|
| G1 (deciding) | Axion: SHA-256 / BLAKE3 ≥ 1.00 | 2.970 | **Held** |
| G2 | Altra: SHA-256 / BLAKE3 ≥ 1.00 | 3.092 | **Held** |
| G3 | BLAKE3 / BLAKE2b ≥ 1.5 on ARM | 0.683 and 0.789 | **Failed** |

**Why G3 failed (exploratory, not registered).** BLAKE3's Rust library has a `neon` feature that is off by default ("If "neon" is on, NEON support is assumed"; the library's Cargo.toml). The `blake3` Python package's release workflow (`.github/workflows/dists.yml`) builds its Linux, macOS and Windows ARM wheels without it, so they run BLAKE3's portable code (inferred from the build files; the wheel's binary wasn't inspected). Built from source with `--features neon` on the same Altra, BLAKE3 ran 2.09x as fast (129,365 pages a second), above EXP-29's 1.5x bar over BLAKE2b. SHA-256 still led it by 1.48x. So the sign of SHA-256 against BLAKE3 follows the instruction set even with NEON, while the size of the gap, and BLAKE3 losing to BLAKE2b, come from the package build.

## Conclusion and consequences

- **The Mac's result generalizes to ARM servers:** where the CPU has ARMv8's SHA-2 instructions, SHA-256 is the faster keyed hash per 8 KiB page (1.5x to 3x BLAKE3, depending on the build). On x86 (EXP-29, EXP-90) BLAKE3 is 2.1x SHA-256. The mentor's point holds on ARM.
- **On ARM, `--hash auto` picks the slower hash:** it offers BLAKE3 first, which with the PyPI package hashes at 0.68–0.79x BLAKE2b and a third of SHA-256. pgdelta has no SHA-256 option.
- **No change now.** No hashing run has been bound by CPU (EXP-90: hashing used 3–5% of the CPUs; the disks bind). The idea archive records the ARM finding, with this trigger: an ARM target, or a run bound by CPU.

## Limitations

- **One VM of each ARM type, one session, no repeat:** the repeats agreed within 0.3%, far inside any deciding margin, but a second session on other hosts wasn't run.
- **A microbenchmark of the hash alone,** called from Python per page as pgdelta calls it; not a refresh.
- **The NEON arm was added after the registered runs,** on one machine type (Altra), to explain G3. Axion with NEON wasn't run.
- **The Mac's BLAKE3** (275,456 pages a second, from a wheel built the same way) is far faster than either ARM server's; the cores differ, and this experiment doesn't explain it.
