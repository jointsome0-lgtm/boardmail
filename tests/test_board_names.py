"""The core names no board. What a board is stands in its own module, and every other module asks boards.py."""
import ast
import inspect
from pathlib import Path
import unittest

import boardmail
from boardmail.boards import BOARDS

PACKAGE = Path(boardmail.__file__).resolve().parent
# The module of a board is where its collector is. Every other module of the package is core.
BOARD_MODULES = {Path(inspect.getfile(getattr(about.collect, 'func', about.collect))).name for about in BOARDS.values()}


def named(source):
    """(line, name) for each string constant of the source that is the name of a board. A text that mentions a
    board is longer than its name, so it is none."""
    return sorted((node.lineno, node.value) for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BOARDS)


class BoardNameTests(unittest.TestCase):
    def test_no_core_module_holds_the_name_of_a_board(self):
        core = {path.name: named(path.read_text(encoding='utf-8')) for path in PACKAGE.glob('*.py')
                if path.name not in BOARD_MODULES}
        # The four modules that held the names, and the three that the boards are declared and asked through.
        self.assertLessEqual({'config.py', 'commands.py', 'verification.py', 'reader.py',
                              'adapters.py', 'boards.py', 'transport.py'}, set(core))
        self.assertEqual({name: lines for name, lines in core.items() if lines}, {})

    def test_the_guard_sees_a_name_and_lets_a_text_mention_one(self):
        for name in BOARDS:
            with self.subTest(name=name):
                self.assertEqual(named(f'if adapter == {name!r}:\n    pass\n'), [(1, name)])
                self.assertEqual(named(f'HOSTS = {{{name!r}: 1}}\nfor adapter in ({name!r}, "other"):\n    pass\n'),
                                 [(1, name), (2, name)])
                self.assertEqual(named(f'HELP = "Mail of {name} is collected like any other."\n'), [])


if __name__ == '__main__':
    unittest.main()
