"""Every error code keeps its next-step hint and its exit code, and the catalog has every code the package raises."""
import ast
from pathlib import Path
import unittest

import boardmail
from boardmail import commands, errors, providers, transport


TESTS = Path(__file__).resolve().parent
PACKAGE = Path(boardmail.__file__).resolve().parent
# The stored rows for codes the catalog does not list.
UNLISTED = {'a_code_from_a_custom_adapter', 'http_500'}
# The raises that build their code at run time. The stored table has rows for what they build.
BUILT = {"adapter_botnet.py: transport.failure('botnet', exc)",  # what Botnet calls a request that failed
         'adapter_clawdchat.py: code',                           # what ClawdChat calls a request that failed
         'config.py: error',                                     # the code that a call names with error=
         'providers.py: code',                                   # a Colony sign-in code
         'transport.py: about.large',                            # what a board calls an answer over its size cap
         'transport.py: about.late',                             # what a board calls an answer that is late
         'transport.py: self.refused',                           # what a board calls a redirect
         "verification.py: error or 'reply_' + status"}          # reply_deleted, reply_missing or a lookup error


def stored():
    """The stored table: for each code, its next-step hint and its exit code."""
    rows = [line.split() for line in (TESTS / 'error_codes.txt').read_text(encoding='utf-8').splitlines()]
    return {code: (hint, int(exit_code)) for code, exit_code, hint in rows}


def modules():
    return [(path.name, ast.parse(path.read_text(encoding='utf-8'))) for path in sorted(PACKAGE.glob('*.py'))]


def raised():
    """(the codes the package raises as literals, each raise that builds its code as 'module: expression')

    A code that a call names with error= counts as a literal: config.converted() raises it, and so does the
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

    def test_an_http_status_without_a_row_is_treated_like_500(self):
        table = stored()
        for status in range(100, 600):
            self.assertEntry(f'http_{status}', table.get(f'http_{status}', table['http_500']))

    def test_a_code_the_package_raises_is_in_the_catalog(self):
        literal, built = raised()
        self.assertEqual(literal - set(errors.CODES), set())
        self.assertEqual(built, BUILT)
        self.assertEqual({code.lower() for code in providers.COLONY_AUTH_CODES} - set(errors.CODES), set())
        named = {code for about in transport.BOARDS.values()
                 for code in (about.late, about.large, about.network, about.content, about.status, about.redirect)}
        self.assertEqual(named - set(errors.CODES) - {None}, set())
        self.assertEqual(UNLISTED & set(errors.CODES), set())

    def test_one_board_answers_for_the_boards_that_call_a_failure_alike(self):
        # providers.error_code() names a failure for the three boards of its module without knowing which one
        # it was, and the reply checks call it for ClawdChat too.
        def called(board):
            about = transport.BOARDS[board]
            return about.network, about.content, about.statuses, about.status, about.redirect
        self.assertEqual({called(board) for board in (*providers.HOSTS, 'clawdchat')}, {called(providers.ANY)})

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
