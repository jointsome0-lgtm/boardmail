"""The stored tests that reach inside the package follow the tests."""
import importlib.util
from pathlib import Path
import unittest


TESTS = Path(__file__).resolve().parent
COUNTER_PATH = TESTS.parent / 'scripts/inner_reach.py'
spec = importlib.util.spec_from_file_location('inner_reach', COUNTER_PATH)
counter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(counter)


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


if __name__ == '__main__':
    unittest.main()
