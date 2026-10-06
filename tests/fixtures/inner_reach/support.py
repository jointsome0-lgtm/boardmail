"""Helpers in a second module for the examples in cases.py. Read by the count, never run."""
from boardmail.store import Store


class Double:
    def get(self, url):
        return {}

    def initialize(self):
        return None

    def mark(self, *arguments):
        return None


def filled(path):
    store = Store(path)
    store.save('board', 'account', [])
    return store


def paused(store):
    store.set_paused('board', True)
