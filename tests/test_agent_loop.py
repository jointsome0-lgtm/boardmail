"""Consumer recovery and explicit outcome observations, using temporary local state."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from boardmail.store import Store
from examples import agent_loop
from examples.fixtures import uid
from test_mail import mail


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root/'delivery.jsonl'

    def test_delivered_characters_count_text_without_metadata_or_assumed_reading(self):
        message = {'title': 'Тема', 'body': '🦊é', 'author': 'not counted', 'url': 'https://example.invalid',
                   'brief': {'root': {'title': '根', 'body': 'hello'},
                             'parent': {'status': 'same_as_root', 'id': 'not counted'},
                             'previous_exchange': {'messages': [{'title': 'old', 'body': 'text'}], 'more': True}}}
        self.assertEqual(agent_loop.delivered_chars(message), 19)
        with redirect_stdout(io.StringIO()) as output:
            outcome = agent_loop.deliver(message, {'scope': 'addressed', 'context': 'brief'})
        self.assertEqual(json.loads(output.getvalue()), message)
        self.assertEqual(outcome, 'unrecorded')

    def test_summary_failure_retains_checkpoint_and_replay_preserves_explicit_outcome(self):
        store = Store(self.root/'inbox.sqlite3')
        store.initialize()
        store.save('moltbook', uid(2), [dict(mail(10), addressing='direct'), dict(mail(11), addressing='thread')])
        checkpoint = self.root/'after.txt'
        checkpoint.write_text('0\n')
        args = ['--db', str(store.path), '--checkpoint', str(checkpoint), '--ledger', str(self.ledger), '--once']
        before = store.path.read_bytes()

        def handle(message, reading):
            if message.get('event') == 'thread_activity':
                raise RuntimeError('interrupted while handling the summary')
            return 'ignored'

        with patch.object(agent_loop, 'deliver', side_effect=handle):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                agent_loop.main(args)
        self.assertEqual(checkpoint.read_text(), '0\n')
        first = agent_loop.load_ledger(self.ledger)
        self.assertEqual([(entry['attempt'], entry['outcome']) for entry in first], [(1, 'ignored')])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(agent_loop.main(args), 0)
        self.assertEqual(checkpoint.read_text(), '2\n')
        entries = agent_loop.load_ledger(self.ledger)
        self.assertEqual([entry['event'] for entry in entries], ['delivery', 'delivery', 'thread_activity'])
        self.assertEqual(entries[1]['attempt'], 2)
        summary = agent_loop.summarize(entries)
        self.assertEqual((summary['unique_messages'], summary['delivery_attempts'], summary['summary_attempts']), (1, 2, 1))
        self.assertEqual(summary['outcomes']['ignored'], 1)
        self.assertEqual(summary['outcomes']['unrecorded'], 0)
        self.assertEqual(store.path.read_bytes(), before)

    def test_ledger_commands_distinguish_replays_outcomes_and_reading_modes(self):
        first_reading = {'scope': 'addressed', 'context': 'brief'}
        second_reading = {'scope': 'all', 'context': 'none'}
        def delivery(source, attempt, reading):
            agent_loop.append_ledger(self.ledger, {'event': 'delivery', 'source': source, 'id': 'same-id',
                'arrival_seq': 1, 'attempt': attempt, 'reading': reading, 'delivered_chars': 11,
                'outcome': 'unrecorded'})
        def cli(*args):
            return subprocess.run([sys.executable, str(Path(agent_loop.__file__)), '--ledger', str(self.ledger), *args],
                                  capture_output=True, text=True, timeout=10)
        delivery('one', 1, first_reading)
        recorded = cli('--record-outcome', 'one', 'same-id', '--outcome', 'resolved', '--ref', 'receipt:123')
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)['attempt'], 1)
        delivery('one', 2, second_reading)
        delivery('two', 1, first_reading)
        result = cli('--summarize')
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual((summary['unique_messages'], summary['delivery_attempts'], summary['delivered_chars']), (2, 3, 33))
        self.assertEqual(summary['outcomes']['resolved'], 1)
        self.assertEqual(summary['outcomes']['unrecorded'], 1)
        self.assertEqual(summary['by_reading']['all/none']['outcomes']['unrecorded'], 1)
        self.assertEqual(summary['by_reading']['addressed/brief']['outcomes']['resolved'], 1)
        before = self.ledger.read_bytes()
        self.assertNotEqual(cli('--record-outcome', 'absent', 'same-id', '--outcome', 'acted').returncode, 0)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_failed_ledger_write_does_not_advance_the_delivery_checkpoint(self):
        store = Store(self.root/'inbox.sqlite3')
        store.initialize()
        store.save('moltbook', uid(2), [dict(mail(10), addressing='direct')])
        checkpoint = self.root/'after.txt'
        with patch.object(agent_loop, 'append_ledger', side_effect=OSError('ledger unavailable')):
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(OSError, 'ledger unavailable'):
                    agent_loop.main(['--db', str(store.path), '--checkpoint', str(checkpoint),
                                     '--ledger', str(self.ledger), '--once'])
        self.assertFalse(checkpoint.exists())


if __name__ == '__main__':
    unittest.main()
