"""Tests for VM sets (infra/gcp/README.md): several sets of benchmark VMs at once, without a cloud call.

- config.sh's resolution: the default set as before, a named set's names and SSH paths, set files, roles and
  per-role settings, and the names and roles it refuses;
- create.sh's and teardown.sh's dry runs: every gcloud command printed, none run, nothing on the Mac changed;
- both scripts against a fake gcloud that keeps a project's VMs and disks in a file: each set's own known_hosts,
  a VM of another set or in another zone refused, the quota check, and teardown deleting only its set and its
  disks and failing when something of it is left;
- remote.py's set selection: --pair and PAIR, each role described in its zone, the set's known_hosts, ssh to any
  role, setup-ssh on a client, ssh-config, config and fetch's directory;
- startup.sh's choice of data disk, the reformat scripts on a named set's dest, and agent.py env's record of the
  set.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.network import agent, remote  # noqa: E402

GCP = ROOT / 'infra' / 'gcp'
PROJECT = 'postgres-delta-backup'

# A gcloud stand-in: the project's VMs and disks in a JSON file (FAKE_GCLOUD_STATE), every call appended to
# FAKE_GCLOUD_LOG. It answers what create.sh, teardown.sh and remote.py ask, ignores --filter (the scripts pick
# their rows themselves) and exits 3 on anything else.
FAKE_GCLOUD = r'''
import json, os, sys

state_path, log_path = os.environ['FAKE_GCLOUD_STATE'], os.environ['FAKE_GCLOUD_LOG']
argv = sys.argv[1:]
with open(log_path, 'a') as log:
    log.write(json.dumps(argv) + '\n')
with open(state_path) as stream:
    state = json.load(stream)
assert argv[:2] == ['--project', 'postgres-delta-backup'], argv
args = argv[2:]
first_option = next((i for i, a in enumerate(args) if a.startswith('-')), len(args))
words = args[:first_option]  # the command and its positional arguments
options = {}
for i, a in enumerate(args):
    if a.startswith('--'):
        key, _, value = a.partition('=')
        if not value and i + 1 < len(args) and not args[i + 1].startswith('-'):
            value = args[i + 1]
        options.setdefault(key, []).append(value)


def save():
    with open(state_path, 'w') as stream:
        json.dump(state, stream)


def cpus(machine):
    return int(machine.rsplit('-', 1)[1])


def labels(text, sep=','):
    return dict(item.split('=', 1) for item in text.split(sep) if item)


if args[:2] == ['auth', 'print-access-token']:
    sys.exit(0 if state.get('signed_in', True) else 1)
elif args[:2] == ['services', 'list']:
    print('compute.googleapis.com')
elif args[:3] == ['compute', 'instances', 'list']:
    for vm in state['instances']:
        if options['--format'][0].startswith('csv'):
            print(','.join([vm['name'], vm['zone'], vm['labels'].get('pgd-set', ''),
                            vm['labels'].get('purpose', '')]))
        else:
            print(vm['name'], vm['zone'], vm['labels'].get('pgd-set', ''))
elif args[:3] == ['compute', 'instances', 'create']:
    name, zone = words[3], options['--zone'][0]
    if any(vm['name'] == name and vm['zone'] == zone for vm in state['instances']):
        sys.exit('already exists')
    state['instances'].append({'name': name, 'zone': zone, 'machine': options['--machine-type'][0],
                               'labels': labels(options['--labels'][0]),
                               'local_ssds': len(options.get('--local-ssd', []))})
    state['disks'].append({'name': name, 'zone': zone, 'labels': {}, 'users': [name], 'gb': 50})
    for spec in options.get('--create-disk', []):
        disk = dict(item.split('=', 1) for item in spec.split(','))
        state['disks'].append({'name': disk['name'], 'zone': zone, 'labels': labels(disk.get('labels', ''), ':'),
                               'users': [name], 'gb': int(disk['size'].rstrip('GB'))})
    save()
elif args[:3] == ['compute', 'instances', 'delete']:
    zone = options['--zone'][0]
    assert options.get('--delete-disks') == ['all'] and '--quiet' in args, args
    for name in words[3:]:
        if name in state.get('sticky', []):
            continue
        before = len(state['instances'])
        state['instances'] = [vm for vm in state['instances'] if not (vm['name'] == name and vm['zone'] == zone)]
        if len(state['instances']) == before:
            sys.exit(f'no {name} in {zone}')
        state['disks'] = [disk for disk in state['disks'] if not (disk['zone'] == zone and name in disk['users'])]
    save()
elif args[:3] == ['compute', 'disks', 'list']:
    for disk in state['disks']:
        print(','.join([disk['name'], disk['zone'], disk['labels'].get('pgd-set', ''),
                        disk['labels'].get('purpose', ''), ';'.join(disk['users'])]))
elif args[:3] == ['compute', 'disks', 'delete']:
    zone = options['--zone'][0]
    for name in words[3:]:
        disk = next(d for d in state['disks'] if d['name'] == name and d['zone'] == zone)
        if disk['users']:
            sys.exit(f'{name} is in use')
        state['disks'].remove(disk)
    save()
elif args[:3] == ['compute', 'machine-types', 'describe']:
    print(cpus(words[3]))
elif args[:3] == ['compute', 'project-info', 'describe']:
    print(f"CPUS_ALL_REGIONS,{state['quota']['CPUS_ALL_REGIONS']}.0,"
          f"{sum(cpus(vm['machine']) for vm in state['instances'])}.0")
elif args[:3] == ['compute', 'regions', 'describe']:
    region = words[3]
    vms = [vm for vm in state['instances'] if vm['zone'].rsplit('-', 1)[0] == region]
    disks = [disk for disk in state['disks'] if disk['zone'].rsplit('-', 1)[0] == region]
    usage = {'CPUS': 0, 'N2_CPUS': sum(cpus(vm['machine']) for vm in vms if vm['machine'].startswith('n2-')),
             'INSTANCES': len(vms), 'IN_USE_ADDRESSES': len(vms),
             'LOCAL_SSD_TOTAL_GB': 375 * sum(vm.get('local_ssds', 0) for vm in vms),
             'SSD_TOTAL_GB': sum(disk['gb'] for disk in disks)}
    for metric, limit in state['quota'].items():
        if metric in usage:
            print(f'{metric},{limit}.0,{usage[metric]}.0')
else:
    sys.exit(3)
'''

# The project's quota as the owner's decisions give it: 32 vCPUs across all regions, 24 instances and 8 external
# addresses in us-central1.
QUOTA = {'CPUS_ALL_REGIONS': 32, 'CPUS': 24, 'N2_CPUS': 32, 'INSTANCES': 24, 'IN_USE_ADDRESSES': 8,
         'LOCAL_SSD_TOTAL_GB': 6000, 'SSD_TOTAL_GB': 8192}


def vm(name, zone='us-central1-b', pair=None, purpose=True, machine='n2-standard-8'):
    labels = {'purpose': 'pg-delta-benchmark'} if purpose else {}
    if pair:
        labels['pgd-set'] = pair
    return {'name': name, 'zone': zone, 'machine': machine, 'labels': labels, 'local_ssds': 0}


def boot_disk(name, zone='us-central1-b', attached=True):
    return {'name': name, 'zone': zone, 'labels': {}, 'users': [name] if attached else [], 'gb': 50}


def data_disk(name, pair, zone='us-central1-b', attached=True, gb=100):
    return {'name': f'{name}-data', 'zone': zone, 'labels': {'purpose': 'pg-delta-benchmark', 'pgd-set': pair},
            'users': [name] if attached else [], 'gb': gb}


class Sandbox:
    """A temporary HOME with the Mac-side key, a sets directory, the fake gcloud and its project."""

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        self.home = self.root / 'home'
        self.keys = self.home / '.ssh' / 'pgd-bench'
        self.keys.mkdir(parents=True)
        (self.keys / 'id_ed25519').write_text('PRIVATE\n')
        (self.keys / 'id_ed25519.pub').write_text('ssh-ed25519 AAAATEST bench@pgd-bench\n')
        self.sets = self.root / 'sets'
        self.sets.mkdir()
        self.gcloud = self.root / 'gcloud'
        self.gcloud.write_text(f'#!{sys.executable}\n{FAKE_GCLOUD}')
        self.gcloud.chmod(0o755)
        self.state_path = self.root / 'state.json'
        self.log_path = self.root / 'gcloud.log'
        self.state = {'instances': [], 'disks': [], 'quota': dict(QUOTA)}
        self.save()

    def save(self):
        self.state_path.write_text(json.dumps(self.state))

    def load(self) -> dict:
        return json.loads(self.state_path.read_text())

    def calls(self) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line)[2:] for line in self.log_path.read_text().splitlines()]

    def env(self, **extra) -> dict:
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.home), 'GCLOUD': str(self.gcloud),
               'PGD_SETS_DIR': str(self.sets), 'FAKE_GCLOUD_STATE': str(self.state_path),
               'FAKE_GCLOUD_LOG': str(self.log_path)}
        env.update(extra)
        return env

    def run(self, script: str, **extra) -> subprocess.CompletedProcess:
        return subprocess.run(['/bin/bash', str(GCP / script)], env=self.env(**extra), capture_output=True,
                              text=True, timeout=120)

    def write_set(self, name: str, text: str):
        (self.sets / f'{name}.sh').write_text(text)


def printed(stdout: str) -> list[list[str]]:
    """The gcloud commands a dry run printed, split as a shell would, without gcloud and --project."""
    commands = []
    for line in stdout.splitlines():
        if line.startswith('+ '):
            words = shlex.split(line[2:])
            assert words[1:3] == ['--project', PROJECT], words
            commands.append(words[3:])
    return commands


def creates(commands) -> dict:
    return {words[3]: words for words in commands if words[:3] == ['compute', 'instances', 'create']}


def option(words, name):
    """The value of NAME in WORDS, whether given as NAME VALUE or NAME=VALUE."""
    for i, word in enumerate(words):
        if word == name:
            return words[i + 1]
        if word.startswith(name + '='):
            return word.split('=', 1)[1]
    return None


# --- config.sh, read through remote.load_config ------------------------------------------------------------------

class ConfigResolution(unittest.TestCase):
    def config(self, pair=None, **env):
        with tempfile.TemporaryDirectory() as tmp:
            base = {'PATH': os.environ['PATH'], 'HOME': tmp, 'PGD_SETS_DIR': getattr(self, 'sets', tmp)}
            base.update(env)
            with mock.patch.dict(os.environ, base, clear=True):
                return remote.load_config(pair=pair)

    def test_the_default_set_is_pgd_source_and_pgd_dest_as_before(self):
        config = self.config(SSH_KEY='/k/id')
        self.assertEqual((config['SOURCE_VM'], config['DEST_VM'], config['ZONE']),
                         ('pgd-source', 'pgd-dest', 'us-central1-b'))
        self.assertEqual((config['PAIR'], config['SET_NAME'], config['SET_FILE']), ('', 'default', ''))
        self.assertEqual(config['ROLES'], ['source', 'dest'])
        self.assertEqual(config['SSH_KNOWN_HOSTS'], '/k/known_hosts')
        self.assertEqual(config['SSH_CONFIG'], '/k/ssh_config')
        self.assertEqual(config['VMS'], {
            role: {'name': f'pgd-{role}', 'zone': 'us-central1-b', 'machine': 'n2-standard-8',
                   'data_disk': 'local-nvme', 'local_ssds': '4', 'pd_size_gb': ''} for role in ('source', 'dest')})
        # The environment still overrides each setting, as before sets.
        config = self.config(ZONE='us-central1-a', LOCAL_SSDS='2', DEST_VM='other-dest')
        self.assertEqual(config['VMS']['dest'], {'name': 'other-dest', 'zone': 'us-central1-a',
                                                 'machine': 'n2-standard-8', 'data_disk': 'local-nvme',
                                                 'local_ssds': '2', 'pd_size_gb': ''})

    def test_a_named_set_has_its_own_names_and_ssh_files(self):
        config = self.config(PAIR='a', SSH_KEY='/k/id')
        self.assertEqual((config['SOURCE_VM'], config['DEST_VM']), ('pgd-a-source', 'pgd-a-dest'))
        self.assertEqual((config['PAIR'], config['SET_NAME']), ('a', 'a'))
        self.assertEqual(config['SSH_KNOWN_HOSTS'], '/k/sets/a/known_hosts')
        self.assertEqual(config['SSH_CONFIG'], '/k/sets/a/ssh_config')
        self.assertEqual(config['SSH_KEY'], '/k/id')  # one key for every set
        # load_config's pair wins over PAIR in the environment, and '' is the default set.
        self.assertEqual(self.config('b', PAIR='a')['DEST_VM'], 'pgd-b-dest')
        self.assertEqual(self.config('', PAIR='a')['DEST_VM'], 'pgd-dest')

    def test_a_set_file_and_the_environment_over_it(self):
        with tempfile.TemporaryDirectory() as sets:
            self.sets = sets
            Path(sets, 't9.sh').write_text(
                'ROLES=${ROLES:-source dest client}\nZONE=${ZONE:-us-central1-a}\n'
                'DEST_ZONE=${DEST_ZONE:-us-central1-c}\nDATA_DISK=${DATA_DISK:-pd-balanced}\n'
                'PD_SIZE_GB=${PD_SIZE_GB:-2000}\n')
            config = self.config('t9')
            self.assertEqual(config['SET_FILE'], str(Path(sets, 't9.sh')))
            self.assertEqual(config['ROLES'], ['source', 'dest', 'client'])
            vms = config['VMS']
            self.assertEqual([vms[role]['name'] for role in vms], ['pgd-t9-source', 'pgd-t9-dest', 'pgd-t9-client'])
            self.assertEqual([vms[role]['zone'] for role in vms], ['us-central1-a', 'us-central1-c', 'us-central1-a'])
            self.assertEqual([(vms[role]['data_disk'], vms[role]['pd_size_gb']) for role in vms],
                             [('pd-balanced', '2000'), ('pd-balanced', '2000'), ('pd-balanced', '50')])
            # The client's own defaults: an n2-standard-4 with a 50 GB balanced PD.
            self.assertEqual([vms[role]['machine'] for role in vms], ['n2-standard-8'] * 2 + ['n2-standard-4'])
            # The environment overrides the set file; a role's own setting overrides the set's.
            config = self.config('t9', ZONE='us-central1-f', PD_SIZE_GB='100', DEST_MACHINE='n2-standard-16',
                                 CLIENT_ZONE='us-central1-b')
            vms = config['VMS']
            self.assertEqual([vms[role]['zone'] for role in vms], ['us-central1-f', 'us-central1-c', 'us-central1-b'])
            self.assertEqual([vms[role]['pd_size_gb'] for role in vms], ['100', '100', '50'])
            self.assertEqual(vms['dest']['machine'], 'n2-standard-16')
            del self.sets

    def test_a_set_without_a_file_takes_the_defaults(self):
        config = self.config('zz')
        self.assertEqual(config['SET_FILE'], '')
        self.assertEqual(config['VMS']['source'], {'name': 'pgd-zz-source', 'zone': 'us-central1-b',
                                                   'machine': 'n2-standard-8', 'data_disk': 'local-nvme',
                                                   'local_ssds': '4', 'pd_size_gb': ''})

    def test_bad_names_and_roles_are_refused(self):
        for pair in ('A', 'default', 'a_b', '-a', 'a-', 'abcdefghijklm', 'a b'):
            with self.subTest(pair=pair), self.assertRaisesRegex(SystemExit, 'bad PAIR'):
                self.config(pair)
        for roles in ('source', 'dest client', 'source dest dest', 'source dest Client', 'source dest c-1'):
            with self.subTest(roles=roles), self.assertRaisesRegex(SystemExit, 'bad ROLES'):
                self.config(ROLES=roles)
        self.assertEqual(self.config(ROLES='dest source client')['ROLES'], ['dest', 'source', 'client'])


# --- create.sh and teardown.sh: dry runs ---------------------------------------------------------------------------

class DryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)
        # Host keys of the default set and of two named sets; a dry run removes none.
        for path in (self.box.keys / 'known_hosts', self.box.keys / 'sets' / 'a' / 'known_hosts'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('old\n')

    def tearDown(self):
        self.tmp.cleanup()

    def assertNothingRan(self):
        self.assertEqual(self.box.calls(), [])
        self.assertEqual((self.box.keys / 'known_hosts').read_text(), 'old\n')
        self.assertEqual((self.box.keys / 'sets' / 'a' / 'known_hosts').read_text(), 'old\n')

    def test_create_prints_every_command_for_the_default_set_and_runs_none(self):
        result = self.box.run('create.sh', DRY_RUN='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNothingRan()
        commands = printed(result.stdout)
        self.assertEqual(commands[0], ['services', 'list', '--enabled', '--filter=config.name=compute.googleapis.com',
                                       '--format=value(config.name)'])
        self.assertEqual(commands[1][:3], ['compute', 'instances', 'list'])
        self.assertIn('--filter=labels.pgd-set=default OR name=(pgd-source pgd-dest)', commands[1])
        # The quota reads (n2-standard-8 read once), then the two VMs, then the listing.
        self.assertEqual([words[:3] for words in commands[2:5]],
                         [['compute', 'machine-types', 'describe'], ['compute', 'project-info', 'describe'],
                          ['compute', 'regions', 'describe']])
        self.assertEqual(commands[4][3], 'us-central1')
        made = creates(commands)
        self.assertEqual(list(made), ['pgd-source', 'pgd-dest'])
        for role in ('source', 'dest'):
            self.assertEqual(made[f'pgd-{role}'], [
                'compute', 'instances', 'create', f'pgd-{role}', '--zone', 'us-central1-b',
                '--machine-type', 'n2-standard-8', '--image-family', 'debian-12', '--image-project', 'debian-cloud',
                '--boot-disk-size', '50GB', '--boot-disk-type', 'pd-balanced', *['--local-ssd=interface=NVME'] * 4,
                '--no-service-account', '--no-scopes', '--metadata',
                'block-project-ssh-keys=TRUE,ssh-keys=bench:ssh-ed25519 AAAATEST bench@pgd-bench,'
                f'pgd-set=default,pgd-role={role},pgd-data-disk=local-nvme',
                '--metadata-from-file', 'startup-script=startup.sh', '--labels',
                f'purpose=pg-delta-benchmark,owner=pg-delta-backup,pgd-set=default,pgd-role={role}'])
        self.assertEqual(commands[-1][:3], ['compute', 'instances', 'list'])
        self.assertIn(f'dry run: would remove {self.box.keys / "known_hosts"}', result.stdout)

    def test_create_a_set_with_a_client_on_persistent_disks(self):
        self.box.write_set('t9', 'ROLES=${ROLES:-source dest client}\nDEST_ZONE=${DEST_ZONE:-us-central1-c}\n'
                                 'DATA_DISK=${DATA_DISK:-pd-balanced}\nPD_SIZE_GB=${PD_SIZE_GB:-2000}\n')
        result = self.box.run('create.sh', DRY_RUN='1', PAIR='t9', ZONE='us-central1-a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNothingRan()
        commands = printed(result.stdout)
        # Two machine types, each read once; one region.
        self.assertEqual([words[3] for words in commands if words[:3] == ['compute', 'machine-types', 'describe']],
                         ['n2-standard-8', 'n2-standard-4'])
        made = creates(commands)
        self.assertEqual(list(made), ['pgd-t9-source', 'pgd-t9-dest', 'pgd-t9-client'])
        self.assertEqual([option(made[name], '--zone') for name in made],
                         ['us-central1-a', 'us-central1-c', 'us-central1-a'])
        self.assertEqual([option(made[name], '--machine-type') for name in made],
                         ['n2-standard-8', 'n2-standard-8', 'n2-standard-4'])
        for name, role, size in (('pgd-t9-source', 'source', 2000), ('pgd-t9-client', 'client', 50)):
            words = made[name]
            self.assertNotIn('--local-ssd=interface=NVME', words)
            self.assertIn(f'--create-disk=name={name}-data,size={size}GB,type=pd-balanced,device-name=pgd-data,'
                          'auto-delete=yes,labels=purpose=pg-delta-benchmark:owner=pg-delta-backup:pgd-set=t9:'
                          f'pgd-role={role}', words)
            self.assertTrue(option(words, '--metadata').endswith(f'pgd-set=t9,pgd-role={role},'
                                                                 'pgd-data-disk=pd-balanced'))
            self.assertEqual(option(words, '--labels'),
                             f'purpose=pg-delta-benchmark,owner=pg-delta-backup,pgd-set=t9,pgd-role={role}')
        self.assertIn(f'dry run: would remove {self.box.keys / "sets" / "t9" / "known_hosts"}', result.stdout)
        self.assertNotIn(f'would remove {self.box.keys / "known_hosts"}', result.stdout)

    def test_create_refuses_bad_settings_before_any_command(self):
        cases = (({'DATA_DISK': 'pd-balanced'}, 'pd-balanced needs a size in GB'),
                 ({'DATA_DISK': 'ssd'}, "DATA_DISK 'ssd' is none of local-nvme, pd-balanced and pd-ssd"),
                 ({'LABELS': 'owner=me'}, 'lacks purpose=pg-delta-benchmark'),
                 ({'LOCAL_SSDS': '0'}, 'LOCAL_SSDS of 1 or more'),
                 ({'DEST_ZONE': 'nowhere'}, "bad zone 'nowhere'"),
                 ({'DEST_VM': 'pgd-source'}, "is another role's too"),
                 ({'PAIR': 'Bad'}, 'bad PAIR'))
        for env, message in cases:
            with self.subTest(message=message):
                result = self.box.run('create.sh', DRY_RUN='1', **env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(printed(result.stdout), [])
        self.assertNothingRan()

    def test_teardown_prints_the_deletes_of_its_set_only(self):
        self.box.write_set('t9', 'ROLES=${ROLES:-source dest client}\nDEST_ZONE=${DEST_ZONE:-us-central1-c}\n')
        result = self.box.run('teardown.sh', DRY_RUN='1', PAIR='t9')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNothingRan()
        commands = printed(result.stdout)
        deletes = [words for words in commands if words[:3] == ['compute', 'instances', 'delete']]
        self.assertEqual(deletes, [
            ['compute', 'instances', 'delete', 'pgd-t9-source', 'pgd-t9-client', '--zone', 'us-central1-b',
             '--delete-disks=all', '--quiet'],
            ['compute', 'instances', 'delete', 'pgd-t9-dest', '--zone', 'us-central1-c', '--delete-disks=all',
             '--quiet']])
        # The disk listing and the two checks afterwards are printed too.
        self.assertEqual([words[:3] for words in commands].count(['compute', 'disks', 'list']), 2)
        self.assertEqual([words[:3] for words in commands].count(['compute', 'instances', 'list']), 3)
        self.assertIn('--filter=labels.pgd-set=t9 OR name=(pgd-t9-source pgd-t9-source-data pgd-t9-dest '
                      'pgd-t9-dest-data pgd-t9-client pgd-t9-client-data)',
                      [words for words in commands if words[:3] == ['compute', 'disks', 'list']][0])
        self.assertIn('set t9: dry run, nothing deleted', result.stdout)

    def test_the_default_teardown_deletes_both_vms_in_one_call(self):
        result = self.box.run('teardown.sh', DRY_RUN='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        deletes = [words for words in printed(result.stdout) if words[:3] == ['compute', 'instances', 'delete']]
        self.assertEqual(deletes, [['compute', 'instances', 'delete', 'pgd-source', 'pgd-dest', '--zone',
                                    'us-central1-b', '--delete-disks=all', '--quiet']])


# --- create.sh and teardown.sh against the fake project ------------------------------------------------------------

class FakeProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def host_files(self, *pairs):
        """Write a known_hosts and an ssh_config for each set ('' is the default one)."""
        for pair in pairs:
            folder = self.box.keys / 'sets' / pair if pair else self.box.keys
            folder.mkdir(parents=True, exist_ok=True)
            for name in ('known_hosts', 'ssh_config'):
                (folder / name).write_text(f'{pair or "default"} {name}\n')

    def host_file_exists(self, pair, name='known_hosts'):
        return ((self.box.keys / 'sets' / pair if pair else self.box.keys) / name).exists()

    def test_create_forgets_only_its_own_sets_host_keys(self):
        self.host_files('', 'a', 'b')
        result = self.box.run('create.sh', PAIR='a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(vm['name'] for vm in self.box.load()['instances']), ['pgd-a-dest', 'pgd-a-source'])
        self.assertFalse(self.host_file_exists('a'))
        self.assertFalse(self.host_file_exists('a', 'ssh_config'))
        for pair in ('', 'b'):
            for name in ('known_hosts', 'ssh_config'):
                self.assertTrue(self.host_file_exists(pair, name), (pair, name))
        self.assertIn('quota (what the new VMs need, what is free now):', result.stdout)
        self.assertIn('project CPUS_ALL_REGIONS', result.stdout)
        # Run again: both exist, nothing is made, and the set's new host keys stay.
        self.host_files('a')
        result = self.box.run('create.sh', PAIR='a')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('pgd-a-source already exists in us-central1-b', result.stdout)
        self.assertTrue(self.host_file_exists('a'))
        self.assertEqual([call[:3] for call in self.box.calls()].count(['compute', 'instances', 'create']), 2)

    def test_create_makes_only_the_missing_roles_of_a_set_made_before_labels(self):
        # The default set as create.sh made it before sets: no pgd-set label.
        self.box.state['instances'] = [vm('pgd-source')]
        self.box.save()
        result = self.box.run('create.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        made = [call[3] for call in self.box.calls() if call[:3] == ['compute', 'instances', 'create']]
        self.assertEqual(made, ['pgd-dest'])

    def test_create_refuses_a_vm_of_another_set_or_in_another_zone(self):
        for existing, message in ((vm('pgd-a-source', pair='b'), "pgd-a-source in us-central1-b is not set a's"),
                                  (vm('pgd-a-dest', purpose=False), "pgd-a-dest in us-central1-b is not set a's"),
                                  (vm('pgd-a-dest', zone='us-central1-c', pair='a'),
                                   'pgd-a-dest is in us-central1-c; set a asks for us-central1-b')):
            with self.subTest(message=message):
                self.box.state['instances'] = [existing]
                self.box.save()
                self.box.log_path.unlink(missing_ok=True)
                result = self.box.run('create.sh', PAIR='a')
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual([c for c in self.box.calls() if c[:3] == ['compute', 'instances', 'create']], [])
                self.assertEqual(self.box.load()['instances'], [existing])

    def test_create_names_expired_credentials_and_makes_nothing(self):
        # EXP-72's first try: gcloud's sign-in had expired, and create.sh called it a disabled API.
        self.box.state['signed_in'] = False
        self.box.save()
        result = self.box.run('create.sh', PAIR='a')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("run 'gcloud auth login'", result.stderr)
        self.assertNotIn('not enabled', result.stderr)
        self.assertEqual(self.box.load()['instances'], [])

    def test_create_refuses_a_set_the_quota_cant_hold(self):
        # Three n2-standard-8 VMs of other sets use 24 of the 32 vCPUs; a pair needs 16.
        self.box.state['instances'] = [vm(f'pgd-{p}-source', pair=p) for p in ('b', 'c', 'd')]
        self.box.save()
        result = self.box.run('create.sh', PAIR='a')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout, r'project CPUS_ALL_REGIONS +needs +16, +8 free of +32  SHORT')
        self.assertRegex(result.stdout, r'us-central1 N2_CPUS +needs +16, +8 free of +32  SHORT')
        self.assertIn("doesn't fit the quota; nothing was created", result.stderr)
        self.assertEqual(len(self.box.load()['instances']), 3)
        # N2's vCPUs count under N2_CPUS, not CPUS (limit 24, unused here).
        self.assertNotIn('us-central1 CPUS ', result.stdout)
        # QUOTA_CHECK=0 skips the check (the fake project then holds them all).
        result = self.box.run('create.sh', PAIR='a', QUOTA_CHECK='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('quota check skipped (QUOTA_CHECK=0)', result.stdout)
        self.assertEqual(len(self.box.load()['instances']), 5)

    def test_the_quota_counts_addresses_and_persistent_disk(self):
        # Seven VMs hold seven of us-central1's eight external addresses (as small e2 VMs, so vCPUs fit).
        self.box.state['instances'] = [vm(f'other-{i}', machine='e2-small-1', pair='z') for i in range(7)]
        self.box.state['quota']['SSD_TOTAL_GB'] = 2000
        self.box.save()
        self.box.write_set('pd', 'DATA_DISK=${DATA_DISK:-pd-balanced}\nPD_SIZE_GB=${PD_SIZE_GB:-1000}\n')
        result = self.box.run('create.sh', PAIR='pd')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stdout, r'us-central1 IN_USE_ADDRESSES +needs +2, +1 free of +8  SHORT')
        # Two boot disks of 50 GB and two data disks of 1000 GB, against 2000 GB.
        self.assertRegex(result.stdout, r'us-central1 SSD_TOTAL_GB +needs +2100, +2000 free of +2000  SHORT')
        self.assertRegex(result.stdout, r'project CPUS_ALL_REGIONS +needs +16, +25 free of +32\n')
        self.assertEqual(self.box.load()['instances'], self.box.state['instances'])

    def project_with_three_sets(self):
        state = self.box.state
        state['instances'] = [
            vm('pgd-a-source', pair='a'), vm('pgd-a-dest', zone='us-central1-c', pair='a'),
            vm('pgd-a-client', pair='a', machine='n2-standard-4'),
            vm('pgd-b-source', pair='b'), vm('pgd-b-dest', pair='b'),
            vm('pgd-source'), vm('pgd-dest'),  # the default set, made before sets had labels
            vm('web', purpose=False)]
        state['disks'] = [boot_disk(v['name'], v['zone']) for v in state['instances']] + [
            data_disk('pgd-a-source', 'a'), data_disk('pgd-a-client', 'a'), data_disk('pgd-b-source', 'b'),
            # A data disk whose VM is gone, and a boot disk kept by hand.
            data_disk('pgd-a-old', 'a', attached=False), boot_disk('pgd-a-dest', 'us-central1-a', attached=False)]
        self.box.save()

    def names(self, kind):
        return sorted(item['name'] for item in self.box.load()[kind])

    def test_teardown_deletes_only_its_set_and_its_disks(self):
        self.project_with_three_sets()
        self.box.write_set('a', 'ROLES=${ROLES:-source dest client}\nDEST_ZONE=${DEST_ZONE:-us-central1-c}\n')
        result = self.box.run('teardown.sh', PAIR='a')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('set a: no VM or disk of it is left', result.stdout)
        self.assertEqual(self.names('instances'), ['pgd-b-dest', 'pgd-b-source', 'pgd-dest', 'pgd-source', 'web'])
        self.assertEqual(self.names('disks'), ['pgd-b-dest', 'pgd-b-source', 'pgd-b-source-data', 'pgd-dest',
                                               'pgd-source', 'web'])
        deleted = [call for call in self.box.calls() if call[1:3] == ['instances', 'delete']
                   or call[1:3] == ['disks', 'delete']]
        self.assertEqual(deleted, [
            ['compute', 'instances', 'delete', 'pgd-a-source', 'pgd-a-client', '--zone', 'us-central1-b',
             '--delete-disks=all', '--quiet'],
            ['compute', 'instances', 'delete', 'pgd-a-dest', '--zone', 'us-central1-c', '--delete-disks=all',
             '--quiet'],
            ['compute', 'disks', 'delete', 'pgd-a-dest', '--zone', 'us-central1-a', '--quiet'],
            ['compute', 'disks', 'delete', 'pgd-a-old-data', '--zone', 'us-central1-b', '--quiet']])
        # The default set, made before labels: found by name; set b and the unlabelled VM stay.
        result = self.box.run('teardown.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.names('instances'), ['pgd-b-dest', 'pgd-b-source', 'web'])

    def test_a_set_that_lost_a_role_still_loses_that_vm(self):
        # ROLES no longer names the client, but its label says set a: teardown deletes it too.
        self.project_with_three_sets()
        result = self.box.run('teardown.sh', PAIR='a', DEST_ZONE='us-central1-c')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('pgd-a-client', self.names('instances'))

    def test_teardown_leaves_a_same_named_vm_of_another_set_alone(self):
        self.box.state['instances'] = [vm('pgd-a-source', pair='b'), vm('pgd-a-dest', pair='a')]
        self.box.state['disks'] = [boot_disk('pgd-a-source'), boot_disk('pgd-a-dest')]
        self.box.save()
        result = self.box.run('teardown.sh', PAIR='a')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('left alone: pgd-a-source,us-central1-b,b,pg-delta-benchmark', result.stderr)
        self.assertEqual(self.names('instances'), ['pgd-a-source'])
        self.assertEqual(self.names('disks'), ['pgd-a-source'])  # attached to set b's VM

    def test_teardown_fails_when_something_of_the_set_is_left(self):
        self.project_with_three_sets()
        self.box.state['sticky'] = ['pgd-a-dest']
        self.box.save()
        result = self.box.run('teardown.sh', PAIR='a', ROLES='source dest client', DEST_ZONE='us-central1-c')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('teardown.sh: set a is not gone', result.stderr)
        self.assertIn('  VM pgd-a-dest,us-central1-c', result.stderr)
        self.assertIn('  disk pgd-a-dest,us-central1-c,pgd-a-dest', result.stderr)  # its boot disk, still attached

    def test_the_whole_cycle_of_two_sets_at_once(self):
        # 16 + 12 vCPUs: both fit the 32 at once.
        self.box.write_set('t9', 'ROLES=${ROLES:-source dest client}\nMACHINE=${MACHINE:-n2-standard-4}\n'
                                 'DATA_DISK=${DATA_DISK:-pd-balanced}\nPD_SIZE_GB=${PD_SIZE_GB:-100}\n')
        for pair in ('a', 't9'):
            result = self.box.run('create.sh', PAIR=pair)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.names('instances'), ['pgd-a-dest', 'pgd-a-source', 'pgd-t9-client', 'pgd-t9-dest',
                                                   'pgd-t9-source'])
        self.assertIn('pgd-t9-dest-data', self.names('disks'))
        result = self.box.run('teardown.sh', PAIR='t9')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.names('instances'), ['pgd-a-dest', 'pgd-a-source'])
        self.assertEqual(self.names('disks'), ['pgd-a-dest', 'pgd-a-source'])


# --- remote.py's set selection --------------------------------------------------------------------------------------

def description(name, internal, external):
    return json.dumps({'name': name, 'status': 'RUNNING', 'networkInterfaces': [
        {'networkIP': internal, 'accessConfigs': [{'natIP': external}]}]})


class RemoteSets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(self.tmp.name)
        self.box.write_set('t10', 'ROLES=${ROLES:-source dest client}\nCLIENT_ZONE=${CLIENT_ZONE:-us-central1-c}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, pair=None, **env):
        with mock.patch.dict(os.environ, self.box.env(**env), clear=True):
            return remote.load_config(pair=pair)

    def describe(self, config):
        """instances() against canned descriptions; the describe command lines it ran."""
        addresses = {'pgd-t10-source': ('10.0.0.2', '203.0.113.2'), 'pgd-t10-dest': ('10.0.0.3', '203.0.113.3'),
                     'pgd-t10-client': ('10.0.0.4', '203.0.113.4'), 'pgd-source': ('10.0.0.5', '203.0.113.5'),
                     'pgd-dest': ('10.0.0.6', '203.0.113.6')}
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, description(argv[6], *addresses[argv[6]]), '')

        with mock.patch.object(remote.subprocess, 'run', run):
            return remote.instances(config), calls

    def test_the_set_is_named_before_the_command_or_in_the_environment(self):
        parser = remote.build_parser()
        for argv, pair in ((['--pair', 'a', 'start', 'x', '--', '--scale', '5'], 'a'),
                           (['start', 'x', '--', '--scale', '5'], None)):
            args = parser.parse_args(argv)
            extra = args.args[1:] if args.args[:1] == ['--'] else args.args  # as cmd_start reads them
            self.assertEqual((args.pair, args.name, extra), (pair, 'x', ['--scale', '5']))
        seen = []
        with mock.patch.object(remote, 'load_config', side_effect=lambda pair=None: seen.append(pair) or {}), \
                mock.patch.object(remote, 'cmd_config', return_value=0):
            remote.main(['--pair', 'a', 'config'])
            remote.main(['config'])
        self.assertEqual(seen, ['a', None])  # None: config.sh reads PAIR from the environment

    def test_each_role_is_described_in_its_zone(self):
        found, calls = self.describe(self.config('t10'))
        self.assertEqual(list(found), ['source', 'dest', 'client'])
        self.assertEqual([(argv[6], argv[8]) for argv in calls], [
            ('pgd-t10-source', 'us-central1-b'), ('pgd-t10-dest', 'us-central1-b'),
            ('pgd-t10-client', 'us-central1-c')])
        self.assertEqual(found['client'], {'name': 'pgd-t10-client', 'internal': '10.0.0.4',
                                           'external': '203.0.113.4', 'status': 'RUNNING'})
        # With no set named, the same two describes as before sets.
        found, calls = self.describe(self.config())
        self.assertEqual(calls, [[str(self.box.gcloud), '--project', PROJECT, 'compute', 'instances', 'describe',
                                  f'pgd-{role}', '--zone', 'us-central1-b', '--format=json']
                                 for role in ('source', 'dest')])
        # A config made by hand, without VMS, as the older tests make them.
        config = {'GCLOUD': 'g', 'PROJECT': 'p', 'SOURCE_VM': 'pgd-source', 'DEST_VM': 'pgd-dest', 'ZONE': 'z'}
        self.assertEqual(remote.set_vms(config), {'source': {'name': 'pgd-source', 'zone': 'z'},
                                                  'dest': {'name': 'pgd-dest', 'zone': 'z'}})

    def test_ssh_uses_the_sets_own_known_hosts(self):
        for pair, path in (('t10', self.box.keys / 'sets' / 't10' / 'known_hosts'),
                           (None, self.box.keys / 'known_hosts')):
            argv = remote.ssh_argv(self.config(pair), '203.0.113.2', 'true')
            self.assertIn(f'UserKnownHostsFile={path}', argv)
            self.assertIn(f'-i', argv)
            self.assertEqual(argv[argv.index('-i') + 1], str(self.box.keys / 'id_ed25519'))

    def test_ssh_to_any_role_of_the_set(self):
        config = self.config('t10')
        found, _ = self.describe(config)
        with mock.patch.object(remote, 'instances', return_value=found), \
                mock.patch.object(remote.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(remote.cmd_ssh(config, Namespace(role='client', command=['hostname'])), 0)
        self.assertEqual(run.call_args.args[0][-2:], ['bench@203.0.113.4', 'hostname'])
        with self.assertRaisesRegex(SystemExit, "set default has no 'client'; its roles: source, dest"):
            remote.cmd_ssh(self.config(), Namespace(role='client', command=[]))

    def test_setup_ssh_authorizes_dest_on_the_client_too(self):
        config = self.config('t10')
        found, _ = self.describe(config)
        calls = []

        def ssh(config, address, command, **kwargs):
            calls.append((address, command))
            return mock.Mock(stdout=b'ssh-ed25519 AAAA key\n' if 'cat' in command else b'host\n', returncode=0)

        with mock.patch.object(remote, 'instances', return_value=found), mock.patch.object(remote, 'ssh', ssh), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(remote.cmd_setup_ssh(config, Namespace(control_port=0)), 0)
        self.assertEqual([address for address, _ in calls],
                         ['203.0.113.3', '203.0.113.2', '203.0.113.3', '203.0.113.4', '203.0.113.3'])
        self.assertIn('authorized_keys', calls[3][1])
        self.assertTrue(calls[4][1].endswith('bench@10.0.0.4 hostname'))
        self.assertIn('pgd-t10-dest -> 10.0.0.4: host', out.getvalue())

    def test_ssh_config_is_written_beside_the_sets_known_hosts(self):
        config = self.config('t10')
        found, _ = self.describe(config)
        with mock.patch.object(remote, 'instances', return_value=found), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(remote.cmd_ssh_config(config, Namespace()), 0)
        path = self.box.keys / 'sets' / 't10' / 'ssh_config'
        self.assertEqual(out.getvalue().strip(), str(path))
        text = path.read_text()
        self.assertIn('Host client pgd-t10-client\n  HostName 203.0.113.4\n  User bench\n', text)
        self.assertIn(f'  UserKnownHostsFile "{self.box.keys / "sets" / "t10" / "known_hosts"}"\n', text)
        self.assertEqual(text.count('\nHost '), 3)

    def test_config_prints_the_set_without_a_cloud_call(self):
        config = self.config('t10')
        with mock.patch.object(remote.subprocess, 'run', side_effect=AssertionError('no call')), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(remote.cmd_config(config, Namespace()), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual(shown['set'], 't10')
        self.assertEqual(shown['vms']['client']['zone'], 'us-central1-c')
        self.assertEqual(shown['vms']['client']['machine'], 'n2-standard-4')

    def test_fetch_keeps_each_sets_runs_apart(self):
        self.assertEqual(remote.fetch_dir(self.config(), 'smoke'), remote.REPO_ROOT / 'results' / 'exp04' / 'smoke')
        self.assertEqual(remote.fetch_dir(self.config('t10'), 'smoke'),
                         remote.REPO_ROOT / 'results' / 'exp04' / 'sets' / 't10' / 'smoke')

    def test_a_bad_set_stops_remote_py(self):
        with self.assertRaisesRegex(SystemExit, "bad PAIR 'T10'"):
            self.config('T10')


# --- on the VMs ------------------------------------------------------------------------------------------------------

class StartupDataDisk(unittest.TestCase):
    """startup.sh's data_disks, sourced: only the function is defined, nothing runs."""

    def data_disks(self, *devices):
        with tempfile.TemporaryDirectory() as tmp:
            by_id, stubs = Path(tmp, 'by-id'), Path(tmp, 'bin')
            by_id.mkdir()
            stubs.mkdir()
            for name in devices:
                (by_id / name).touch()
            for name in ('apt-get', 'mdadm', 'mkfs.ext4', 'mount', 'systemctl', 'sshd'):
                (stubs / name).write_text(f'#!/bin/sh\ntouch {tmp}/ran-{name}\nexit 99\n')
                (stubs / name).chmod(0o755)
            result = subprocess.run(['/bin/bash', '-c', f'source {shlex.quote(str(GCP / "startup.sh"))}; '
                                     f'data_disks {shlex.quote(str(by_id))}'], capture_output=True, text=True,
                                    env={'PATH': f'{stubs}:{os.environ["PATH"]}'}, timeout=30)
            self.assertEqual(sorted(p.name for p in Path(tmp).glob('ran-*')), [])
            return result.returncode, result.stdout.replace(str(by_id) + '/', ''), result.stderr

    def test_the_kinds(self):
        self.assertEqual(self.data_disks(*[f'google-local-nvme-ssd-{i}' for i in range(4)]),
                         (0, 'raid google-local-nvme-ssd-0 google-local-nvme-ssd-1 google-local-nvme-ssd-2 '
                             'google-local-nvme-ssd-3\n', ''))
        self.assertEqual(self.data_disks('google-local-nvme-ssd-0'), (0, 'nvme google-local-nvme-ssd-0\n', ''))
        self.assertEqual(self.data_disks('google-pgd-data', 'google-persistent-disk-0'),
                         (0, 'pd google-pgd-data\n', ''))
        code, _, error = self.data_disks('google-pgd-data', 'google-local-nvme-ssd-0')
        self.assertEqual(code, 1)
        self.assertIn('refusing to choose', error)
        code, _, error = self.data_disks('google-persistent-disk-0')
        self.assertEqual(code, 1)
        self.assertIn('no data disk', error)

    def test_both_kinds_get_the_same_filesystem(self):
        text = (GCP / 'startup.sh').read_text()
        self.assertEqual(text.count('mkfs.ext4 -q -F -m 0'), 1)
        self.assertIn('-E lazy_itable_init=0,lazy_journal_init=0 "$target"', text)
        self.assertIn('mount -o noatime "$target" /data', text)


