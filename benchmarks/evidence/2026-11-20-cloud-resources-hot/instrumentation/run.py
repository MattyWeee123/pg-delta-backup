"""Run the existing cloud benchmark with additional memory samples and automatic VM cleanup."""
import argparse
import hashlib
import json
import os
import signal
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from bench import config, gcp
from bench.runner import execute


def code_tarball(path):
    with tarfile.open(path, 'w:gz') as archive:
        for item in (*gcp.CODE, 'benchmarks/__init__.py', 'benchmarks/resources.py'):
            source = HERE/'util_series_memory.py' if item == 'experiments/network/util_series.py' else REPO/item
            archive.add(source, arcname=item, filter=lambda entry: None if '__pycache__' in entry.name else entry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--change', choices=('hot', 'uniform'), default='hot')
    parser.add_argument('--scale', type=int, default=50)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    os.environ['CLOUDSDK_CORE_PROJECT'] = args.project
    cfg = config.load(where='gcp', scale=args.scale, repeats=args.repeats, change=args.change,
                      link='1gbit', name='cloud-resources-'+args.change+'-20261120')
    check = gcp.preflight(cfg)
    print(json.dumps({'estimate': check['estimate'], 'problems': check['problems']}, indent=2), flush=True)
    if check['problems']:
        raise RuntimeError('GCP preflight failed; no VMs created')
    gcp._code_tarball = code_tarball
    # Verify the full backup too. Its restore checks run outside the measured interval.
    original = gcp._lane_pg_basebackup

    def full_checked(src, dst, path, lane_id):
        times, result = original(src, dst, path, lane_id)
        pg = gcp.PGBIN
        expect = src.ssh(f'{pg}/psql -h /tmp -U postgres -qAt postgres -c "SELECT count(*), sum(abalance) FROM pgbench_accounts"').strip()
        actual = dst.ssh(f'''set -e
{pg}/pg_verifybackup /data/full > /dev/null
{pg}/pg_ctl -D /data/full -l /data/full-check.log -w start -o '-p 5434 -c listen_addresses= -c unix_socket_directories=/tmp' > /dev/null
{pg}/psql -h /tmp -p 5434 -U postgres -qAt postgres -c "SELECT count(*), sum(abalance) FROM pgbench_accounts"
{pg}/pg_ctl -D /data/full -m fast -w stop > /dev/null
rm -rf /data/full''').strip()
        result.update(copy=actual, match=actual == expect)
        if not result['match']:
            raise RuntimeError('Full backup restore differs from source')
        return times, result

    gcp._lane_pg_basebackup = full_checked
    def timed_out(*_):
        raise TimeoutError('20-minute resource test limit reached')

    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(20 * 60)
    directory = execute(cfg, work=Path('/tmp/unused-cloud-work'), pgbin=None, publish=False,
                        estimate=check['estimate'], keep_vms=False)
    signal.alarm(0)
    provenance = {'sampler_sha256': hashlib.sha256((HERE/'util_series_memory.py').read_bytes()).hexdigest(),
                  'memory_metric': 'summed RSS of matching backup processes; shared pages can be counted multiple times',
                  'memory_record': 'M epoch total available cached dirty writeback matched_rss matched_process_count',
                  'sampling_interval_s': 0.1}
    (directory/'resource-provenance.json').write_text(json.dumps(provenance, indent=2)+'\n')
    print('Completed cloud run:', directory, flush=True)


if __name__ == '__main__':
    main()
