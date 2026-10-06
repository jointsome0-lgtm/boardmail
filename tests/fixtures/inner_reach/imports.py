"""Examples of what a test file runs by importing another. Read by the count, never run.

The docstring of each test is what the count reports for it, as in cases.py.
"""
import unittest

import startup


class Imported(unittest.TestCase):
    def test_file_imported_at_the_top(self):
        """assign providers.MAX_PAGES"""
        self.assertTrue(startup)
