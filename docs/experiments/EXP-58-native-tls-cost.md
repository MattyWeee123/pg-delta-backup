# EXP-58: What does pgdelta's own TLS cost at DR rates?

Status: **pre-registered October 27, 2026, before any run.** Lean: one default pair, about 40 minutes. Owner decision O6 requires the data stream encrypted. Native TLS was built on October 27 (063831e), and its cost was the open half of O6's goal.

## Question

pgdelta now runs its connection inside mutual TLS 1.3 when given `--tls-cert`, `--tls-key` and `--tls-ca` on both ends. Its tests show nothing crosses in clear, and that a peer from another CA, or one without TLS, is refused. They don't show what encryption costs. EXP-13 priced TLS only through an stunnel pair, an extra process and hop on each end. Does pgdelta's own TLS slow a DR refresh, and does it add bytes?

## Design

The default `n2-standard-8` pair with EXP-60's conditions:
- BBR on v1's data socket, tuned TCP buffers, realistic rows;
- `blake3` installed on both ends before the sweep.

| Setting | Value |
|---|---|
| Arms | `pgdelta_direct_inplace_bbr_b3` (v1, BBR, BLAKE3, in clear, as EXP-60's) and `pgdelta_direct_inplace_bbr_b3_tls` (the same inside pgdelta's TLS; new in `measure.py`); `full` for context |
| Certificates | The session's CA and certificates, made by the harness's `agent.py tls-material` (EC P-256): the receiver presents `server.crt`, the sender `client.crt`, both trusting only the session's CA. A local check showed pgdelta's own TLS contexts complete a mutual TLS 1.3 handshake with them. |
| Pattern | `uniform` (scattered), 1%, scale 70 (2.1 GB): about 610 MB sent, the most bytes of the patterns |
| Conditions | 2 Gbit/s (the O1 bar's DR cell) and 5 Gbit/s, both at 60 ms, tuned (`--conditions 2:60:tuned,5:60:tuned`) |
| Repeats | 2 |
| Order seed | 580 |

Each run's statistics record the TLS session the sender negotiated (version, cipher, peer, handshake time), now kept in its `tool_reported`. **The analysis,** committed before the data: `analysis/exp58.py`, figure `exp58-tls`.

## Predictions

1. **Deciding: at 2 Gbit/s and 60 ms, TLS takes at most 1.10 of clear's time.** TLS 1.3's AES-GCM runs at gigabytes a second a core with AES-NI, and 610 MB at 2 Gbit/s takes about 2.5 s of link time.
2. **At 5 Gbit/s, at most 1.20 of clear's time.** There the link time shrinks to about 1 s, so any per-byte cost on one sending thread shows more.
3. **TLS adds at most 2% to the bytes on the wire:** about 22 bytes of record framing per 16 KiB, plus the handshake.
4. **Every run passes.** Every TLS run negotiated TLS 1.3 with the session's server certificate, and no clear run negotiated TLS.

The sender's handshake time and both VMs' CPU are reported, not predicted. **Rule 11:** a ratio within 0.05 of its bar gets a second session, for that cell only.

## What follows

- **If 1 holds:** O6's goal is met at DR rates. The scorecard's TLS row moves to "Met", and the README says TLS costs at most 10% at 2 Gbit/s, with the measured figure.
- **If 1 fails:** the write-up attributes the cost from the CPU counters and the phase timers: encryption on the sender's thread, decryption on the receiver's, or the handshake. O6 then stays "Partly met", with the cost stated.
- **If 2 fails but 1 holds:** TLS is cheap at DR rates and costs something on fast links. Both are stated.
