"""Disposable PostgreSQL 17 full/incremental baseline and recovery checks.

Only controls clusters created beneath a new temporary directory. This first
runner measures local native backups, not cross-host network performance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

from benchmarks.backup import (atomic_json, basebackup_command, directory_bytes,
                               run_backup, run_command)


TOOLS = ("postgres", "initdb", "pg_ctl", "psql", "pg_basebackup",
         "pg_combinebackup", "pg_verifybackup", "pgbench", "pg_waldump")
TABLES = {"accounts": "id", "ledger": "worker, seq", "markers": "name", "items": "id"}


def controlled_env():
    # Avoid a caller's libpq settings silently changing this disposable experiment.
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Documented nearest-rank estimator, not an average of interval percentiles.
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def parse_pgbench_logs(paths: list[Path], start: float, end: float) -> dict:
    """Default non-aggregated PG17 log; windows use completion timestamps."""
    latency, lag = [], []
    failures = skipped = total_success = 0
    for path in paths:
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise ValueError(f"Malformed pgbench log in {path.name}")
            finished = int(fields[4]) + int(fields[5]) / 1_000_000
            is_success = fields[2].isdigit()
            total_success += is_success
            if not start <= finished < end:
                continue
            if is_success:
                latency.append(int(fields[2]) / 1000)
                if len(fields) > 6:
                    lag.append(int(fields[6]) / 1000)
            elif fields[2] == "skipped":
                skipped += 1
            else:
                failures += 1
    duration = end - start
    if duration <= 0:
        raise ValueError("Workload measurement window must be positive")
    return {
        "window_seconds": duration, "completed": len(latency),
        "completed_tps": len(latency) / duration,
        "p50_ms": percentile(latency, .50),
        "p95_ms": percentile(latency, .95),
        "p99_ms": percentile(latency, .99),
        "schedule_lag_p95_ms": percentile(lag, .95),
        "failures": failures, "skipped": skipped,
        "all_logged_successes": total_success,
        "window_policy": "transaction completion in [start, end)",
        "latency_policy": "pgbench scheduled-start latency including lag",
    }


def same_state(expected: dict, actual: dict) -> bool:
    return expected == actual


def preflight(bin_dir: str | None) -> dict[str, str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("Run as an unprivileged user; initdb refuses root")
    tools = {}
    versions = {}
    for name in TOOLS:
        executable = str(Path(bin_dir) / name) if bin_dir else shutil.which(name)
        if not executable or not Path(executable).is_file():
            raise ValueError(f"Missing PostgreSQL binary: {name}")
        tools[name] = executable
        version = subprocess.check_output([executable, "--version"], text=True)
        match = re.search(r"\(PostgreSQL\) (17(?:\.[0-9]+)*)", version)
        if not match:
            raise ValueError(f"This runner requires PostgreSQL 17: {version.strip()}")
        versions[name] = match.group(1)
    if len(set(versions.values())) != 1:
        raise ValueError(f"Server/client versions differ: {versions}")
    return tools


class Trial:
    def __init__(self, root: Path, output: Path, tools: dict, args):
        self.root, self.output, self.tools, self.args = root, output, tools, args
        output.mkdir()
        self.source = root / "source"
        self.socket = root / "socket"
        self.archive = root / "archive"
        self.socket.mkdir()
        self.archive.mkdir()
        self.started: list[Path] = []
        self.workload = None
        self.workload_files = None
        self.commands = []
        self.counter = 0

    def command(self, name, arguments=(), *, sql=None, check=True, timeout=120):
        self.counter += 1
        prefix = self.output / f"{self.counter:03d}-{name}"
        argv = [self.tools.get(name, name), *map(str, arguments)]
        phase = run_command(argv, prefix.with_suffix('.out'), timeout,
                            stderr_path=prefix.with_suffix('.err'), input_text=sql,
                            env=controlled_env())
        record = {"argv": argv, "seconds": phase['duration_seconds'],
                  "returncode": phase['exit_code'], "error": phase['error'],
                  "stdout": prefix.with_suffix(".out").name,
                  "stderr": prefix.with_suffix(".err").name}
        self.commands.append(record)
        atomic_json(self.output / "commands.json", self.commands)
        if check and (record['returncode'] != 0 or record['error']):
            raise RuntimeError(f"{name} failed ({record['returncode']}): "
                               + prefix.with_suffix(".err").read_text()[-3000:])
        return record, prefix.with_suffix(".out")

    def sql(self, statement, socket=None):
        _, path = self.command("psql", ["-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                              "-h", socket or self.socket, "-p", "5432", "-U", "bench", "-d", "postgres"], sql=statement)
        return path.read_text().strip()

    def start(self, data, socket, target=None):
        socket.mkdir(exist_ok=True)
        options = ["-k", str(socket), "-h", "", "-p", "5432",
                   "-c", "archive_mode=off", "-c", "primary_conninfo=",
                   "-c", "restore_command="] if data != self.source else ["-k", str(socket), "-h", "", "-p", "5432"]
        if target:
            options += ["-c", "restore_command=" + shlex.join([sys.executable,
                str(Path(__file__).with_name("wal_copy.py")), "restore",
                str(self.archive / "%f"), "%p"]),
                "-c", "recovery_target_name=" + target,
                "-c", "recovery_target_timeline=current",
                "-c", "recovery_target_action=pause", "-c", "hot_standby=on"]
        self.started.append(data)  # Also attempt cleanup if startup times out.
        self.command("pg_ctl", ["-D", data, "-l", self.output / (data.name + "-server.log"),
                                "-o", shlex.join(options), "-w", "-t", "45", "start"])
        actual = self.sql("SHOW data_directory;", socket)
        if Path(actual).resolve() != data.resolve():
            raise RuntimeError("Connected to the wrong cluster")

    def stop(self, data):
        self.command("pg_ctl", ["-D", data, "-m", "fast", "-w", "-t", "30", "stop"], check=False)
        status, _ = self.command("pg_ctl", ["-D", data, "status"], check=False)
        if status["returncode"] != 3:
            self.command("pg_ctl", ["-D", data, "-m", "immediate", "-w", "-t", "10", "stop"], check=False)
            status, _ = self.command("pg_ctl", ["-D", data, "status"], check=False)
            if status["returncode"] != 3:
                raise RuntimeError(f"Could not stop owned cluster: {data}")
        if data in self.started:
            self.started.remove(data)

    def setup(self):
        self.command("initdb", ["-D", self.source, "--username=bench", "--auth-local=trust",
                              "--auth-host=reject", "--data-checksums", "--encoding=UTF8", "--locale=C"])
        archive_command = shlex.join([sys.executable, str(Path(__file__).with_name("wal_copy.py")),
                                     "archive", "%p", str(self.archive / "%f")])
        config = "\n".join(["listen_addresses = ''", "shared_buffers = '128MB'",
            "wal_level = replica", "max_wal_senders = 10", "max_replication_slots = 10",
            "summarize_wal = on", "wal_summary_keep_time = '1d'", "archive_mode = on",
            "archive_command = '" + archive_command.replace("'", "''") + "'",
            "fsync = on", "full_page_writes = on", "synchronous_commit = on",
            "autovacuum = on", "logging_collector = off"])
        with (self.source / "postgresql.conf").open("a") as stream:
            stream.write("\n" + config + "\n")
        shutil.copyfile(self.source / "postgresql.conf", self.output / "source-postgresql.conf")
        self.start(self.source, self.socket)
        self.sql(f"""
