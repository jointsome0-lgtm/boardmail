"""The stored tests that reach inside the package follow the tests, and the counting rule is pinned."""
import ast
import codecs
import importlib.util
from pathlib import Path
import re
import tempfile
import unittest


TESTS = Path(__file__).resolve().parent
COUNTER_PATH = TESTS.parent / 'scripts/inner_reach.py'
spec = importlib.util.spec_from_file_location('inner_reach', COUNTER_PATH)
counter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counter)
CASES = TESTS / 'fixtures/inner_reach'


class InnerReachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.counted = counter.analyse()

    def test_stored_tests_are_the_ones_that_reach_inside(self):
        stored, total = counter.read_stored()
        current = counter.counted(self.counted)
        new, gone = sorted(set(current) - set(stored)), sorted(set(stored) - set(current))
        problems = []
        if new:
            problems += ['These tests reach inside and are not stored. A new test never reaches inside; '
                         'python scripts/inner_reach.py --list shows why a test is counted.', *new]
        if gone:
            problems += ['These stored tests no longer reach inside. '
                         'Drop them with python scripts/inner_reach.py --update', *gone]
        if not problems and not total == len(stored) == len(current):
            problems += [f'{len(current)} tests reach inside, {len(stored)} lines name them '
                         f'and the stored total is {total}.']
        if problems:
            self.fail('\n' + '\n'.join(problems))

    def test_every_test_unittest_runs_is_counted(self):
        loader = unittest.TestLoader()
        loaded, pending = set(), [loader.discover(str(TESTS))]
        if loader.errors:
            self.fail('The test files do not load here. The first error:\n' + loader.errors[0])
        while pending:
            item = pending.pop()
            if isinstance(item, unittest.TestSuite):
                pending.extend(item)
            else:
                loaded.add(item.id())
        self.assertEqual({f'{name[:-3]}.{test}' for name, tests in self.counted.items() for test in tests}, loaded)

    def test_rule_examples(self):
        expected, examples = {}, ('cases', 'imports')
        for name in examples:
            tree = ast.parse((CASES / f'{name}.py').read_text(encoding='utf-8'))
            for cls in tree.body:
                for item in cls.body if isinstance(cls, ast.ClassDef) else ():
                    if isinstance(item, ast.FunctionDef) and item.name.startswith('test'):
                        for line in filter(None, ast.get_docstring(item).splitlines()):
                            runner, report = re.fullmatch(r'(?:In (\w+): )?(.+)', line).groups()
                            expected[f'{name}.py {runner or cls.name}.{item.name}'] = report
        counted = counter.analyse({'': CASES}, examples)
        self.assertEqual({f'{name} {test}': '; '.join(found) or 'not counted'
                          for name, tests in counted.items() for test, found in tests.items()}, expected)

    def test_reads_what_python_reads(self):
        # The sources are put together here, so that this file holds no source for the count to follow.
        store = f'from {counter.PACKAGE}.store import Store\n'
        marked = codecs.BOM_UTF8 + (
            'import unittest\n' + store + 'import declared\n'
            'class Tests(unittest.TestCase):\n'
            '    def test_helper_in_a_declared_encoding(self):\n'
            '        declared.fill()\n'
            '    def test_long_chain(self):\n'
            '        Store("inbox.sqlite3")' + '.next' * 1100 + '.save("board", "account", [])\n'
            '    def test_long_choice(self):\n'
            '        store = ' + 'None if self else ' * 1100 + 'Store("inbox.sqlite3")\n'
            '        store.mark("board", "message", "read")\n').encode()
        declared = ('# coding: latin-1\n' + store + 'NAME = "caf\xe9.sqlite3"\n'
                    'def fill():\n    Store(NAME).initialize()\n').encode('latin-1')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'test_marked.py').write_bytes(marked)
            (Path(folder) / 'declared.py').write_bytes(declared)
            (Path(folder) / 'not_python.py').write_bytes(b'def (:\n')
            counted = counter.analyse({'': Path(folder)})
        self.assertEqual(counted, {'test_marked.py': {'Tests.test_helper_in_a_declared_encoding': ['store initialize'],
                                                      'Tests.test_long_chain': ['store save'],
                                                      'Tests.test_long_choice': ['store mark']}})

    def test_update_only_drops_stored_lines(self):
        lines = ['# The header stays.', 'test_a.py Tests.test_one', 'test_a.py Tests.test_two  # keeps a lock held',
                 'test_b.py Tests.test_one', 'test_b.py Tests.test_one', 'total 4  # was 9 in May']
        self.assertEqual(counter.read_stored(lines), ([
            'test_a.py Tests.test_one', 'test_a.py Tests.test_two', 'test_b.py Tests.test_one',
            'test_b.py Tests.test_one'], 4))
        self.assertEqual(counter.kept(lines, ['test_a.py Tests.test_two', 'test_b.py Tests.test_one']), [
            '# The header stays.', 'test_a.py Tests.test_two  # keeps a lock held', 'test_b.py Tests.test_one',
            'total 2 # was 9 in May'])

    def test_stored_list_has_one_shape(self):
        for lines in (['test_a.py Tests.test_one'], ['test_a.py Tests.test_one', 'total 1', 'total 1'],
                      ['total 1', 'test_a.py Tests.test_one'], ['test_a.py Tests.test_one', 'total 1 more'],
                      ['test_a.py Tests.test_one', 'total one'], ['test_a.py', 'total 1']):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                counter.read_stored(lines)


if __name__ == '__main__':
    unittest.main()
