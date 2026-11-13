"""Turning each method's output into a lane's events (the format of pgdelta/events.py)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pgdelta.events import EventLog

PROGRESS = re.compile(rb'(\d+)/(\d+) kB')


def pump_pg_basebackup(stream, events: EventLog) -> int:
    """Read pg_basebackup's --progress output ('done/total kB') from `stream` until it ends, writing
    progress events. Returns the total bytes it reported."""
    done_kb = total_kb = 0
    buffer = b''
    while True:
        chunk = stream.read1(4096) if hasattr(stream, 'read1') else stream.read(4096)
        if not chunk:
            break
        buffer += chunk
        for match in PROGRESS.finditer(buffer):
            done_kb, total_kb = int(match.group(1)), int(match.group(2))
        buffer = buffer[-64:]
        if events.due():
            events.emit('progress', bytes_sent=done_kb * 1024, total_bytes=total_kb * 1024)
    return total_kb * 1024


def copy_events(stream, path: Path, method: str) -> dict | None:
    """Copy pgdelta's event lines from `stream` into `path` as they arrive, re-timed from this machine's
    clock (so a remote lane lines up with the samplers). Returns the done event, if one came."""
    done = None
    started = time.perf_counter()
    with open(path, 'w') as out:
        for raw in stream:
            line = raw.decode() if isinstance(raw, bytes) else raw
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event['t'] = round(time.perf_counter() - started, 4) if event.get('kind') != 'start' else 0.0
            event['method'] = method
            if event.get('kind') == 'done':
                event['elapsed'] = event['t']
                done = event
            out.write(json.dumps(event, separators=(',', ':')) + '\n')
            out.flush()
    return done


def merge_gauges(path: Path, gauges: list[dict]) -> None:
    """Add sampler gauge events to a lane's event file, in time order."""
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    events += gauges
    events.sort(key=lambda e: (e['t'], e['kind'] != 'start', e['kind'] == 'done'))
    Path(path).write_text(''.join(json.dumps(e, separators=(',', ':')) + '\n' for e in events))
