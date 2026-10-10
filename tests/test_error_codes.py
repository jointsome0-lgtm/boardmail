"""What an error gives besides its code: its next-step hint, its next call and its exit code."""
from http.client import HTTPException
from pathlib import Path
import re
import unittest

from boardmail import commands, errors, mcp, table, transport
from examples.fixtures import status


TESTS = Path(__file__).resolve().parent
# Codes that the catalog does not list.
UNLISTED = {'a_code_from_a_custom_adapter', 'http_500'}
# The read of the journal is the step after a reply command that was refused for what the journal holds, and
# after one that could not read or write the inbox file.
JOURNAL = ('reply_show', ('source', 'id'))


class ErrorCodeTests(unittest.TestCase):
    def test_an_error_names_its_next_call_where_the_step_is_one(self):
        steps = {code: (entry.next.command, entry.next.takes) for code, entry in errors.CODES.items() if entry.next}
        self.assertEqual(steps['reply_not_started'], JOURNAL)
        given = {'source': 'alias', 'id': 'numeric-20', 'key': 'an-invented-key', 'body': 'An invented answer.'}
        for code, (command, takes) in steps.items():
            with self.subTest(code=code):
                result, _ = commands.error_result(code, call=given)
                self.assertEqual(list(result), ['event', 'error', 'next_action', 'next', 'history_complete'])
                self.assertEqual((sorted(result['next']), result['next']['tool']),
                                 (['arguments', 'tool'], 'boardmail_' + command))
                # The call has the name and the id of the call that failed, and no other word of it.
                arguments = result['next']['arguments']
                self.assertEqual({name: arguments[name] for name in arguments.keys() & given.keys()},
                                 {name: given[name] for name in takes})
                # The command takes it as it is written.
                table.checked(table.COMMANDS[command], arguments)
                # A call that gave no name or id, or one that cannot be one, leaves the step nothing to take.
                for lacking in ({}, dict(given, source=None), dict(given, id='two\nlines', source='')):
                    self.assertEqual('next' in commands.error_result(code, call=lacking)[0], not takes)
                # Nor does a name or an id that is longer than the tool of the step takes.
                takes_up_to = mcp.input_schema(table.COMMANDS[command])['properties']
                for name in takes:
                    longest = takes_up_to[name]['maxLength']
                    self.assertIn('next', commands.error_result(code, call=dict(given, **{name: 'x' * longest}))[0])
                    self.assertNotIn('next', commands.error_result(code, call=dict(given, **{name: 'x' * (longest + 1)}))[0])
        for code in (set(errors.CODES) | UNLISTED) - set(steps):
            self.assertNotIn('next', commands.error_result(code, call=given)[0], code)

    def test_an_http_status_without_a_row_is_treated_like_500(self):
        hint, exit_code = commands.error_result('http_500')[0]['next_action'], commands.error_result('http_500')[1]
        for code in (f'http_{number}' for number in range(100, 600)):
            if code not in errors.CODES:
                result, status_of_exit = commands.error_result(code)
                self.assertEqual((code, result['next_action'], status_of_exit), (code, hint, exit_code))

    def test_an_adapter_file_is_told_what_the_boards_of_the_package_call_a_failed_request(self):
        notes = (TESTS.parent / 'ADAPTERS.md').read_text(encoding='utf-8')
        told = notes.split('\n## What a failure is called\n')[1].split('\n## ')[0]
        listed = dict(re.findall(r'^\| `(\w+)` \|.*\| `(\w+)` \|$', told, re.MULTILINE))
        # Each row of the table is read: the table has no row besides its two head lines that is not a code.
        self.assertEqual(len([line for line in told.splitlines() if line.lstrip().startswith('|')]), len(listed) + 2)
        # Each code of the list is one of the catalog, and the list says the hint that a result gives for it.
        self.assertEqual(set(listed) - set(errors.CODES), set())
        self.assertEqual({code: errors.next_action(code) for code in listed}, listed)
        # The list has what failure() calls a failed request that has no code of its own. A status has its number
        # in its code, and the list has the three statuses that have a hint of their own.
        called = {transport.failure(exc) for exc in (OSError(), HTTPException(), ValueError())}
        self.assertEqual((called | {code for code in errors.CODES if code.startswith('http_')}) - set(listed), set())
        # Any other code gets the hint that the notes say: a status that has no entry, the one that they name
        # among them, and a code of the file's own.
        self.assertIn('Every code without an entry gets `retry_collect`.', told)
        for code in ('http_503', transport.failure(status(404)), 'a_code_from_a_custom_adapter'):
            self.assertNotIn(code, errors.CODES)
            self.assertEqual(errors.next_action(code), 'retry_collect')
        self.assertIn('`http_503`', told)


if __name__ == '__main__':
    unittest.main()
