"""Before a run is committed to the public repository: mask addresses and stop on secrets.

Masked in every text file of the run: IPv4 addresses (VMs' internal addresses appear in logs), email
addresses, and home-directory paths. Stopped on: anything that looks like a key or token, which must
never be committed and can't be masked safely.
"""
from __future__ import annotations

import re
from pathlib import Path

TEXT = {'.json', '.jsonl', '.txt', '.md', '.log', '.csv', '.svg'}
MASKS = [
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), '<ip>'),
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'), '<email>'),
    (re.compile(r'/(?:Users|home)/[^/\s"\']+'), '~'),
]
SECRETS = [
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(r'\bAIza[0-9A-Za-z_-]{30,}'),
    re.compile(r'\bya29\.[0-9A-Za-z_-]{20,}'),
    re.compile(r'\bxox[abprs]-[0-9A-Za-z-]{10,}'),
    re.compile(r'"private_key"\s*:'),
]


class SecretFound(Exception):
    pass


def scrub(run_dir: Path) -> dict:
    """Mask addresses in place; raise SecretFound if a secret is present. Returns what was masked."""
    masked = {}
    for path in sorted(Path(run_dir).rglob('*')):
        if not path.is_file() or path.suffix not in TEXT:
            continue
        text = path.read_text(errors='replace')
        for pattern in SECRETS:
            if pattern.search(text):
                raise SecretFound(f'{path.name} looks like it contains a secret ({pattern.pattern[:20]}…); '
                                  'not publishing')
        new = text
        for pattern, replacement in MASKS:
            new, count = pattern.subn(replacement, new)
            if count:
                masked[path.name] = masked.get(path.name, 0) + count
        if new != text:
            path.write_text(new)
    return masked
