"""Error guidance uses fixed codes and read-only recovery for invented state."""
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from boardmail import commands
from boardmail.config import MailError
from kit import arrive, new_inbox
from test_mail import mail

SECRET = '/invented/private/path token=do-not-expose'


def sqlite_error(code):
    error = sqlite3.OperationalError(SECRET)
    error.sqlite_errorcode = code
    return error


def fail(error):
    raise error


class CommandErrorTests(unittest.TestCase):
    def test_known_local_reasons_use_codes_without_exception_text(self):
        errors = [(PermissionError(errno.EACCES, SECRET), 'permission_denied'),
                  (OSError(errno.EPERM, SECRET), 'permission_denied'),
                  (OSError(errno.EROFS, SECRET), 'read_only'),
                  (sqlite_error(sqlite3.SQLITE_READONLY | (2 << 8)), 'read_only'),
                  (sqlite_error(sqlite3.SQLITE_PERM), 'permission_denied')]
        for error, reason in errors:
            with self.subTest(code=getattr(error, 'errno', None), sqlite=getattr(error, 'sqlite_errorcode', None)):
                result, code = commands.outcome(lambda: fail(error))
                self.assertEqual((result['error'], code, result['reason']), ('local_state_error', 2, reason))
                self.assertEqual(result['next_action'], 'inspect_database_do_not_delete')
                self.assertFalse(result['history_complete'])
                self.assertNotIn(SECRET, json.dumps(result))

    def test_unknown_local_causes_remain_generic(self):
        for error in (sqlite_error(sqlite3.SQLITE_CANTOPEN), sqlite3.OperationalError('readonly '+SECRET),
                      PermissionError(SECRET), OSError(errno.EIO, SECRET), ValueError(SECRET),
                      KeyError(SECRET), TypeError(SECRET), OverflowError(SECRET)):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(commands.outcome(lambda: fail(error)), commands.error_result('local_state_error'))
        with self.assertRaises(RuntimeError):
            commands.outcome(lambda: fail(RuntimeError(SECRET)))

    def test_missing_message_hint_does_not_reinterpret_numeric_remote_id(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'invented.sqlite3'
            store = new_inbox(path)
            arrive(store, 'synthetic', 'owner', [dict(mail(10), id='1087')])
            before = path.read_bytes()
            result, code = commands.outcome(lambda: commands.execute(store, 'show', source='synthetic', id='1087'))
            self.assertEqual((code, result['message']['id']), (0, '1087'))
            missing, code = commands.outcome(lambda: commands.execute(store, 'show', source='synthetic', id='1'))
            self.assertEqual((missing['error'], code), ('message_not_found', 2))
            self.assertIn('source', missing['identifier_hint'])
            self.assertIn('remote', missing['identifier_hint'])
            self.assertIn('list', missing['identifier_hint'])
            self.assertIn('arrival_seq', missing['identifier_hint'])
            self.assertEqual(path.read_bytes(), before)

    def test_reply_write_failure_offers_exact_read_only_recovery_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'invented.sqlite3'
            store = new_inbox(path)
            arrive(store, 'synthetic', 'owner', [dict(mail(10), id='1087')])
            before = path.read_bytes()
            with patch('sqlite3.connect', side_effect=PermissionError(errno.EACCES, SECRET)):
                result, code = commands.outcome(lambda: commands.execute(store, 'reply_prepare',
                                    source='synthetic', id='1087', body='Invented answer.'))
            self.assertEqual((result['error'], code, result['reason']), ('local_state_error', 2, 'permission_denied'))
            self.assertEqual(result['next_action'], 'inspect_database_do_not_delete')
            self.assertFalse(result['send_allowed'])
            self.assertEqual(result['recovery'], {'command': 'reply show', 'tool': 'boardmail_reply_show',
                             'arguments': {'source': 'synthetic', 'id': '1087'}, 'read_only': True})
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertEqual(path.read_bytes(), before)
            args = result['recovery']['arguments']
            recovered, code = commands.execute(store, 'reply_show', **args)
            self.assertEqual((code, recovered['reply'], recovered['changed'], recovered['send_allowed']),
                             (0, None, False, False))
            self.assertEqual(path.read_bytes(), before)

    def test_reply_operations_keep_unknown_cause_and_never_authorize_sending(self):
        key, ref = {'key': 'an-invented-key'}, {'ref': 'https://example.invalid/an-answer'}
        given = {'reply_prepare': {'body': 'Invented answer.'}, 'reply_begin': key, 'reply_show': {},
                 'reply_confirm': {**key, **ref, 'readback_body': 'Invented answer.'}, 'reply_verify': {**key, **ref}}
        store = new_inbox(Path(self.enterContext(tempfile.TemporaryDirectory()))/'invented.sqlite3')
        for command in ('reply_prepare', 'reply_begin', 'reply_show', 'reply_confirm', 'reply_verify'):
            # The inbox file cannot be opened, and SQLite does not say why.
            with self.subTest(command=command), patch('sqlite3.connect', side_effect=sqlite_error(sqlite3.SQLITE_CANTOPEN)) as opened:
                result, code = commands.outcome(lambda: commands.execute(store, command, source='alias', id='numeric-20',
                                                                         **given[command]))
                opened.assert_called_once()
                self.assertEqual((result['error'], code), ('local_state_error', 2))
                self.assertNotIn('reason', result)
                self.assertFalse(result['send_allowed'])
                self.assertEqual(result['recovery']['arguments'], {'source': 'alias', 'id': 'numeric-20'})
                self.assertEqual(result['next_action'], 'inspect_database_do_not_delete')
                self.assertNotIn(SECRET, json.dumps(result))

    def test_invalid_target_does_not_produce_recovery_route(self):
        for source, mid in ((None, '10'), ('alias', None), ('alias', 'bad\nidentifier')):
            result, _ = commands.local_state_result(OSError(errno.EROFS, SECRET), source, mid)
            self.assertNotIn('recovery', result)
        result, code = commands.outcome(lambda: fail(MailError('invalid_arguments')))
        self.assertEqual((result['error'], code), ('invalid_arguments', 2))
        self.assertNotIn('identifier_hint', result)


if __name__ == '__main__':
    unittest.main()
