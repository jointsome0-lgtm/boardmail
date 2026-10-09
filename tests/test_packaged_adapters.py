"""A board of the package other than Postingboard, Colony and Moltbook goes through the same local delivery as a
supplied file.

Every board here is invented, and each has one message for the account."""
import json
from pathlib import Path
import tempfile
import unittest

from boardmail.boards import BOARDS, collect_all
from boardmail.config import load
from kit import mark, new_inbox
import test_botnet
import test_clawdchat

KEY = 'example.key'


def botnet():
    board = test_botnet.Board()
    board.add(10)
    return board


def clawdchat():
    board = test_clawdchat.Board()
    board.events = [test_clawdchat.event(10)]
    board.originals[test_clawdchat.uid(10)] = test_clawdchat.original(10)
    return board


# For each board: the settings of its source in a config, and what gives the invented board for one pass.
INVENTED = {'botnet': ({'account_id': test_botnet.OWNER, 'api_key_file': KEY}, botnet),
            'clawdchat': ({'account_id': test_clawdchat.uid(1), 'api_key_file': KEY}, clawdchat)}


class PackagedAdapterTests(unittest.TestCase):
    def test_named_boards_deliver_once_and_local_reads_keep_what_they_left(self):
        self.assertEqual(set(INVENTED), set(BOARDS) - {'postingboard', 'the-colony', 'moltbook'})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / KEY).write_text(test_botnet.KEY + '\n')
            path = root / 'config.json'
            path.write_text(json.dumps({'database': 'mail.sqlite3',
                                        'sources': {name: about for name, (about, _) in INVENTED.items()}}))
            config = load(path)
            store = new_inbox(config['database'], config['sources'])
            for name, (_, board) in INVENTED.items():
                with self.subTest(board=name):
                    source = {name: config['sources'][name]}
                    self.assertEqual(source[name]['adapter'], name)
                    result = collect_all(store, source, fetch=board())
                    self.assertEqual((result['added'], result['failed']), (1, False), result)
                    message, = (m for m in store.page(0)['messages'] if m['source'] == name)
                    mark(store, name, message['id'], 'read')
                    self.assertEqual(collect_all(store, source, fetch=board())['added'], 0)
                    self.assertIsNotNone(store.show(name, message['id'])['read_at'])
                    self.assertTrue(store.collection_state(name, source[name]['account_id'], name)[1])
                    self.assertTrue(store.wait(0, 0)['messages'])
