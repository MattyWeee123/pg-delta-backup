# EXP-26: Is BBR on pgdelta's socket fair enough to other traffic to be the default?

Status: **pre-registered October 25, 2026, before any run.** One VM pair (`PAIR=fair`, two `n2-standard-8` in `us-central1-b`, as EXP-20/24's), about 2 hours. The open item [EXP-24](EXP-24-dr-with-bbr.md) left: with BBR on its own socket v1 holds at every DR round trip, but BBR isn't pgdelta's default because its fairness to other traffic on a shared link was untested.

## Question

When pgdelta's one data connection uses BBR and shares a bottleneck with other TCP traffic using cubic (Debian's default, and what `pg_basebackup`, replication and most applications use), does it take more from that traffic than a cubic connection in its place would?

## The bar

The harm-based bar for deploying a new congestion control (Ware, Mukerjee, Seshan and Sherry, "Beyond Jain's Fairness Index: Setting the Bar for the Deployment of Congestion Control Algorithms", HotNets 2019): a newcomer is acceptable if it harms the existing traffic no more than that traffic's own algorithm would in its place. Equal shares aren't the bar, since cubic flows aren't equal to each other either (different round trips, the slow mode of EXP-21 and EXP-23).

**The measure.** In each cell, the existing traffic is N cubic flows. Two newcomers take turns: a cubic flow (the reference) and a BBR flow. With C_cubic and C_bbr the competitors' total received rate while each newcomer runs (medians over repeats), the harm is **H = 1 − C_bbr / C_cubic**. H ≤ 0 means a BBR newcomer leaves the competitors at least as much as a cubic one does. H = 0.25 means they lose a quarter more of their rate to it.

**The threshold: H ≤ 0.20.** This is rule 11's 20% margin, the drift EXP-16 measured between sessions. Every verdict is also reported at 0.10 and 0.40 (half and twice), as K7 asks for O4's tolerances.

## Design

