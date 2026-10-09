"""Every error code keeps its next-step hint, its next call and its exit code, and the catalog has every code the
package raises."""
import ast
from http.client import HTTPException
import inspect
from pathlib import Path
import unittest

import boardmail
from boardmail import adapter_colony, commands, errors, mcp, table, transport
from examples.fixtures import status


TESTS = Path(__file__).resolve().parent
PACKAGE = Path(boardmail.__file__).resolve().parent
# The stored rows for codes the catalog does not list.
UNLISTED = {'a_code_from_a_custom_adapter', 'http_500'}
# The codes of a pass that could not finish. Collecting again can work for them, and for no other code of the catalog.
UNFINISHED = {'budget_exhausted', 'collection_conflict', 'invalid_response', 'network_error',
              'pagination_no_progress', 'pending_overflow', 'source_timeout'}
# The codes that 4claw and Fruitflies had for a failed request while each board called one in its own way.
GONE = ('fourclaw_http_error', 'fourclaw_invalid_public_page', 'fourclaw_network_error', 'network_timeout')
# The codes whose next step is one call: the command of that call, and what it takes from the call that failed.
# The read of the journal is the step after a reply command that was refused for what the journal holds, and
# after one that could not read or write the inbox file.
JOURNAL = ('reply_show', ('source', 'id'))
NEXT = {'database_exists': ('status', ()), 'database_missing': ('init', ()), 'invalid_settings': ('settings', ()),
        'local_state_error': JOURNAL, 'message_not_found': ('list', ('source',)),
        'reply_already_recorded': JOURNAL, 'reply_already_started': JOURNAL, 'reply_body_conflict': JOURNAL,
        'reply_candidate_limit': JOURNAL, 'reply_key_mismatch': JOURNAL, 'reply_not_prepared': JOURNAL,
        'reply_not_started': JOURNAL, 'reply_readback_mismatch': JOURNAL, 'reply_reference_conflict': JOURNAL,
        'source_not_found': ('status', ()), 'source_paused': ('status', ())}
# The raises that build their code at run time. The stored table has rows for what they build.
BUILT = {'adapter_botnet.py: transport.failure(exc)',         # what a request to Botnet that failed is called
         'adapter_common.py: code',                           # the code of a refusal that a board explains: a Colony sign-in code
         'adapter_clawdchat.py: code',                        # what a request to ClawdChat that failed is called
         'errors.py: error',                                  # the code that a call names with error=
         'table.py: str(exc)',                                # the code of a check, with the name of its argument
         "adapter_moltbook.py: error or 'reply_' + status"}   # reply_deleted, reply_missing or a lookup error


def stored():
    """The stored table: for each code, its next-step hint and its exit code."""
    rows = [line.split() for line in (TESTS / 'error_codes.txt').read_text(encoding='utf-8').splitlines()]
    return {code: (hint, int(exit_code)) for code, exit_code, hint in rows}


def modules():
    return [(path.name, ast.parse(path.read_text(encoding='utf-8'))) for path in sorted(PACKAGE.glob('*.py'))]


def raised():
    """(the codes the package raises as literals, each raise that builds its code as 'module: expression')

    A code that a call names with error= counts as a literal: errors.converted() raises it, and so does the
    command table for an argument."""
    literal, built = set(), set()
    for name, tree in modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            literal.update(named.value.value for named in node.keywords
                           if named.arg == 'error' and isinstance(named.value, ast.Constant))
            if getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'MailError':
                code, = node.args
                if isinstance(code, ast.Constant):
                    literal.add(code.value)
                else:
                    built.add(f'{name}: {ast.unparse(code)}')
    return literal, built


