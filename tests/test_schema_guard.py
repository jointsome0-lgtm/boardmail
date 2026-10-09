"""Only boardmail/schema.py says what the inbox file is made of, and only it asks the file what it has."""
import ast
from pathlib import Path
import re
import unittest

import boardmail


PACKAGE = Path(boardmail.__file__).resolve().parent
# A statement that gives the file a part or takes one away, and the two ways to ask the file what it has.
SHAPE = re.compile(r'\b(?:CREATE|ALTER|DROP)\s+(?:(?:TEMP|TEMPORARY|UNIQUE|VIRTUAL)\s+)?(?:TABLE|INDEX|VIEW|TRIGGER)\b'
                   r'|\bPRAGMA\b|\bsqlite_(?:master|schema)\b', re.IGNORECASE)


def found(path):
    """Each text in a module that holds such a statement or question, with the line it starts on."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return [f'{path.name}:{node.lineno}: {" ".join(node.value.split())[:60]}' for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SHAPE.search(node.value)]


class SchemaGuardTests(unittest.TestCase):
    def test_no_other_module_says_or_asks_what_the_file_has(self):
        others = [line for path in sorted(PACKAGE.rglob('*.py')) if path.name != 'schema.py' for line in found(path)]
        self.assertEqual(others, [], 'These belong in boardmail/schema.py')

    def test_the_guard_finds_each_kind_in_the_schema_module(self):
        # If it found none of them there, the test above would say nothing.
        texts = '\n'.join(found(PACKAGE / 'schema.py'))
        for kind in ('CREATE TABLE', 'CREATE INDEX', 'ALTER TABLE', 'PRAGMA user_version', 'PRAGMA table_info',
                     'sqlite_master'):
            self.assertIn(kind, texts)


if __name__ == '__main__':
    unittest.main()
