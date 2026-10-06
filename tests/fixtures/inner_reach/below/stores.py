"""What below/helpers.py hands on by a relative import. Read by the count, never run."""
from boardmail.store import Store


def subscribed(path):
    Store(path).set_subscription('board', 'thread', True)
