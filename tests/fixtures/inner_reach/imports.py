"""Examples of what a file runs when it is imported, itself or through another file. Read by the count, never run.

The docstring of each test is what the count reports for it, as in cases.py. The first four reasons of every
test here come from the import: three from startup.py and one from the class-level statement in Decorated.
"""
import unittest
from unittest.mock import patch

from boardmail import providers

import startup


class Imported(unittest.TestCase):
    def test_file_imported_at_the_top(self):
        """assign providers.HOSTS['board']; assign providers.MAX_PAGES; assign providers.PAGE_SIZE; assign providers.TIMEOUT"""
        self.assertTrue(startup)


@patch.object(providers, 'RETRIES', 1)
class Decorated(unittest.TestCase):
    """Its decorator counts for the tests the class has, whichever class runs them."""

    providers.TIMEOUT = 1

    def test_below_a_class_decorator(self):
        """assign providers.HOSTS['board']; assign providers.MAX_PAGES; assign providers.PAGE_SIZE; assign providers.TIMEOUT; patch providers.RETRIES

        In Extended: assign providers.HOSTS['board']; assign providers.MAX_PAGES; assign providers.PAGE_SIZE; assign providers.TIMEOUT; patch providers.RETRIES
        """


class Extended(Decorated):
    def test_added_below_the_decorated_class(self):
        """assign providers.HOSTS['board']; assign providers.MAX_PAGES; assign providers.PAGE_SIZE; assign providers.TIMEOUT"""
