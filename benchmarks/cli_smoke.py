"""Exercise the consolidated public CLI and restore both outputs in CI."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

from benchmarks.backup import atomic_json
from benchmarks.native_baseline import Trial, preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output must be new')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tools = preflight(None)
    tools['python'] = sys.executable
    root = Path(tempfile.mkdtemp(prefix='pdb-cli-'))
    trial = Trial(root, args.output, tools, SimpleNamespace(rows=200, change_fraction=.1))
    results = []
    try:
        trial.setup()
        basis = None
        for method in ('full', 'incremental'):
            if basis is not None:
                trial.change()
            expected = trial.state(trial.socket, 'expected-' + method)
            command = ['-m', 'benchmarks.benchmark', '--host', str(trial.socket),
                       '--user', 'bench', '--out', str(root / 'runs'),
                       '--method', 'pg_basebackup_' + method]
            if basis is not None:
                command += ['--basis', str(basis)]
            _, stdout = trial.command('python', command)
            result = json.loads(stdout.read_text())
            if result['status'] != 'PASS_MANIFEST_ONLY' or result['restore_tested']:
                raise RuntimeError('CLI confused manifest and restore verification')
            run = root / 'runs' / result['run_id']
            evidence = args.output / method
            evidence.mkdir()
            for file in run.iterdir():
                if file.is_file():
                    shutil.copyfile(file, evidence / file.name)
            restored = trial.restore(run / 'backup', 'cli-' + method, expected)
            results.append({'method': method, 'backup_result': result, 'restore': restored})
            basis = run / 'backup'
        atomic_json(args.output / 'cli-results.json', {'status': 'pass', 'trials': results})
    finally:
        trial.cleanup()
        shutil.rmtree(root)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
