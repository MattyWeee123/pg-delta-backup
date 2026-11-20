"""Extend the existing VM sampler with memory observations; keep the engine unchanged."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
source = (REPO/'experiments/network/util_series.py').read_text()
source = source.replace('import time\n', 'import time\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[2]))\nfrom benchmarks.resources import _processes\n', 1)
helper = '''
def memory(now):
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        name, rest = line.split(':', 1)
        if name in ('MemTotal', 'MemAvailable', 'Cached', 'Dirty', 'Writeback'):
            values[name] = int(rest.split()[0]) * 1024
    rss = count = 0
    for pid, entry in _processes(Path('/proc')).items():
        try:
            if MATCH.search((entry['path'] / 'cmdline').read_bytes()):
                rss += entry['rss_pages'] * os.sysconf('SC_PAGE_SIZE')
                count += 1
        except OSError:
            continue
    fields = [values.get(name, 0) for name in ('MemTotal', 'MemAvailable', 'Cached', 'Dirty', 'Writeback')]
    return ' '.join(['M', f'{now:.3f}', *map(str, fields), str(rss), str(count)]) + '\\n'

'''
source = source.replace('\ndef main():', '\n'+helper+'\ndef main():', 1)
source = source.replace('        if n % a.threads_every == 0:', '        out.write(memory(now))\n        if n % a.threads_every == 0:', 1)
(HERE/'util_series_memory.py').write_text(source)
compile(source, str(HERE/'util_series_memory.py'), 'exec')
print('Prepared memory sampler extension. Engine source remains unchanged.')
