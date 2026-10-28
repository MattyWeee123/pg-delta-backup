# EXP-72: What does pgdelta's own TLS cost on the fixed transport?

Status: **pre-registered October 28, 2026, before any run.** One default VM pair, about 40 minutes. It repeats EXP-58's design on the transport fixed earlier the same day.

## Question

EXP-58 found that pgdelta's own mutual TLS 1.3 cost 1.033 of its in-clear time at 2 Gbit/s and 60 ms, and 1.148 at 5 Gbit/s, with under 1% more bytes. That met O6's goal.

Since then the transport was found to break OpenSSL's rule that one SSL object is never used by two threads at once:
- a session reads on one thread while another writes, and both went through one `ssl.SSLSocket`;
- a full-duplex stress crashed the process (SIGBUS in OpenSSL) within 30 rounds of 64 MiB, three times in three tries;
- CI's TLS session test hung once.

The fix keeps the TLS state in an `ssl.SSLObject` over memory buffers, used only under a lock, and reads and writes the socket outside it (`transport._TlsConnection`; LOG, October 28). Locally it moved one-way bulk data about 2.4 times as fast as before. EXP-58's figures were measured on the old transport, so the docs now call them "likely an upper bound", which is inferred.

**Does the fixed transport still meet EXP-58's bars, and what does TLS cost on it?**

## Design

**EXP-58's design, unchanged except the seed and the transport** (the same harness, arms and command; `pgdelta` at this commit):
- **The default `n2-standard-8` pair in us-central1,** with BBR on v1's data socket, tuned TCP buffers, realistic rows, and `blake3` 1.0.11 on both ends.
- **Arms:**
  - `pgdelta_direct_inplace_bbr_b3`: v1, BBR, BLAKE3, in clear;
  - `pgdelta_direct_inplace_bbr_b3_tls`: the same inside pgdelta's own TLS, with the session's CA and certificates (EC P-256);
  - `full`: for context.
- **Pattern:** `uniform` (scattered), 1%, scale 70 (2.1 GB), about 610 MB sent.
- **Conditions:** 2 and 5 Gbit/s at 60 ms, tuned (`--conditions 2:60:tuned,5:60:tuned`).
- **Repeats:** 2, order seed 720.

The clear arm doesn't touch the changed code: without TLS the transport still uses the socket's own streams. The fast-path changes since EXP-58 apply only with `--fast-path`, which no arm uses.

**The analysis,** committed before the data: `analysis/exp72.py`. It parses the runs as EXP-58's analysis does and reports each cell beside EXP-58's; figure `exp72-tls`, data `docs/experiments/data/exp72-cells.csv`. It was dry-run on EXP-58's own results, where it reproduced EXP-58's cells exactly; the dry run's outputs were removed.

## Predictions

**The model:** encryption itself (AES-256-GCM with AES-NI) is unchanged. The fix removes Python work per TLS record on the reading side: locally, bulk data moved 2.4 times as fast. So TLS should cost no more than in EXP-58, and probably less on the receiver's CPU.

1. **Deciding: at 2 Gbit/s and 60 ms, TLS takes at most 1.10 of clear's time** (EXP-58's bar; it measured 1.033).
2. **At 5 Gbit/s, at most 1.20 of clear's time** (EXP-58's bar; it measured 1.148).
3. **TLS adds at most 2% to the bytes on the wire.**
4. **Every run passes.** Every TLS run negotiated TLS 1.3 with the session's server certificate, and no clear run negotiated TLS.
5. **Controls:** clear and the full copy within 5% of EXP-58's times (5.23 and 4.04 s; 11.38 and 6.52 s).

**Reported beside EXP-58's, not scored:** each ratio, the CPU TLS adds on each VM (EXP-58: +0.69 and +0.57 s at 2 Gbit/s, +0.59 and +0.65 s at 5), and the handshake time. **The expectation,** stated now: both ratios at or below EXP-58's, and less CPU added on the receiver.

**Rule 11:** as in EXP-58, a ratio within 0.05 of its bar gets a second session, for that cell only.

## What follows

- **If 1–4 hold:** the docs give EXP-72's measured cost for the fixed transport, in place of EXP-58's figures and the "likely an upper bound" notes. O6 stays met.
- **If 1 fails:** the fix costs time at DR rates. Its locks and copies get profiled on the VMs (the phase timers and both VMs' CPU), and O6 goes back to "Partly met" until that is fixed.
- **If 2 fails and 1 holds:** TLS stays cheap at DR rates and costs more on fast links than before. Both are stated, and the reading side's per-record work is the first suspect.