CREATE SCHEMA bench;
CREATE TABLE bench.accounts(id bigint PRIMARY KEY, balance bigint NOT NULL, payload text NOT NULL);
CREATE TABLE bench.ledger(worker integer, seq bigint, a bigint NOT NULL, b bigint NOT NULL,
                          PRIMARY KEY(worker, seq));
CREATE TABLE bench.markers(name text PRIMARY KEY, value text NOT NULL);
CREATE TABLE bench.items(id integer PRIMARY KEY, value text NOT NULL);
INSERT INTO bench.accounts SELECT i, 1000000, repeat(md5(i::text), 8)
  FROM generate_series(1, {self.args.rows}) i;
INSERT INTO bench.markers VALUES ('phase', 'basis');
INSERT INTO bench.items VALUES (1, 'delete me'), (2, 'keep me');
VACUUM ANALYZE bench.accounts;
""")

    def backup(self, output, basis=None):
        argv = basebackup_command(self.tools['pg_basebackup'], output,
                                  host=self.socket, port=5432, user='bench', basis=basis)
        record, _ = self.command("pg_basebackup", argv[1:])
        shutil.copyfile(output / "backup_manifest", self.output / (output.name + "-manifest.json"))
        return record["seconds"]

    def verify(self, path, *, check=True):
        record, _ = self.command("pg_verifybackup", [path], check=check)
        return record

    def backup_phase(self, stage, argv):
        record, _ = self.command(Path(argv[0]).name, argv[1:], check=False)
        return {'exit_code': record['returncode'], 'duration_seconds': record['seconds'],
                'error': record['error'], 'stdout': record['stdout'], 'stderr': record['stderr']}

    def change(self):
        changed = max(1, int(self.args.rows * self.args.change_fraction))
        # A fixed permutation spreads selected keys across the table.
        self.sql(f"""
