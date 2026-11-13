"""Run directories: meta.json plus one events-<lane>.jsonl per lane."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = (REPO / 'runs', REPO / 'viz' / 'examples')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise ValueError(f'invalid run name {name!r}: letters, digits, dot, dash and underscore only')
    return name


def write_meta(run_dir: Path, meta: dict) -> None:
    tmp = run_dir / 'meta.json.tmp'
    tmp.write_text(json.dumps(meta, indent=2) + '\n')
    tmp.replace(run_dir / 'meta.json')


def new_run(root: Path, name: str, title: str, lanes: list[dict], **extra) -> Path:
    """Create root/name with its meta.json. `lanes`: [{'id': ..., 'label': ...}, ...] in display order."""
    run_dir = Path(root) / check_name(name)
    run_dir.mkdir(parents=True, exist_ok=False)
    for lane in lanes:
        lane.setdefault('events', f"events-{lane['id']}.jsonl")
    write_meta(run_dir, {'name': name, 'title': title, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                         'finished': False, 'lanes': lanes, **extra})
    return run_dir


def find(roots, name: str) -> Path | None:
    check_name(name)
    for root in roots:
        candidate = Path(root) / name
        if (candidate / 'meta.json').is_file():
            return candidate
    return None


def list_runs(roots) -> list[dict]:
    runs = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for meta_file in sorted(root.glob('*/meta.json')):
            try:
                meta = json.loads(meta_file.read_text())
            except (OSError, ValueError):
                continue
            runs.append({'name': meta_file.parent.name, 'title': meta.get('title', meta_file.parent.name),
                         'created': meta.get('created', ''), 'finished': bool(meta.get('finished')),
                         'lanes': [lane.get('label', lane.get('id')) for lane in meta.get('lanes', [])],
                         'example': root.name == 'examples'})
    runs.sort(key=lambda run: run['created'], reverse=True)
    return runs


def load(run_dir: Path) -> dict:
    """meta.json and every lane's events so far (a lane still being written may end mid-line)."""
    meta = json.loads((run_dir / 'meta.json').read_text())
    lanes = {}
    for lane in meta.get('lanes', []):
        events = []
        path = run_dir / lane['events']
        if path.is_file():
            for line in path.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    break  # a line still being written
        lanes[lane['id']] = events
    return {'meta': meta, 'lanes': lanes}
