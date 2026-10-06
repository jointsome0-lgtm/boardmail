"""Every error code keeps its next-step hint and its exit code."""
from pathlib import Path
import unittest

from boardmail import commands


TESTS = Path(__file__).resolve().parent


def stored():
    """The stored table: for each code, its next-step hint and its exit code."""
    rows = [line.split() for line in (TESTS / 'error_codes.txt').read_text(encoding='utf-8').splitlines()]
    return {code: (hint, int(exit_code)) for code, exit_code, hint in rows}


class ErrorCodeTests(unittest.TestCase):
    def assertEntry(self, code, entry):
        result, exit_code = commands.error_result(code)
        self.assertEqual((code, result['next_action'], exit_code), (code, *entry))

    def test_every_code_keeps_its_hint_and_exit_code(self):
        table = stored()
        self.assertEqual(len(table), 81)
        for code, entry in table.items():
            self.assertEntry(code, entry)

    def test_an_http_status_without_a_row_is_treated_like_500(self):
        table = stored()
        for status in range(100, 600):
            self.assertEntry(f'http_{status}', table.get(f'http_{status}', table['http_500']))


if __name__ == '__main__':
    unittest.main()
