"""One benchmark run, start to finish: record the config, run the lanes, write the report, and publish."""
from __future__ import annotations

import json
import traceback
from pathlib import Path

from bench import report, scrub
from bench.steps import Steps
from bench.config import RunConfig
from viz import runs


def title_of(cfg: RunConfig) -> str:
    where = 'laptop' if cfg.where == 'laptop' else f'GCP {cfg.machine}, link {cfg.link}'
    return f"{' vs '.join(cfg.methods)}, scale {cfg.scale}, {cfg.change} change ({where})"


def execute(cfg: RunConfig, root: Path = runs.DEFAULT_ROOTS[0], *, work: Path = Path('demo-work'),
            pgbin: str | None = None, publish: bool = False, estimate: dict | None = None,
            keep_vms: bool = False, log=print) -> Path:
    """Run `cfg` into root/<name>. The run directory is created first, so its lanes can be watched live."""
    lanes = [{'id': lane['id'], 'label': lane['label']} for lane in cfg.lanes()]
    run_dir = runs.new_run(root, cfg.name, title_of(cfg), lanes, setup=_setup_line(cfg), config=cfg.to_json(),
                           estimate=estimate)
    (run_dir / 'config.json').write_text(json.dumps(cfg.to_json(), indent=2) + '\n')
    steps = Steps(run_dir, cfg, publish=publish, root=root)  # the plan the viewer's progress bar and ETA follow
    meta = json.loads((run_dir / 'meta.json').read_text())
    try:
        if cfg.where == 'laptop':
            from bench.laptop import pg_bin, run_laptop
            facts = run_laptop(cfg, run_dir, work, pg_bin(pgbin), log=log, steps=steps)
        else:
            from bench.gcp import run_gcp
            facts = run_gcp(cfg, run_dir, log=log, keep=keep_vms, steps=steps)
        markdown = report.write(run_dir, cfg, facts, estimate)
        meta.update(finished=True, facts=facts, verify=facts.get('verify'))
        runs.write_meta(run_dir, meta)
        masked = scrub.scrub(run_dir)
        if masked:
            log(f'masked before publishing: {masked}')
        if publish:
            with steps.step('publish'):
                result = _publish(run_dir, cfg, markdown, log)
            meta.update(publish=result)
            runs.write_meta(run_dir, meta)
        return run_dir
    except BaseException as exc:
        meta.update(finished=True, failed=f'{type(exc).__name__}: {exc}')
        runs.write_meta(run_dir, meta)
        (run_dir / 'error.txt').write_text(traceback.format_exc())
        raise


def _publish(run_dir: Path, cfg: RunConfig, markdown: str, log) -> dict:
    from bench.publish import publish
    scrub.scrub(run_dir)  # again, in case the run was edited since it was recorded
    log('committing the run and opening a pull request …')
    body = markdown.split('## Reproduce')[0] + '\n*Recorded with `python -m bench run`; the full run is in this PR.*\n'
    result = publish(run_dir, title_of(cfg), body)
    log(f"pull request: {result['url']}")
    return result


def _setup_line(cfg: RunConfig) -> dict:
    setup = {'where': cfg.where, 'scale': cfg.scale, 'change': f'1% of rows, {cfg.change}', 'repeats': cfg.repeats}
    if cfg.where == 'gcp':
        setup.update(machine=cfg.machine, disk=f'{cfg.disk_gb} GB {cfg.disk_type}', link=cfg.link,
                     zones=' / '.join(cfg.zones))
    return setup
