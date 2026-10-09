"""Check the retained Tcl configuration without pretending to run HammerDB."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('tclsh'), 'Tcl interpreter unavailable')
class HammerDBConfigurationTests(unittest.TestCase):
    def invoke(self, name, password, **settings):
        script = Path(__file__).resolve().parents[1] / 'benchmarks/workloads/hammerdb' / name
        driver = '''
proc dbset {args} {}
proc diset {section name value} {dict set ::options "$section/$name" $value}
foreach cmd {buildschema loadscript vuset vucreate tcstart vudestroy tcstop} {
    proc $cmd {args} {}
}
proc vurun {} {return test-job}
source [lindex $argv 0]
foreach key {tpcc/pg_count_ware tpcc/pg_num_vu tpcc/pg_duration tpcc/pg_rampup tpcc/pg_timeprofile tpcc/pg_allwarehouse} {
    if {[dict exists $::options $key]} {puts "OPTION $key [dict get $::options $key]"}
}
'''
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TPCC_', 'PGBENCH_'))}
        env.update(PGBENCH_PASS=password, **settings)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'driver.tcl'
            path.write_text(driver)
            return subprocess.run(['tclsh', str(path), str(script)], env=env,
                                  capture_output=True, text=True, timeout=10)

    def test_empty_password_refused_by_both_entrypoints(self):
        for name in ('build_schema.tcl', 'run_timed.tcl'):
            with self.subTest(name=name):
                result = self.invoke(name, '')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('PGBENCH_PASS is not set', result.stderr)

    def test_build_scale_is_configurable(self):
        result = self.invoke('build_schema.tcl', 'synthetic-test-secret',
                             TPCC_WAREHOUSES='3', TPCC_BUILD_VU='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OPTION tpcc/pg_count_ware 3', result.stdout)
        self.assertIn('OPTION tpcc/pg_num_vu 2', result.stdout)
        self.assertNotIn('synthetic-test-secret', result.stdout)

    def test_timed_profile_retains_latency_and_declared_window_settings(self):
        result = self.invoke('run_timed.tcl', 'synthetic-test-secret',
                             TPCC_DURATION='4', TPCC_RAMPUP='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ('OPTION tpcc/pg_duration 4', 'OPTION tpcc/pg_rampup 2',
                         'OPTION tpcc/pg_timeprofile true', 'OPTION tpcc/pg_allwarehouse true'):
            self.assertIn(expected, result.stdout)
        self.assertNotIn('synthetic-test-secret', result.stdout)