UPDATE bench.accounts SET payload = 'changed:' || payload
 WHERE (id * 48271) % {self.args.rows} < {changed};
INSERT INTO bench.items VALUES (3, 'inserted after basis');
DELETE FROM bench.items WHERE id = 1;
UPDATE bench.markers SET value = 'target' WHERE name = 'phase';
BEGIN;
INSERT INTO bench.markers VALUES ('rolled_back', 'must not survive');
ROLLBACK;
""")

    def state(self, socket, label):
        result = {}
        for table, order in TABLES.items():
            _, path = self.command("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", socket,
                                            "-p", "5432", "-U", "bench", "-d", "postgres"],
                sql=f"COPY (SELECT * FROM bench.{table} ORDER BY {order}) TO STDOUT WITH (FORMAT csv, NULL '\\N', FORCE_QUOTE *);")
            result[table] = {"sha256": sha256(path), "canonical_bytes": path.stat().st_size,
                            "rows": int(self.sql(f"SELECT count(*) FROM bench.{table};", socket))}
            # Keep hashes and commands; large canonical data is reproducible from the fixture.
            path.unlink()
        schema = self.sql("""SELECT json_agg(x ORDER BY tab, num)::text FROM
 (SELECT c.relname AS tab, a.attnum AS num, a.attname AS name,
         format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  JOIN pg_attribute a ON a.attrelid=c.oid
  WHERE n.nspname='bench' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped) x;
SELECT json_agg(x ORDER BY tab, name)::text FROM
 (SELECT c.relname AS tab, con.conname AS name, pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid
  JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='bench') x;""", socket)
        result["schema_sha256"] = hashlib.sha256(schema.encode()).hexdigest()
        result["balance_total"] = int(self.sql("SELECT sum(balance) FROM bench.accounts;", socket))
        atomic_json(self.output / (label + "-state.json"), result)
        return result

    def launch_workload(self):
        script = self.root / "workload.sql"
        script.write_text(f"""\\set a random(1, {self.args.rows})
\\set b (:a % {self.args.rows}) + 1
\\set seq :seq + 1
BEGIN;
SELECT id FROM bench.accounts WHERE id IN (:a, :b) ORDER BY id FOR UPDATE;
UPDATE bench.accounts SET balance = balance + CASE WHEN id = :a THEN 1 ELSE -1 END
 WHERE id IN (:a, :b);
