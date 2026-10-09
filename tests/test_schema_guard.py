"""Only boardmail/schema.py says what the inbox file is made of, and only it asks the file what it has. Only the
store calls it: where it opens a file, which gives an older file its parts, and where init creates one. So no
command asks whether a part of the file is there."""
import ast
from pathlib import Path
import re
import unittest

import boardmail


PACKAGE = Path(boardmail.__file__).resolve().parent
OTHERS = [path for path in sorted(PACKAGE.rglob('*.py')) if path.name != 'schema.py']
# A statement that gives the file a part or takes one away, and the two ways to ask the file what it has.
SHAPE = re.compile(r'\b(?:CREATE|ALTER|DROP)\s+(?:(?:TEMP|TEMPORARY|UNIQUE|VIRTUAL)\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)\b'
                   r'|\bPRAGMA\b|\bsqlite_(?:master|schema)\b', re.IGNORECASE)
# Every use of the schema module by another module: what the store does when it opens a file, and init.
USES = ['store.py: Store._open: schema.check_version', 'store.py: Store.connect: schema.complete',
        'store.py: Store.connect: schema.lacks', 'store.py: Store.initialize: schema.create']
# The store makes the connection, so it alone is told whether a connection writes.
CONNECTS = ['store.py: Store._open', 'store.py: Store.connect']
DEFINED = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def found(path):
    """Each text in a module that holds such a statement or question, with the line it starts on."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [f'{path.name}:{node.lineno}: {" ".join(node.value.split())[:60]}' for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SHAPE.search(node.value)]


def inside(path):
    """Each node of a module with the name of the class and the function that it is in. A definition is in
    itself, and what stands outside every definition is in ''."""
    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            here = f'{name}.{child.name}'.lstrip('.') if isinstance(child, DEFINED) else name
            yield child, here
            yield from walk(child, here)

    return walk(ast.parse(path.read_text(encoding='utf-8')), '')


def imported(path):
    """The names under which a module holds the schema module, or something of it."""
    names = []
    for node, _ in inside(path):
        if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[-1] == 'schema':
            names += [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names += [alias.asname or alias.name for alias in node.names if alias.name.split('.')[-1] == 'schema']
    return names


class SchemaGuardTests(unittest.TestCase):
    def test_no_other_module_says_or_asks_what_the_file_has(self):
        others = [line for path in OTHERS for line in found(path)]
        self.assertEqual(others, [], 'These belong in boardmail/schema.py')

    def test_the_guard_finds_each_kind_in_the_schema_module(self):
        # If it found none of them there, the test above would say nothing.
        texts = '\n'.join(found(PACKAGE / 'schema.py'))
        for kind in ('CREATE TABLE', 'CREATE INDEX', 'ALTER TABLE', 'PRAGMA user_version', 'PRAGMA table_info',
                     'sqlite_master'):
            self.assertIn(kind, texts)

    def test_only_the_store_calls_the_schema_module_where_it_opens_a_file_and_where_init_creates_one(self):
        self.assertEqual({path.name: imported(path) for path in OTHERS if imported(path)}, {'store.py': ['schema']})
        store = list(inside(PACKAGE / 'store.py'))
        uses = [f'store.py: {name}: schema.{node.attr}' for node, name in store
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'schema']
        self.assertEqual(sorted(uses), USES)
        # The module is not handed on under its name either.
        self.assertEqual(len([node for node, _ in store if isinstance(node, ast.Name) and node.id == 'schema']),
                         len(USES))

    def test_no_other_function_is_told_whether_its_connection_writes(self):
        told = []
        for path in OTHERS:
            for node, name in inside(path):
                if isinstance(node, DEFINED[1:]):
                    takes = node.args
                    arguments = [*takes.posonlyargs, *takes.args, *takes.kwonlyargs, takes.vararg, takes.kwarg]
                    if {'write', 'writing'} & {argument.arg for argument in arguments if argument}:
                        told.append(f'{path.name}: {name}')
        self.assertEqual(told, CONNECTS)

    def test_only_the_step_that_gives_an_older_file_its_parts_asks_which_boards_version_1_had(self):
        # A version-1 file is a version-2 file after the first command, so nothing else has a branch for one.
        asks = [f'{path.name}: {name}' for path in OTHERS for node, name in inside(path)
                if isinstance(node, ast.Attribute) and node.attr == 'since_v1']
        self.assertEqual(asks, ['store.py: Store.connect'])


if __name__ == '__main__':
    unittest.main()
