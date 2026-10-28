"""--selector (EXP-74; research loop 1's idea 4): once the guard has run, the sender knows the fast path's plan, every
page it will read and write. From it this predicts the fast path's time at each fill (fastpath.fill_gaps) and a full
copy's, from each disk's two limits, and the sender takes the fastest: that fill, or every listed file sent whole.

The model is EXP-74's (analysis/fastpath_model.py). Tested on cells it hadn't seen, it put every fast arm within
2.6% and both full copies within 0.1%, and picked the fastest arm measured in both cells:
- **requests:** the sender's runs (at most max_run pages), each reaching the disk as requests of at most 168 KiB
  (EXP-70);
- **each disk's time:** the larger of its requests at its operation rate (Disks.read_ops on the source,
  Disks.write_ops on the destination) and its bytes at Disks.bytes_per_second;
- **the transfer:** the busier disk;
- **the rest:** the guard, already spent, plus OVERHEAD_S and OVERHEAD_S_PER_MB per megabyte sent (fitted on
  EXP-70);
- **a full copy:** FULL_FIXED_S plus every listed file's bytes at Disks.bytes_per_second (EXP-70's full copy).

The default limits are EXP-74's pair's: n2d-highcpu-8 VMs with 150 GiB balanced persistent disks. The request count
was 33-41% high for long runs there (those plans were bound by bytes, so it didn't matter). A full copy here is
pgdelta's own, every file sent whole with B unread. EXP-74 timed `pg_basebackup` instead.
"""
from __future__ import annotations

from dataclasses import dataclass

FILLS = (0, 1, 2, 4)
REQUEST_BYTES = 168 << 10  # the block layer's requests (EXP-70, inferred from fill 4)
OVERHEAD_S = 0.6  # the session beyond the guard and the busier disk (EXP-70's hot and scattered cells)
OVERHEAD_S_PER_MB = 0.00097  # plus this per MB sent
FULL_FIXED_S = 5.7  # a full copy beyond its bytes (EXP-70: 91.02 s, both disks busy 85.3 s)
FULL = 'full'


@dataclass(frozen=True)
class Disks:
    """The source's reads and the destination's writes per busy second, and either disk's bytes per second."""
    read_ops: float = 4100.0  # EXP-68, EXP-70
    write_ops: float = 4790.0  # EXP-70's post-hoc check
    bytes_per_second: float = 210e6  # EXP-70's full copy; EXP-66: 205.5 MB/s

    def __post_init__(self):
        for name in ('read_ops', 'write_ops', 'bytes_per_second'):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not value > 0:
                raise ValueError(f'{name} must be a positive number, not {value!r}')


def segments(pages):
    """Sorted pages as their maximal runs of consecutive pages: (lengths, gaps), gaps[i] the clean pages between run
    i and run i + 1. Each fill's plan follows from these alone (filled), so the pages are walked once."""
    lengths, gaps = [], []
    start = previous = None
    for page in pages:
        if previous is not None and page == previous + 1:
            previous = page
            continue
        if previous is not None:
            lengths.append(previous - start + 1)
            gaps.append(page - previous - 1)
        start = previous = page
    if previous is not None:
        lengths.append(previous - start + 1)
    return lengths, gaps


def _requests(length, max_run, max_request):
    """The requests a stretch of `length` consecutive pages reaches the disk as: runs of at most max_run pages, each
    split into requests of at most max_request."""
    whole_runs, rest = divmod(length, max_run)
    return whole_runs * -(-max_run // max_request) + -(-rest // max_request)


def filled(lengths, gaps, gap, max_run, max_request):
    """(pages, requests) of a file's plan after fastpath.fill_gaps(pages, gap), from its segments."""
    if not lengths:
        return 0, 0
    pages = requests = 0
    current = lengths[0]
    for between, length in zip(gaps, lengths[1:]):
        if between <= gap:
            current += between + length
        else:
            pages += current
            requests += _requests(current, max_run, max_request)
            current = length
    return pages + current, requests + _requests(current, max_run, max_request)


def predict(pages, requests, whole_bytes, guard_s, block, disks):
    """The fast path's seconds: `pages` chosen pages in `requests` requests, and `whole_bytes` sent whole."""
    data = pages * block + whole_bytes
    source = max(requests / disks.read_ops, data / disks.bytes_per_second)
    dest = max(requests / disks.write_ops, data / disks.bytes_per_second)
    return guard_s + OVERHEAD_S + OVERHEAD_S_PER_MB * data / 1e6 + max(source, dest)


def predict_full(listed_bytes, disks):
    return FULL_FIXED_S + listed_bytes / disks.bytes_per_second


def choose(pages_by_file, whole_bytes, listed_bytes, guard_s, block, max_run, disks=Disks(), fills=FILLS):
    """({'fill0': seconds, ..., 'full': seconds}, the fastest: a fill's pages, or None for a full copy). On a tie the
    first listed wins: less filling, and a full copy last. `pages_by_file`: each file's sorted unfilled pages."""
    max_request = max(1, REQUEST_BYTES // block)
    shapes = [segments(pages) for pages in pages_by_file]
    candidates = []
    for gap in fills:
        pages = requests = 0
        for lengths, gaps in shapes:
            p, r = filled(lengths, gaps, gap, max_run, max_request)
            pages, requests = pages + p, requests + r
        candidates.append((f'fill{gap}', gap, predict(pages, requests, whole_bytes, guard_s, block, disks)))
    candidates.append((FULL, None, predict_full(listed_bytes, disks)))
    _, fill, _ = min(candidates, key=lambda c: c[2])
    return {name: seconds for name, _, seconds in candidates}, fill