INSERT INTO bench.ledger VALUES (:client_id, :seq, :a, :b);
COMMIT;
""")
        argv = [self.tools["pgbench"], "-h", str(self.socket), "-p", "5432", "-U", "bench", "-n",
                "-c", "2", "-j", "2", "-T", str(self.args.duration), "-R", str(self.args.rate),
                "--random-seed=" + str(self.args.seed), "--max-tries=1", "-D", "seq=0",
                "-l", "--log-prefix=" + str(self.output / "pgbench-log"), "-f", str(script), "postgres"]
        self.workload_files = [(self.output / "pgbench.out").open("w"), (self.output / "pgbench.err").open("w")]
        self.workload_started = time.time()
        self.workload = subprocess.Popen(argv, stdout=self.workload_files[0], stderr=self.workload_files[1], env=controlled_env())
        self.commands.append({"argv": argv, "kind": "background_workload"})
        # Readiness is real completed work, not an arbitrary sleep.
        deadline = time.monotonic() + 10
        while int(self.sql("SELECT count(*) FROM bench.ledger;")) < 10:
            if self.workload.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Workload did not become ready")
            time.sleep(.05)

    def finish_workload(self):
        result = self.workload.wait(timeout=self.args.duration + 30)
        self.workload_finished = time.time()
        for file in self.workload_files:
            file.close()
        self.workload_files = None
        if result:
            raise RuntimeError("pgbench failed: " + (self.output / "pgbench.err").read_text())
        if self.sql("SELECT count(*) FROM pg_stat_activity WHERE datname='postgres' AND application_name='pgbench';") != "0":
            raise RuntimeError("Workload writers did not drain")

    def restore(self, backup, label, expected=None, target=None):
        restored = self.root / label
        socket = self.root / (label + "-sock")
        started = time.perf_counter()
        shutil.copytree(backup, restored)
        # Only these controlled fixtures are supported, never arbitrary PGDATA.
        for name in ("standby.signal", "recovery.signal"):
            (restored / name).unlink(missing_ok=True)
        if target:
            (restored / "recovery.signal").touch()
        self.start(restored, socket, target)
        if target:
            deadline = time.monotonic() + 45
            while self.sql("SELECT pg_is_wal_replay_paused();", socket) != "t":
                if time.monotonic() >= deadline:
                    raise RuntimeError("Recovery did not pause at the requested target")
                time.sleep(.05)
            server_log = (self.output / (label + "-server.log")).read_text()
            if target not in server_log:
                raise RuntimeError("Restore log did not identify the requested target")
        elif self.sql("SELECT pg_is_in_recovery();", socket) != "f":
            raise RuntimeError("Package-only recovery did not complete")
        actual = self.state(socket, label)
        if expected is not None and not same_state(expected, actual):
            raise RuntimeError("Restored logical data/schema differs from independent source export")
        if actual["balance_total"] != self.args.rows * 1000000:
            raise RuntimeError("Atomic transfer conservation invariant failed")
        self.stop(restored)
        shutil.rmtree(restored)
        return {"seconds": time.perf_counter() - started, "startup": "pass",
                "logical_match": "pass" if expected is not None else "not_checked",
                "conservation": "pass", "target": target, "state": actual}

    def marker(self):
        target = "baseline_" + uuid.uuid4().hex
        lsn = self.sql(f"SELECT pg_create_restore_point('{target}');")
        segment = self.sql(f"SELECT pg_walfile_name('{lsn}');")
        self.sql("SELECT pg_switch_wal();")
        deadline = time.monotonic() + 45
        while not (self.archive / segment).is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("Target WAL segment was not durably archived")
            time.sleep(.05)
        return target, lsn

    def cleanup(self):
        if self.workload and self.workload.poll() is None:
            self.workload.terminate()
            try:
                self.workload.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.workload.kill()
                self.workload.wait(timeout=5)
        if self.workload_files:
            for file in self.workload_files:
                file.close()
        for data in reversed(self.started[:]):
            self.stop(data)


def run_trial(trial: Trial, method: str, mode: str, negative_controls: bool) -> dict:
    trial.setup()
    basis = trial.root / "basis"
    trial.backup(basis)
    basis_check = trial.verify(basis)
    basis_manifest_sha = sha256(basis / "backup_manifest")
    trial.change()
    record = {"method": method, "mode": mode, "status": "running",
              "measurement_kind": "local_native_backup", "wire_bytes": None,
              "unavailable_metrics": {"wire_bytes": "Unix socket transfer on one host",
               "source_cpu_seconds": "server process monitoring not implemented",
               "disk_io_bytes": "device I/O monitoring not implemented",
               "peak_rss_bytes": "process-group monitoring not implemented"}}
    record["database_bytes"] = int(trial.sql("SELECT pg_database_size('postgres');"))
    record["system_identifier"] = trial.sql("SELECT system_identifier FROM pg_control_system();")
    record["basis_bytes"] = directory_bytes(basis)
    if mode == "active":
        trial.launch_workload()
    else:
        expected = trial.state(trial.socket, "expected")
    start = time.perf_counter()
    start_wall = time.time()
    output = trial.root / "output"
    if method in ('full', 'incremental'):
        backup_result, code = run_backup(
            'pg_basebackup_' + method, run_id=trial.output.name, output=output,
            tools=trial.tools, execute=trial.backup_phase, host=trial.socket, user='bench',
            basis=basis if method == 'incremental' else None,
            increment=trial.root / 'increment' if method == 'incremental' else None,
            basis_verification={'exit_code': basis_check['returncode'],
                'duration_seconds': basis_check['seconds'], 'error': basis_check['error'],
                'stdout': basis_check['stdout'], 'stderr': basis_check['stderr']}
                if method == 'incremental' else None)
        record['backup_result'] = backup_result
        atomic_json(trial.output / 'backup-result.json', backup_result)
        if code:
            raise RuntimeError('Shared backup adapter rejected trial: ' + backup_result['status'])
        record['capture_seconds'] = backup_result['backup']['duration_seconds']
        record['combine_seconds'] = (backup_result['combination']['duration_seconds']
                                     if backup_result['combination'] else 0.0)
        if backup_result['increment_verification']:
            record['increment_input_verification_seconds'] = backup_result['increment_verification']['duration_seconds']
        record['capture_bytes'] = backup_result['backup_file_bytes']
        record['output_bytes'] = backup_result['output_file_bytes']
        record['method_seconds'] = backup_result['method_seconds']
        record['verification_seconds'] = backup_result['verification']['duration_seconds']
        record['verified_backup_seconds'] = backup_result['time_to_verified_seconds']
        for name in ('output', 'increment'):
            manifest = trial.root / name / 'backup_manifest'
            if manifest.is_file():
                shutil.copyfile(manifest, trial.output / (name + '-manifest.json'))
    elif method == "no_backup":
        trial.finish_workload()
    else:
        raise ValueError("Unknown method")
    if method == 'no_backup':
        record['method_seconds'] = time.perf_counter() - start
    end_wall = time.time()
    if mode == "active":
        if method != "no_backup":
            # Workload must stay active through the measured backup interval.
            if trial.workload.poll() is not None:
                raise RuntimeError("Workload finished before backup; increase --duration")
            trial.finish_workload()
        logs = sorted(trial.output.glob("pgbench-log.*"))
        if not logs:
            raise RuntimeError("Missing transaction logs")
        record["workload_window"] = {"start_epoch": start_wall, "end_epoch": end_wall}
        record["workload"] = parse_pgbench_logs(logs, start_wall, end_wall)
        entire = parse_pgbench_logs(logs, trial.workload_started, trial.workload_finished + .001)
        record["workload_all"] = entire
        if entire["failures"] or entire["skipped"]:
            raise RuntimeError("Workload has failed/skipped transactions")
        expected = trial.state(trial.socket, "expected-after-workload")
        if expected["ledger"]["rows"] != entire["all_logged_successes"]:
            raise RuntimeError("External successful transaction count differs from source ledger")
    if method == "no_backup":
        record["status"] = "pass"
        return record
    record["self_contained_restore"] = trial.restore(output, "package-restore",
                                                     expected if mode == "quiescent" else None)
    if mode == "active":
        supplement_start = time.perf_counter()
        target, lsn = trial.marker()
        record["pitr_target"] = {"name": target, "lsn": lsn}
        record["pitr_archive_bytes"] = directory_bytes(trial.archive)
        record["pitr_wal_preparation_seconds"] = time.perf_counter() - supplement_start
        record["pitr_restore"] = trial.restore(output, "pitr-restore", expected, target)
        record["active_package_exact_cutoff_oracle"] = "not_implemented"
        record["correctness_scope"] = "package startup/invariant plus PITR-assisted exact logical match"
    else:
        record["correctness_scope"] = "self-contained exact logical match to quiescent source"
    if negative_controls:
        corrupted = trial.root / "corrupted"
        shutil.copytree(output, corrupted)
        files = json.loads((corrupted / "backup_manifest").read_text())["Files"]
        victim = max((f for f in files if f.get("Path", "").startswith("base/")), key=lambda f: f["Size"])
        with (corrupted / victim["Path"]).open("r+b") as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes([original[0] ^ 255]))
        if trial.verify(corrupted, check=False)["returncode"] == 0:
            raise RuntimeError("Corrupt data negative control was accepted")
        shutil.rmtree(corrupted)
        missing = trial.root / "missing-wal"
        shutil.copytree(output, missing)
        for file in (missing / "pg_wal").iterdir():
            if file.is_file() and re.fullmatch(r"[0-9A-F]{24}", file.name):
                file.unlink()
        if trial.verify(missing, check=False)["returncode"] == 0:
            raise RuntimeError("Missing required WAL negative control was accepted")
        shutil.rmtree(missing)
        stale = trial.restore(basis, "stale-basis")
        if same_state(expected, stale["state"]):
            raise RuntimeError("Stale backup negative control matched new state")
        record["negative_controls"] = {"corrupt_data": "rejected", "missing_wal": "rejected", "stale_backup": "rejected"}
    trial.verify(basis)
    if sha256(basis / "backup_manifest") != basis_manifest_sha:
        raise RuntimeError("Basis manifest changed")
    record["basis_intact"] = True
    record["status"] = "pass"
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New result directory; must not exist")
    parser.add_argument("--bin-dir")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--change-fraction", type=float, default=.01)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261009)
    args = parser.parse_args(argv)
    if args.rows < 100 or math.gcd(args.rows, 48271) != 1 or not 0 < args.change_fraction <= 1:
        parser.error("rows must be >=100 and coprime to 48271; change fraction must be in (0,1]")
    if args.repeats < 1 or args.duration < 4 or args.rate < 10:
        parser.error("repeats >=1, duration >=4 seconds and rate >=10 required")
    if args.output.exists():
        parser.error("Output must not already exist")
    tools = preflight(args.bin_dir)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    version = subprocess.check_output([tools["postgres"], "--version"], text=True).strip()
    bundle = {"schema_version": 2, "status": "running", "scope": "native baseline smoke",
        "performance_claim": "descriptive local measurements; not a controlled cross-host performance baseline",
        "postgres_version": version, "python_version": platform.python_version(),
        "platform": platform.platform(), "cpu_count": os.cpu_count(),
        "commit": os.environ.get("GITHUB_SHA"), "image_digest": os.environ.get("BENCH_IMAGE_DIGEST"),
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "units": {"duration": "seconds", "size": "bytes", "latency": "milliseconds"},
        "cache_policy": "uncontrolled OS cache; fresh cluster per trial; no cold-cache claim",
        "warmup_policy": "active workload ready after >=10 commits; no excluded backup warmup",
        "trials": []}
    rng = random.Random(args.seed)
    matrix = []
    for repeat in range(args.repeats):
        block = [(method, mode) for mode in ("quiescent", "active") for method in ("full", "incremental")]
        block.append(("no_backup", "active"))
        rng.shuffle(block)
        matrix.extend((repeat, method, mode) for method, mode in block)
    bundle["trial_order"] = matrix
    atomic_json(args.output / "results.json", bundle)
    for index, (repeat, method, mode) in enumerate(matrix):
        run_id = f"{index:02d}-{method}-{mode}-r{repeat}"
        print(f"Starting {run_id}", flush=True)
        record = {"run_id": run_id, "repeat": repeat, "method": method, "mode": mode}
        root = Path(tempfile.mkdtemp(prefix="pdb-"))
        trial = Trial(root, args.output / run_id, tools, args)
        try:
            record.update(run_trial(trial, method, mode, repeat == 0 and method == "full" and mode == "quiescent"))
        except Exception as exc:
            record.update(status="error", error=str(exc))
            (trial.output / "error.txt").write_text(traceback.format_exc())
        finally:
            try:
                trial.cleanup()
                shutil.rmtree(root)
            except Exception as exc:
                record.update(status="error", cleanup_error=str(exc), retained_scratch=str(root))
        bundle["trials"].append(record)
        atomic_json(trial.output / "result.json", record)
        atomic_json(args.output / "results.json", bundle)
        print(json.dumps(record), flush=True)
        if record["status"] != "pass":
            # Avoid spending runner time on repeated infrastructure/configuration failures.
            break
    bundle["status"] = "pass" if len(bundle["trials"]) == len(matrix) and all(r["status"] == "pass" for r in bundle["trials"]) else "error"
    atomic_json(args.output / "results.json", bundle)
    print("BENCHMARK_RESULTS_JSON=" + json.dumps(bundle), flush=True)
    return 0 if bundle["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
