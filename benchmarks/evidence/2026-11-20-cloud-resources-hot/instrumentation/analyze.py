"""Summarize VM counters inside each recorded backup interval."""
import argparse
import bisect
import json
import re
import statistics
from pathlib import Path


def at(rows, t):
    times = [row[0] for row in rows]
    i = bisect.bisect_right(times, t) - 1
    if i < 0 or i >= len(rows)-1:
        raise ValueError('Resource samples do not bracket the measured interval')
    left, right = rows[i], rows[i+1]
    weight = (t-left[0])/(right[0]-left[0])
    return [a+(b-a)*weight for a,b in zip(left[1:],right[1:])]


def summarize(path, start, end):
    text = path.read_text()
    header = re.search(r'hz=(\d+) cpus=(\d+)',text)
    hz, cpus = map(int,header.groups())
    samples, memory = [], []
    for line in text.splitlines():
        fields=line.split()
        if fields and fields[0]=='S':
            samples.append([float(fields[1]),*map(int,fields[2:11])])
        if fields and fields[0]=='M' and start <= float(fields[1]) <= end:
            memory.append([float(fields[1]),*map(int,fields[2:])])
    if not memory:
        raise ValueError('No memory samples in timed window')
    before, after = at(samples,start), at(samples,end)
    rx, tx, reads, sectors_read, writes, sectors_written, io_ms, busy, total = [b-a for a,b in zip(before,after)]
    duration=end-start
    return {'duration_s':duration,'cpu_core_seconds':busy/hz,'average_cpu_percent':100*busy/total,
            'network_tx_bytes':tx,'network_rx_bytes':rx,'disk_read_bytes':sectors_read*512,
            'disk_write_bytes':sectors_written*512,'disk_read_ops':reads,'disk_write_ops':writes,
            'disk_busy_percent':100*io_ms/1000/duration,
            'peak_backup_process_rss_bytes':max(row[6] for row in memory),
            'peak_vm_used_available_bytes':max(row[1]-row[2] for row in memory),
            'minimum_vm_available_bytes':min(row[2] for row in memory),'cpu_count':cpus}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir',type=Path)
    args=parser.parse_args()
    rows=[]
    for lane in sorted((args.run_dir/'raw').iterdir()):
        meta=json.loads((lane/'meta.json').read_text())
        if meta.get('status') != 0:
            raise ValueError('Failed lane '+lane.name)
        record={'lane':lane.name,'method':lane.name.rsplit('-r',1)[0]}
        starts = []
        for side in ('source','dest'):
            first = next(line for line in (lane/f'util-{side}.txt').read_text().splitlines() if line.startswith('S '))
            starts.append(float(first.split()[1]))
        # Include receiver setup/hash work before sender T0 in a separate view.
        setup_start = max(starts) + 0.001
        record['setup_start_epoch'] = setup_start
        record['setup_and_backup'] = {side: summarize(lane/f'util-{side}.txt', setup_start, meta['t1'])
                                      for side in ('source','dest')}
        for side in ('source','dest'):
            record[side]=summarize(lane/f'util-{side}.txt',meta['t0'],meta['t1'])
        rows.append(record)
    (args.run_dir/'resources.json').write_text(json.dumps(rows,indent=2)+'\n')
    methods=('pg_basebackup','pgdelta','pgdelta-inplace')
    def median(method,side,key):
        return statistics.median(row[side][key] for row in rows if row['method']==method)
    lines=['# Cloud resource measurements','',
           'Medians across the recorded repetitions. CPU, disk and network are VM/device counters inside the backup interval. '
           'Counter values at interval boundaries are linearly interpolated between 100 ms samples. '
           'Memory peaks are sampled. Process RSS sums can count shared pages multiple times. '
           'Copy verification after the backup timer is excluded.','',
           '| Method | Runs | Time (s) | Source CPU (core-s) | Destination CPU (core-s) | Source CPU (%) | Destination CPU (%) |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for method in methods:
        values=[median(method,'source','duration_s'),median(method,'source','cpu_core_seconds'),
                median(method,'dest','cpu_core_seconds'),median(method,'source','average_cpu_percent'),
                median(method,'dest','average_cpu_percent')]
        count=sum(row['method']==method for row in rows)
        lines.append(f'| {method} | {count} | '+' | '.join(f'{v:.2f}' for v in values)+' |')
    lines+=['','| Method | Source peak process RSS (MiB) | Destination peak process RSS (MiB) | Source disk read (MB) | Source disk write (MB) | Destination disk read (MB) | Destination disk write (MB) |',
            '|---|---:|---:|---:|---:|---:|---:|']
    for method in methods:
        values=[median(method,side,'peak_backup_process_rss_bytes')/1024**2 for side in ('source','dest')]
        values += [median(method,side,key)/1e6 for side in ('source','dest') for key in ('disk_read_bytes','disk_write_bytes')]
        lines.append(f'| {method} | '+' | '.join(f'{v:.1f}' for v in values)+' |')
    lines+=['','| Method | Source → destination interface TX (MB) | Destination → source interface TX (MB) |',
            '|---|---:|---:|']
    for method in methods:
        values=[median(method,side,'network_tx_bytes')/1e6 for side in ('source','dest')]
        lines.append(f'| {method} | '+' | '.join(f'{v:.2f}' for v in values)+' |')
    lines+=['', '## Observed setup plus backup', '',
            'This view starts when both samplers are available and ends at the backup end. It includes receiver work before sender T0 and SSH/control setup overhead. Trial reset copies made before sampling are excluded.', '',
            '| Method | Source CPU (core-s) | Destination CPU (core-s) | Source disk read (MB) | Destination disk read (MB) | Destination disk write (MB) |',
            '|---|---:|---:|---:|---:|---:|']
    for method in methods:
        group=[row for row in rows if row['method']==method]
        fields=(('source','cpu_core_seconds',1),('dest','cpu_core_seconds',1),
                ('source','disk_read_bytes',1e6),('dest','disk_read_bytes',1e6),('dest','disk_write_bytes',1e6))
        values=[statistics.median(row['setup_and_backup'][side][key]/divisor for row in group)
                for side,key,divisor in fields]
        lines.append(f'| {method} | '+' | '.join(f'{value:.2f}' for value in values)+' |')
    lines+=['','Network counters include all traffic on the measured interfaces, including SSH control traffic. '
            'Use engine protocol counters alongside them. Whole-VM CPU also includes operating-system work. '
            'The sampler extension and its checksum are recorded separately from the engine revision.']
    (args.run_dir/'resources.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__=='__main__':
    main()