- **The link:** `wan.sh`'s tree on both VMs, as in EXP-20/24: 2 Gbit/s, netem delay of half the round trip on each VM, then a tbf shaper with a drop-tail queue. The queue depth is now a factor, set with `QUEUE_MS` so that tc's queue (rate × latency + the 10 ms burst) is exactly the registered depth in round trips. The depth is the factor that decides how BBR v1 shares with loss-based flows in the published measurements: it takes the larger share in shallow buffers and the smaller in deep ones.
- **The hosts:** Debian 12, kernel 6.1 (BBR v1). Tuned TCP buffers (tcp_rmem and tcp_wmem maxima 128 MiB) on both VMs, so one flow can fill 2 Gbit/s at 120 ms. With Debian's defaults one flow can't pass about 0.8 Gbit/s at 60 ms, and the link wouldn't be contested. The source allows BBR for unprivileged sockets, EXP-24's one host setting; its default stays cubic.
- **Flows:** iperf3 3.12 (Debian's package), all source → dest, as pgdelta's data flows. The N competitors (`iperf3 -P N -C cubic`) run from t = 0 to 100 s. The newcomer (`iperf3 -C cubic` or `-C bbr`) joins at t = 20 s and runs to 100 s. The rates are the receiving servers' per-second counts over t = 40–100 s: 60 s, about three of cubic's growth cycles at 60 ms. A ping every 0.2 s from source to dest over the same 60 s measures the queueing delay each newcomer causes. Before each run: `ip tcp_metrics flush all` on both VMs and fresh iperf3 servers.
- **Cells (10):**
  - at 60 ms: queue depth 0.5, 1, 2 and 4 round trips × 1 and 4 cubic competitors (8 cells);
  - at 1 round trip and 1 competitor: 20 ms and 120 ms (2 cells: does the round trip move H?).
- **Repeats:** 3 per newcomer per cell, 60 runs of about 105 s, in an order shuffled with seed 260.
- **Run validity:** a run counts only if both senders got the congestion control they asked for (iperf3's `sender_tcp_congestion`) and each window has at least 55 of its 60 seconds. Runs that don't count are listed with the reason.
- **Analysis committed before the data:** `analysis/exp26.py`; driver `experiments/network/lab_exp26.sh`.

**Why iperf3 stands in for pgdelta.** An iperf3 flow always has data to send, the most a single flow with a given congestion control can take. pgdelta's socket has data to send whenever the link is its bottleneck, which is the DR case (EXP-24: at 20–120 ms, v1's time tracks its bytes over the link). Where pgdelta is held back by hashing or the disk instead, it takes less. So H here is an upper bound on pgdelta's harm, and a verdict of "default" carries over to pgdelta; a verdict of "opt-in" is conservative for the app-limited case.

## Predictions

From the published behaviour of BBR v1 against loss-based flows:

1. At 0.5 round trips, H > 0.20 with 1 and with 4 competitors: BBR v1 keeps its pacing rate while cubic backs off at each loss.
2. At 4 round trips with 1 competitor, H ≤ 0: cubic fills a deep queue that BBR's in-flight cap (about 2 BDP) leaves to it.
3. H with 4 competitors is above H with 1 at every depth: BBR v1's share does not shrink as loss-based flows are added, while a cubic newcomer's does.
4. **Deciding: BBR stays opt-in** (H > 0.20 in at least one cell).
5. The BBR newcomer gets at least the cubic newcomer's rate in every cell at 1 round trip or less.

## What follows

- **If H ≤ 0.20 in every cell:** pgdelta asks for BBR by default on its data socket, falling back to the host default (and saying so) where BBR isn't allowed. A deciding H within 20% of the bar (0.16–0.24) makes the call provisional until a second session repeats that cell (K10, as narrowed).
- **Otherwise: BBR stays opt-in** (`pgdelta send --congestion-control bbr`). The deployment note gives the cells where BBR takes more than cubic would, recommends it for links pgdelta has to itself or that are provisioned for the refresh, and states the harm in the shared cases measured here. The DR verdict of EXP-24 is unchanged either way; it is what BBR buys, not whether to default to it.

## Limitations, known before running

- One emulated bottleneck: a drop-tail FIFO on the sender's egress. Real WAN paths may have AQM (fq_codel or RED, which change the sharing) or policers, and the bottleneck of a real cross-region path is not known. The depths span the range in which the published behaviour changes.
- BBR v1 only, as Linux 6.1 ships it. BBRv3, which was designed to share better with cubic, isn't in the mainline kernel.
- Bulk competitors only. Short flows and latency-sensitive traffic are covered only by the ping's queueing delay, which isn't part of the verdict.
- One newcomer flow, as pgdelta has one data connection; the competitors share one round trip with it.
- One session. A deciding cell near the bar gets a second (K10).

## Smoke

Before the grid, the driver's smoke plan runs one cell (60 ms, 1 round trip, 1 competitor) once per newcomer, to check the pipeline: both congestion controls applied, full windows, readable server intervals. It is not counted.

**Smoke result (October 25, 15:45–15:49 UTC, not counted).** After two harness fixes (ssh read the plan from stdin, so only the first run ran; the iperf3 servers needed `-J` to return their per-second counts as JSON), both runs completed with full windows and the right congestion control. At 60 ms, a 1-round-trip queue and 1 competitor, the cubic competitor got **180 Mbit/s beside a BBR newcomer** (which took 1,683) against **1,396 beside a cubic newcomer** (which took 515): **H = 0.87** from one run each, four times the bar. Data `docs/experiments/data/exp26-cells-smoke.csv`.

## Revision before the grid ran: a lean grid (October 25)

The owner asked that experiments run long only where they must. The registered grid (60 runs of 100 s, about 1 h 45 min) was stopped during its first run, which isn't used. The verdict needs one cell clearly above the bar, and the map of where BBR is safe needs each cell's H, its sign and rough size, not three-repeat precision far from the bar. So, before any grid run:

- **50-second runs:** competitors from 0 to 50 s, the newcomer from 10 s, the window 20–50 s (30 s, about one and a half of cubic's growth cycles at 60 ms).
- **One run per newcomer per cell:** 20 runs, about 20 minutes, in the order seed 260 gives.
- **Extra repeats only near the bar:** a cell whose H falls between 0.10 and 0.40 (half and twice the bar) gets two more runs per newcomer, and its H then comes from the medians.
- **The check on the shorter window:** the lean grid's 60 ms, 1-round-trip, 1-competitor cell is reported beside the smoke's 100-second runs of the same cell (H = 0.87). If the two disagree on which side of the bar the cell falls, the short window isn't trusted and that cell is rerun at 100 s.

The cells, the measure, the bar, the predictions and the verdict rule are unchanged.