class ReformatOnANamedSet(unittest.TestCase):
    SCRIPT = GCP / 'reformat_dest_xfs.sh'

    def run_script(self, host, **env):
        with tempfile.TemporaryDirectory() as tmp:
            stubs = {'id': 'echo 0', 'hostname': f'echo {host}'}
            stubs.update({name: 'echo "$0 must not run" >&2; exit 99'
                          for name in ('umount', 'mkfs.xfs', 'mkfs.ext4', 'mount', 'chown', 'chmod', 'wipefs')})
            for name, body in stubs.items():
                path = Path(tmp, name)
                path.write_text(f'#!/bin/sh\n{body}\n')
                path.chmod(0o755)
            return subprocess.run(['bash', str(self.SCRIPT), '--yes'], capture_output=True, text=True, timeout=60,
                                  env={'PATH': f'{tmp}:{os.environ["PATH"]}', 'HOME': tmp, **env})

    def test_it_takes_the_sets_dest(self):
        if Path('/dev/md/pgdata').exists() or Path('/dev/disk/by-id/google-pgd-data').exists():
            self.skipTest('this machine has a data disk')
        for script in ('reformat_dest_xfs.sh', 'reformat_dest_ext4.sh'):
            self.SCRIPT = GCP / script
            with self.subTest(script=script):
                result = self.run_script('pgd-a-dest', PAIR='a')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('no md array', result.stderr)  # past the host checks
                result = self.run_script('pgd-a-source', PAIR='a')
                self.assertIn('refusing to run on pgd-a-source', result.stderr)
                result = self.run_script('pgd-a-dest')
                self.assertIn('this script is for pgd-dest only (a named set', result.stderr)
                self.assertNotIn('must not run', result.stderr)


class AgentRecordsTheSet(unittest.TestCase):
    def test_env_reads_the_set_from_the_instance_metadata(self):
        values = {'attributes/pgd-set': 't10', 'attributes/pgd-role': 'client',
                  'attributes/pgd-data-disk': 'pd-balanced'}
        with mock.patch.object(agent, 'metadata', side_effect=values.get), \
                mock.patch.object(agent, 'text_of', return_value=None):
            env = agent.cmd_env(Namespace(iface='lo', bin_dir='/b', pg_probackup='/p'))
        self.assertEqual(env['vm_set'], {'set': 't10', 'role': 'client', 'data-disk': 'pd-balanced'})


if __name__ == '__main__':
    unittest.main()
