"""Identify actual benchmark/engine sources, including uncommitted exports."""
import hashlib
from pathlib import Path
import subprocess


def source_provenance(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    files = sorted(path for directory in ('benchmarks', 'pgdelta')
                   for path in (root / directory).rglob('*.py') if path.is_file())
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())

    def git(*arguments):
        try:
            result = subprocess.run(['git', '-C', str(root), *arguments],
                                    capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = git('status', '--porcelain', '--untracked-files=normal')
    return {'code_sha256': digest.hexdigest(), 'python_source_files': len(files),
            'hash_scope': 'sorted benchmarks/ and pgdelta/ Python paths and SHA256 file digests',
            'git_commit': git('rev-parse', 'HEAD'), 'git_branch': git('branch', '--show-current'),
            'working_tree_dirty': None if status is None else bool(status)}
