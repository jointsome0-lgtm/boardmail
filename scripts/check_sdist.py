#!/usr/bin/env python3
"""Rebuild an unpacked sdist and check installed wheels using its own fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


SMOKE_TESTS = [
    'test_adapters.AdapterTests.test_separately_supplied_adapter_and_copyable_consumer_loop',
    'test_adapters.AdapterTests.test_v1_read_then_additive_migration_preserves_arrivals_and_marks',
    'test_agent_loop',
]
REQUIRED = ['pyproject.toml', 'README.md', 'AGENT_GUIDE.md', 'LICENSE',
            'boardmail/cli.py', 'boardmail/mcp.py', 'boardmail/table.py', 'docs/reference.md',
            'scripts/check_sdist.py', 'scripts/inner_reach.py', 'scripts/agent_view.py',
            'examples/demo.py', 'examples/fixtures.py',
            'examples/agent_loop.py', 'examples/custom_board.py',
            'examples/custom_feed.json', 'examples/custom_config.json',
            'tests/test_mail.py', 'tests/test_adapters.py', 'tests/test_agent_loop.py',
            'tests/test_inner_reach.py', 'tests/inner_reach.txt', 'tests/fixtures/inner_reach/cases.py',
            'tests/fixtures/inner_reach/imports.py', 'tests/fixtures/inner_reach/startup.py',
            'tests/fixtures/inner_reach/support.py', 'tests/fixtures/inner_reach/below/helpers.py',
            'tests/fixtures/inner_reach/below/stores.py',
            'tests/test_agent_view.py', 'tests/agent_view_cli.txt', 'tests/agent_view_mcp.txt',
            'tests/test_error_codes.py', 'tests/error_codes.txt',
            'tests/kit.py', 'tests/test_story_inbox.py', 'tests/story_inbox.txt',
            'tests/test_story_reply.py', 'tests/story_reply.txt',
            'tests/test_story_older.py', 'tests/story_older.txt',
            'tests/test_file_shape.py', 'tests/file_shape.txt', 'tests/test_schema_guard.py',
            'tests/test_argument_errors.py', 'tests/argument_errors_cli.txt', 'tests/argument_errors_mcp.txt',
            'tests/fixtures/v1.sql']
GUARD = '''import socket
import sys
def denied(*args, **kwargs):
    raise RuntimeError("artifact checks forbid network connect and bind")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.bind = denied
socket.create_connection = denied
sys._boardmail_artifact_guard_active = True
'''


def distribution(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
    else:
        with tarfile.open(path) as archive:
            members = archive.getnames()
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'members': members}


def captured_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''


def check(args, report):
    report['sdist'] = distribution(args.sdist)
    work = None
    try:
        with tempfile.TemporaryDirectory(prefix='boardmail-sdist-') as name:
            work = Path(name)
            env = {'PATH': os.defpath, 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                   'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
                   'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                        'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'):
                path = work/key.lower()
                path.mkdir(mode=0o700)
                env[key] = str(path)

            def run(command, cwd, environment):
                record = {'command': list(map(str, command)), 'cwd': str(cwd)}
                try:
                    result = subprocess.run(command, cwd=cwd, env=environment,
                                            capture_output=True, text=True, timeout=180)
                except subprocess.TimeoutExpired as exc:
                    report['commands'].append({**record, 'exit_code': None, 'timed_out': True,
                                               'stdout': captured_text(exc.stdout),
                                               'stderr': captured_text(exc.stderr)})
                    raise
                report['commands'].append({'command': list(map(str, command)), 'cwd': str(cwd),
                                           'exit_code': result.returncode,
                                           'stdout': result.stdout, 'stderr': result.stderr})
                if result.returncode:
                    raise RuntimeError('Command failed: ' + ' '.join(map(str, command)))
                return result.stdout

            unpacked = work/'unpacked'
            with tarfile.open(args.sdist) as archive:
                archive.extractall(unpacked, filter='data')
            roots = list(unpacked.iterdir())
            if len(roots) != 1 or not roots[0].is_dir():
                raise ValueError('Expected one source-distribution root')
            source = roots[0]
            missing = [path for path in REQUIRED if not (source/path).is_file()]
            if missing:
                raise ValueError('Source distribution lacks: ' + ', '.join(missing))
            report['required_files_present'] = REQUIRED
            rebuilt = work/'build-output'
            rebuilt.mkdir()
            build = ('from setuptools.build_meta import build_wheel; '
                     'print(build_wheel(' + repr(str(rebuilt)) + '))')
            run([sys.executable, '-B', '-c', build], source, env)
            wheels = list(rebuilt.glob('*.whl'))
            if len(wheels) != 1:
                raise ValueError('Expected one rebuilt wheel')
            if args.rebuilt_wheel_dir:
                args.rebuilt_wheel_dir.mkdir(parents=True, exist_ok=True)
                destination = args.rebuilt_wheel_dir/wheels[0].name
                shutil.copyfile(wheels[0], destination)
                wheels = [destination]
            candidates = [('rebuilt', wheels[0])]
            if args.wheel:
                candidates.append(('direct', args.wheel))
            report['wheels'] = []
            for label, wheel in candidates:
                artifact = distribution(wheel)
                artifact['label'] = label
                report['wheels'].append(artifact)
                smoke = work/label
                smoke.mkdir()
                for directory in ('examples', 'tests', 'scripts'):
                    shutil.copytree(source/directory, smoke/directory)
                guard = smoke/'guard'
                guard.mkdir()
                (guard/'sitecustomize.py').write_text(GUARD)
                prefix = smoke/'venv'
                run([sys.executable, '-B', '-m', 'venv', str(prefix)], smoke, env)
                python = prefix/'bin/python'
                runtime_env = {**env, 'PYTHONPATH': os.pathsep.join(map(str, (guard, smoke, smoke/'tests')))}
                install = [str(python), '-B', '-m', 'pip', 'install', '--no-index']
                if args.mcp_wheels:
                    install.extend(['--find-links', str(args.mcp_wheels), str(wheel) + '[mcp]'])
                else:
                    install.extend(['--no-deps', str(wheel)])
                run(install, smoke, runtime_env)
                proof = '''import importlib.metadata, inspect, json, pathlib, sys
import boardmail
assert getattr(sys, "_boardmail_artifact_guard_active", False)
prefix = pathlib.Path(sys.prefix).resolve()
paths = {"boardmail.__init__": boardmail.__file__}
entries = {entry.name: entry for entry in importlib.metadata.distribution("boardmail").entry_points}
for name in ("boardmail", "boardmail-mcp"):
    paths[name] = inspect.getfile(entries[name].load())
assert all(prefix in pathlib.Path(path).resolve().parents for path in paths.values()), paths
print(json.dumps({"prefix": str(prefix), "origins": paths,
                  "network_guard_active": True,
                  "entry_points": {name: entries[name].value for name in ("boardmail", "boardmail-mcp")}}))
'''
                artifact['installed_origin_proof'] = json.loads(run([str(python), '-B', '-c', proof], smoke, runtime_env))
                for command in ('boardmail', 'boardmail-mcp'):
                    run([str(prefix/'bin'/command), '--help'], smoke, runtime_env)
                tests = ['discover', '-s', 'tests', '-v'] if args.full_suite else ['-v', *SMOKE_TESTS]
                run([str(python), '-B', '-m', 'unittest', *tests], smoke, runtime_env)
                run([str(python), '-B', 'examples/demo.py'], smoke, runtime_env)
    finally:
        if work is not None:
            report['temporary_state_removed_after_check'] = not work.exists()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sdist', type=Path)
    parser.add_argument('--wheel', type=Path, help='Also check the directly built wheel with sdist fixtures')
    parser.add_argument('--mcp-wheels', type=Path, help='Offline directory containing MCP dependency wheels')
    parser.add_argument('--full-suite', action='store_true', help='Run every bundled test instead of the artifact smoke')
    parser.add_argument('--rebuilt-wheel-dir', type=Path, help='Keep the rebuilt wheel after temporary-state cleanup')
    parser.add_argument('--report', type=Path, help='Save inventories, origin proofs and exact command results as JSON')
    args = parser.parse_args(argv)
    args.sdist = args.sdist.resolve()
    if args.wheel:
        args.wheel = args.wheel.resolve()
    if args.mcp_wheels:
        args.mcp_wheels = args.mcp_wheels.resolve()
    if args.rebuilt_wheel_dir:
        args.rebuilt_wheel_dir = args.rebuilt_wheel_dir.resolve()
    report = {'commands': [], 'full_suite': args.full_suite, 'mcp_dependencies': bool(args.mcp_wheels),
              'network_guard': 'Cooperative Python socket prohibition; not OS containment'}
    try:
        check(args, report)
        report['status'] = 'passed'
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
