"""Every error code keeps its next-step hint and its exit code, and the catalog has every code the package raises."""
import ast
from pathlib import Path
import unittest

import boardmail
from boardmail import adapter_colony, adapter_common, commands, errors, transport


TESTS = Path(__file__).resolve().parent
PACKAGE = Path(boardmail.__file__).resolve().parent
# The stored rows for codes the catalog does not list.
UNLISTED = {'a_code_from_a_custom_adapter', 'http_500'}
# The codes of a pass that could not finish. Collecting again can work for them, and for no other code of the catalog.
UNFINISHED = {'budget_exhausted', 'collection_conflict', 'fourclaw_http_error', 'fourclaw_invalid_public_page',
              'fourclaw_network_error', 'invalid_response', 'network_error', 'network_timeout',
              'pagination_no_progress', 'pending_overflow', 'source_timeout'}
# The raises that build their code at run time. The stored table has rows for what they build.
BUILT = {"adapter_botnet.py: transport.failure('botnet', exc)",  # what Botnet calls a request that failed
         'adapter_common.py: code',                              # the code of a refusal that a board explains: a Colony sign-in code
         'adapter_clawdchat.py: code',                           # what ClawdChat calls a request that failed
         'errors.py: error',                                     # the code that a call names with error=
         'table.py: str(exc)',                                   # the code of a check, with the name of its argument
         "adapter_moltbook.py: error or 'reply_' + status",      # reply_deleted, reply_missing or a lookup error
         'transport.py: about.large',                            # what a board calls an answer over its size cap
         'transport.py: about.late',                             # what a board calls an answer that is late
         'transport.py: self.refused'}                           # what a board calls a redirect


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

    def test_an_http_status_without_a_row_is_treated_like_500(self):
        table = stored()
        for status in range(100, 600):
            self.assertEntry(f'http_{status}', table.get(f'http_{status}', table['http_500']))

    def test_a_code_the_package_raises_is_in_the_catalog(self):
        literal, built = raised()
        self.assertEqual(literal - set(errors.CODES), set())
        self.assertEqual(built, BUILT)
        self.assertEqual({code.lower() for code in adapter_colony.COLONY_AUTH_CODES} - set(errors.CODES), set())
        named = {code for about in transport.BOARDS.values()
                 for code in (about.late, about.large, about.network, about.content, about.status, about.redirect)}
        self.assertEqual(named - set(errors.CODES) - {None}, set())
        self.assertEqual(UNLISTED & set(errors.CODES), set())

    def test_one_board_answers_for_the_boards_that_call_a_failure_alike(self):
        # adapter_common.error_code() names a failure for the three boards that it serves without knowing which one
        # it was, and the reply checks call it for ClawdChat too.
        def called(board):
            about = transport.BOARDS[board]
            return about.network, about.content, about.statuses, about.status, about.redirect
        row = adapter_common.ROW
        self.assertEqual({called(board) for board in ('postingboard', 'the-colony', 'moltbook', 'clawdchat')},
                         {(row.network, row.content, row.statuses, row.status, row.redirect)})

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
