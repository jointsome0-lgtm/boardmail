"""Package modules import each other one way: at the top of the file, with no cycle, and no private name of another."""
import ast
import inspect
from pathlib import Path
import unittest

import boardmail
from boardmail.boards import BOARDS

PACKAGE = Path(boardmail.__file__).resolve().parent
# What reads a command from outside. No module of the package imports one of these, but the one that starts the CLI.
ENTRIES = {'cli', 'mcp', '__main__'}
# What a named tuple offers everyone under a leading underscore.
TUPLE = {'_replace', '_asdict', '_fields', '_make'}


def imported(node):
    """The package modules that an import statement names."""
    if isinstance(node, ast.Import):
        return {alias.name.split('.')[1] for alias in node.names if alias.name.startswith('boardmail.')}
    if not isinstance(node, ast.ImportFrom) or not (node.level or (node.module or '').split('.')[0] == 'boardmail'):
        return set()
    module = (node.module or '').removeprefix('boardmail').lstrip('.')
    return {module.split('.')[0]} if module else {alias.name for alias in node.names}


def read(source, own, modules):
    """(modules imported anywhere, imports inside a function, private names of another module) of one source."""
    tree = ast.parse(source)
    top = set().union(*(imported(node) for node in ast.walk(tree))) & modules - {own}
    inside = sorted(f'line {node.lineno}' for scope in ast.walk(tree) if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                    for node in ast.walk(scope) if imported(node))
    private = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and imported(node):
            private += [alias.name for alias in node.names if alias.name.startswith('_') and not alias.name.startswith('__')]
        # A private name on anything but the object itself: on a module, or on what another module handed over.
        if (isinstance(node, ast.Attribute) and node.attr.startswith('_') and not node.attr.startswith('__') and node.attr not in TUPLE
                and not (isinstance(node.value, ast.Name) and node.value.id in ('self', 'cls'))):
            private.append(ast.unparse(node))
    return top, inside, sorted(private)


def cycle(graph):
    """Some cycle of the graph as a list of modules, or None."""
    done, path = set(), []

    def walk(module):
        if module in path:
            return path[path.index(module):] + [module]
        if module in done:
            return None
        path.append(module)
        found = next((found for found in map(walk, sorted(graph[module])) if found), None)
        path.pop()
        done.add(module)
        return found
    return next((found for found in map(walk, sorted(graph)) if found), None)


class ImportGraphTests(unittest.TestCase):
    def setUp(self):
        sources = {path.stem: path.read_text(encoding='utf-8') for path in PACKAGE.glob('*.py')}
        self.read = {name: read(source, name, set(sources)) for name, source in sources.items()}
        self.graph = {name: top for name, (top, _, _) in self.read.items()}

    def test_no_import_between_package_modules_sits_inside_a_function(self):
        self.assertEqual({name: inside for name, (_, inside, _) in self.read.items() if inside}, {})

    def test_no_private_name_is_reached_across_modules(self):
        self.assertEqual({name: private for name, (_, _, private) in self.read.items() if private}, {})

    def test_the_import_graph_has_no_cycle(self):
        self.assertIsNone(cycle(self.graph))
        self.assertGreater(sum(map(len, self.graph.values())), 40)

    def test_entries_are_at_the_top_and_the_error_catalog_at_the_bottom(self):
        self.assertEqual(self.graph['errors'], set())
        importers = {name: sorted(module for module, top in self.graph.items() if name in top) for name in ENTRIES}
        self.assertEqual(importers, {'cli': ['__main__'], 'mcp': [], '__main__': []})

    def test_only_the_list_of_boards_imports_the_module_of_a_board(self):
        # The module of a board is where its collector is. One board is removed by removing its module and its
        # line in the list, so nothing else may lean on it: no core module and no other board.
        boards = {Path(inspect.getfile(getattr(about.collect, 'func', about.collect))).stem for about in BOARDS.values()}
        self.assertEqual(len(boards), len(BOARDS))
        self.assertEqual({name: sorted(top & boards) for name, top in self.graph.items() if top & boards},
                         {'boards': sorted(boards)})

    def test_the_guard_sees_each_of_the_three(self):
        modules = {'one', 'two', 'three'}
        top, inside, private = read('from . import two\nfrom .three import name, _hidden\n'
                                    'def call(store):\n    from .two import other\n    return two._inner, store._row, self._own, row._replace(a=1)\n',
                                    'one', modules)
        self.assertEqual((top, inside, private), ({'two', 'three'}, ['line 4'], ['_hidden', 'store._row', 'two._inner']))
        self.assertEqual(read('import boardmail.two\ntry:\n    from boardmail import three\nexcept ImportError:\n    pass\nimport json\n',
                              'one', modules)[0], {'two', 'three'})
        self.assertIsNone(cycle({'one': {'two', 'three'}, 'two': {'three'}, 'three': set()}))
        self.assertEqual(cycle({'one': {'two'}, 'two': {'three'}, 'three': {'one'}}), ['one', 'two', 'three', 'one'])


if __name__ == '__main__':
    unittest.main()
