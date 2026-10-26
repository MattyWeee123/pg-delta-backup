# EXP-56: Do sub-page deltas, as built, deliver EXP-54's bytes and time on a DR link?

Status: **pre-registered October 27, 2026, before any measurement of the build.** The build of the design EXP-55 settled, then two stages: a local check (`pgdelta local`, scale 70) and one VM session at 0.5 and 1 Gbit/s. The local stage gates the VM stage.

## Why

EXP-54 found that changed 512-byte blocks plus zstd send 0.21 of zstd's bytes for scattered updates. EXP-55 priced what that costs beyond bytes:
- B's block digests can go up front with the leaf digests, so there's no extra round trip;
- hashing the blocks takes about 1.5 CPU-seconds;
- block writes cost about the same as whole runs.

The owner left the choice of next item open. This one is opt-in, so it changes nothing for anyone who doesn't ask for it, and everything up to the VM run is local.

## The build (opt-in: `--subpage` on `send` and `local`)

**Negotiation.** HELLO carries `subpage_offer: 512`. The receiver accepts (`ACCEPT subpage: 512`) only when it reuses B and builds on B's bytes: in place, or `copy-patch`. Otherwise it leaves `subpage` out of ACCEPT, and the session runs as before. No other session's bytes change.

**B's block digests.** With sub-page accepted, the receiver follows every LEAF_DIGESTS with BLOCK_DIGESTS (new message 12) for the same nodes:
- for each page of B there, the keyed BLAKE2b digest (8 bytes) of each of its 16 blocks of 512 bytes;
- hashed on the receiver's worker processes.

**The sender's frames.** For each page it sends, the sender hashes the bytes it is about to send in the same blocks and compares them with B's. It sends a PAGE_DELTA frame (13; compressed, PAGE_DELTA_ZSTD, 14), with the same head as PAGE_DATA and then, per page:
- a 2-byte bitmap of the blocks that differ, then those blocks;
- bitmap 0xFFFF: the whole page. This is used where B has no full page at that offset, where every block differs, and for a file's short last page;
- bitmap 0: no blocks (the page equals B's).

**The receiver's writes.** It writes the changed blocks into B's page in place, or into O after copying B's bytes ahead (copy-then-patch). It counts each such page as written, so verification re-hashes the whole page and the file digest still has to match S's. A frame that claims more blocks than it carries, a block outside a full page of B, or pages out of order is refused.

## Stage 1: local (decides whether stage 2 runs)

`experiments/bytes/exp56.py`: `pgdelta local --in-place --source-kind datadir --jobs 4` on EXP-51's scale-70 data, with fresh APFS clones of B. Four arms, 3 repeats in a rotated order after a warm-up:
- `zstd`;
- `zstd_subpage`;
- `plain`;
- `subpage`.

After every run, every file pgdelta sends is compared with S's (EXP-51's check), plus EXP-51's negative control.

1. **Correctness (gates everything):** every run completes, and every copy is byte-identical to S.
2. **Bytes match EXP-54/55** within 10%:
   - `zstd_subpage` sends, source to dest, EXP-54's 34.6 MB of compressed blocks plus the page-data frame headers;
   - dest to source, about 17.2 MB of block digests (EXP-55's up-front design).

## Stage 2: one VM session (runs only if stage 1's 1 holds)

EXP-52's pair and command (uniform 1%, hex filler, 60 ms tuned, BBR), at `0.5:60:tuned` and `1:60:tuned`. Methods: `full`, v1 + zstd (`pgdelta_direct_inplace_bbr_z`) and v1 + zstd + sub-page (a new arm, `pgdelta_direct_inplace_bbr_zs`). 2 repeats, order seed 560.

3. **Deciding: at 0.5 Gbit/s, v1 + zstd + sub-page takes at most 0.80 of v1 + zstd's time.**
   - At 0.5 Gbit/s v1 + zstd took 6.8 s (EXP-46), about 3.4 s of it on the link for 210 MB.
   - Sub-page sends about 52 MB in all, about 0.8 s on the link.
   - If its hashing doesn't eat the difference, that projects to about 4.5 s (0.66).
4. At 1 Gbit/s it takes at most 0.95 of v1 + zstd's time. The saving there is about 1.3 s of link time, against hashing that costs about the same on 8 vCPUs.
5. Every run passes pgdelta's own verification, and BBR is in effect on every v1 run.

Rule 11 applies to 3: a ratio within 20% of 0.80 gets a second session on fresh VMs.

## What follows

- **If 1–3 hold:** `--subpage` stays opt-in and its row goes in the README. Advice: use it with `--compress zstd` on slow DR links.
- **If 1 fails:** the build is fixed or withdrawn before anything else.
- **If 3 fails:** the code stays, opt-in, with the measured numbers. The analysis looks first at the extra hashing.