class ErrorCodeTests(unittest.TestCase):
    def assertEntry(self, code, entry):
        result, exit_code = commands.error_result(code)
        self.assertEqual((code, result['next_action'], exit_code), (code, *entry))

    def test_every_code_keeps_its_hint_and_exit_code(self):
        table = stored()
        self.assertEqual(set(table), set(errors.CODES) | UNLISTED)
        for code, entry in table.items():
            self.assertEntry(code, entry)

    def test_a_hint_names_a_step_that_can_work(self):
        hints = {code: errors.next_action(code) for code in errors.CODES}
        self.assertEqual({code for code, hint in hints.items() if hint == 'retry_collect'}, UNFINISHED)
        self.assertEqual(errors.next_action('a_code_from_a_custom_adapter'), 'retry_collect')
        # A tool call has no help page to read.
        self.assertEqual({code: hint for code, hint in hints.items() if 'help' in hint}, {})

    def test_an_error_names_the_argument_that_it_is_given(self):
        self.assertEqual(commands.error_result('invalid_arguments', 'limit')[0],
                         {'event': 'error', 'error': 'invalid_arguments', 'argument': 'limit',
                          'next_action': 'fix_the_arguments', 'history_complete': False})
        self.assertNotIn('argument', commands.error_result('invalid_arguments')[0])
        error = errors.MailError('invalid_arguments', argument='limit')
        self.assertEqual((str(error), error.argument, errors.MailError('invalid_arguments').argument),
                         ('invalid_arguments', 'limit', None))
        with self.assertRaises(errors.MailError) as raised:
            errors.converted(int, 'many', error='invalid_arguments', argument='limit')
        self.assertEqual((str(raised.exception), raised.exception.argument), ('invalid_arguments', 'limit'))
        result, exit_code = commands.outcome(lambda: errors.converted(int, 'many', error='invalid_mark', argument='ref'))
        self.assertEqual((result['error'], result['argument'], exit_code), ('invalid_mark', 'ref', 2))

    def test_an_error_names_its_next_call_where_the_step_is_one(self):
        self.assertEqual({code: (entry.next.command, entry.next.takes) for code, entry in errors.CODES.items()
                          if entry.next}, NEXT)
        given = {'source': 'alias', 'id': 'numeric-20', 'key': 'an-invented-key', 'body': 'An invented answer.'}
        for code, (command, takes) in NEXT.items():
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
        for code in (set(errors.CODES) | UNLISTED) - set(NEXT):
            self.assertNotIn('next', commands.error_result(code, call=given)[0], code)

    def test_an_http_status_without_a_row_is_treated_like_500(self):
        table = stored()
        for status in range(100, 600):
            self.assertEntry(f'http_{status}', table.get(f'http_{status}', table['http_500']))

    def test_a_code_the_package_raises_is_in_the_catalog(self):
        literal, built = raised()
        self.assertEqual(literal - set(errors.CODES), set())
        self.assertEqual(built, BUILT)
        self.assertEqual({code.lower() for code in adapter_colony.COLONY_AUTH_CODES} - set(errors.CODES), set())
        # What transport.failure() calls a request that failed with no code of its own.
        called = {transport.failure(exc) for exc in (OSError(), HTTPException(), ValueError())}
        self.assertEqual(called - set(errors.CODES), set())
        self.assertEqual(UNLISTED & set(errors.CODES), set())

    def test_a_failed_request_is_called_the_same_whichever_board_it_was_sent_to(self):
        # What a failure is called is asked without the board, and the row of a board says nothing of it: it
        # holds what the board is asked for, and how large and how slow its answer may be.
        self.assertEqual(list(inspect.signature(transport.failure).parameters), ['exc'])
        self.assertEqual(transport.Board._fields, ('accept', 'protocol', 'kind', 'cap', 'silence', 'budget'))
        for exc, code in ((errors.MailError('source_timeout'), 'source_timeout'), (status(418), 'http_418'),
                          (ConnectionRefusedError(), 'network_error'), (TimeoutError(), 'network_error'),
                          (HTTPException(), 'network_error'), (ValueError(), 'invalid_response'),
                          (KeyError('id'), 'invalid_response')):
            with self.subTest(failed=type(exc).__name__):
                self.assertEqual(transport.failure(exc), code)
        # The codes that only one board had are nowhere in the package, so no pass and no command gives one.
        package = ''.join(path.read_text(encoding='utf-8') for path in sorted(PACKAGE.glob('*.py')))
        self.assertEqual([code for code in GONE if code in package], [])

    def test_mcp_flags_every_error_code(self):
        for code in [*errors.CODES, *UNLISTED]:
            self.assertTrue(errors.mcp_error(errors.exit_code(code)), code)
        self.assertEqual([status for status in range(6) if errors.mcp_error(status)], [1, 2, 5])

    def test_the_catalog_is_at_the_bottom_of_the_import_graph(self):
        for name, tree in modules():
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    reached = {getattr(node, 'module', None), *(alias.name for alias in node.names)} - {None}
                    inside = getattr(node, 'level', 0) > 0 or any(to.split('.')[0] == 'boardmail' for to in reached)
                    if name == 'errors.py':
                        self.assertFalse(inside, 'The catalog imports from the package')
                    elif inside and any(to.split('.')[-1] == 'errors' for to in reached):
                        self.assertIn(node, tree.body, f'{name} imports the catalog inside a function')


if __name__ == '__main__':
    unittest.main()
