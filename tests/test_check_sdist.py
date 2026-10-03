"""Diagnostic controls for the standalone artifact checker, without child processes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


CHECKER_PATH = Path(__file__).resolve().parents[1] / 'scripts/check_sdist.py'
spec = importlib.util.spec_from_file_location('check_sdist', CHECKER_PATH)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class CheckSdistTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix='checker-test-')
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.sdist = self.root / 'fixture.tar.gz'
        with tarfile.open(self.sdist, 'w:gz') as archive:
            for name in checker.REQUIRED:
                payload = b'# tiny checker fixture\n'
                if name == 'scripts/check_sdist.py':
                    payload = CHECKER_PATH.read_bytes()
                entry = tarfile.TarInfo('fixture/' + name)
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))

    def invoke(self, child):
        report_path = self.root / 'report.json'
        with mock.patch.object(checker.subprocess, 'run', side_effect=child), contextlib.redirect_stdout(io.StringIO()):
            status = checker.main([str(self.sdist), '--report', str(report_path)])
        return status, json.loads(report_path.read_text())

    def assert_removed(self, report):
        work = Path(report['commands'][0]['cwd']).parents[1]
        self.assertFalse(work.exists())
        self.assertIs(report['temporary_state_removed_after_check'], True)

    def test_timeout_keeps_partial_output_for_bytes_text_and_none(self):
        for output, stderr, expected_out, expected_err in (
            (b'completed test\n\xff', b'partial error\n', 'completed test\n\ufffd', 'partial error\n'),
            ('completed test\n', 'partial error\n', 'completed test\n', 'partial error\n'),
            (None, None, '', ''),
        ):
            with self.subTest(output=output):
                def timeout(command, **kwargs):
                    self.assertEqual(kwargs['timeout'], 180)
                    self.assertTrue(kwargs['text'])
                    raise subprocess.TimeoutExpired(command, 180, output=output, stderr=stderr)
                status, report = self.invoke(timeout)
                self.assertEqual(status, 1)
                self.assertEqual(report['status'], 'failed')
                self.assertIn('timed out', report['error'])
                command = report['commands'][0]
                self.assertIsNone(command['exit_code'])
                self.assertIs(command['timed_out'], True)
                self.assertEqual(command['stdout'], expected_out)
                self.assertEqual(command['stderr'], expected_err)
                self.assert_removed(report)

    def test_nonzero_keeps_logs_and_reports_cleanup(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 7, 'ordinary output\n', 'ordinary error\n')
        status, report = self.invoke(failed)
        self.assertEqual(status, 1)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('Command failed:', report['error'])
        command = report['commands'][0]
        self.assertEqual((command['exit_code'], command['stdout'], command['stderr']),
                         (7, 'ordinary output\n', 'ordinary error\n'))
        self.assertNotIn('timed_out', command)
        self.assert_removed(report)

    def test_failed_cleanup_does_not_claim_removal(self):
        work = self.root / 'retained-work'
        work.mkdir()
        class FailedCleanup:
            def __enter__(self):
                return str(work)
            def __exit__(self, *args):
                raise OSError('synthetic cleanup refusal')
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 7, 'output', 'error')
        with mock.patch.object(checker.tempfile, 'TemporaryDirectory', return_value=FailedCleanup()):
            status, report = self.invoke(failed)
        self.assertEqual(status, 1)
        self.assertEqual(report['error'], 'synthetic cleanup refusal')
        self.assertTrue(work.exists())
        self.assertIs(report['temporary_state_removed_after_check'], False)
        self.assertEqual(report['commands'][0]['exit_code'], 7)

    def test_success_uses_bundled_checker_and_reports_cleanup(self):
        tested = []
        def successful(command, **kwargs):
            cwd = Path(kwargs['cwd'])
            if 'from setuptools.build_meta' in str(command[-1]):
                wheel = cwd.parents[1] / 'build-output/fixture.whl'
                with zipfile.ZipFile(wheel, 'w') as archive:
                    archive.writestr('fixture.txt', 'mock wheel')
            if 'unittest' in command:
                self.assertEqual((cwd / 'scripts/check_sdist.py').read_bytes(), CHECKER_PATH.read_bytes())
                tested.append(cwd)
            return subprocess.CompletedProcess(command, 0, '{}', '')
        status, report = self.invoke(successful)
        self.assertEqual(status, 0)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(len(tested), 1)
        self.assert_removed(report)


if __name__ == '__main__':
    unittest.main()
