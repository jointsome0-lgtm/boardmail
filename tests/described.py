"""The adapter file of the tests. In a pass over its source it gives what the settings of the source describe
under 'gives': the fields of a Batch. A test fills an inbox with it through the command collect, the way the
adapter file of an operator fills one. See arrive() in tests/kit.py."""
from boardmail.adapters import Batch

API_VERSION = 1


def collect(settings, state, known):
    gives = dict(settings['gives'])
    # What happens while the pass is under way, between its look at the inbox and what it saves.
    gives.pop('meanwhile', lambda: None)()
    return Batch(**{'state': state, **gives})
