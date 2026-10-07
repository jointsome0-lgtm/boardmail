"""Addressing regressions across persisted provider state and public originals."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from boardmail import providers
from boardmail.adapters import validate
from boardmail.store import Store
from examples.fixtures import FixtureBoard, original, settings, uid


class AddressingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())

    def test_unseen_parent_keeps_body_visible_before_later_notification(self):
        for source, reply_type in (('the-colony', 'reply_to_comment'), ('moltbook', 'comment_reply')):
            for later_type in (reply_type, 'mention'):
                with self.subTest(source=source, later_type=later_type), tempfile.TemporaryDirectory() as directory:
                    config = settings(self.folder)[source]
                    board = FixtureBoard(source, config)
                    board.comments = [board.comments[0]]
                    child = board.comments[0]
                    child['parent_id'] = uid(90)
                    board.events = [board.events[0]]
                    store = Store(Path(directory) / 'inbox.sqlite3')
                    store.initialize()

                    def collect():
                        known, state, revision = store.collection_state(source, board.owner, source)
                        batch = providers.collect(source, config, state, known, fetch=board)
                        validate(batch)
                        return store.save_collection(source, board.owner, source, revision, batch)

                    self.assertEqual(collect(), (1, False))
                    page = store.page(scope='addressed')
                    self.assertEqual([m['id'] for m in page['messages']], [child['id']])
                    self.assertIsNone(page['messages'][0]['addressing'])
                    self.assertEqual(page['thread_activity'], [])
                    checkpoint = page['next_after']
                    store.mark(source, child['id'], 'read')
                    stored = store.show(source, child['id'])
                    later = deepcopy(board.events[0])
                    later['id'] = uid(9999)
                    later['notification_type' if source == 'the-colony' else 'type'] = later_type
                    board.events.insert(0, later)
                    self.assertEqual(collect(), (0, False))
                    self.assertEqual(store.show(source, child['id']), stored)
                    self.assertEqual(store.page(checkpoint, scope='addressed')['scanned'], 0)

    def test_moltbook_parent_without_identity_cannot_hide_nested_reply(self):
        for author in (None, {}):
            with self.subTest(author=author):
                source = 'moltbook'
                config = settings(self.folder)[source]
                board = FixtureBoard(source, config)
                child = board.comments[0]
                child['parent_id'] = uid(90)
                parent = original(90, 201)
                parent['author'] = author
                board.comments = [parent, child]
                board.events = board.events[:1]
                batch = providers.collect(source, config, {}, set(), fetch=board)
                validate(batch)
                self.assertIsNone(batch.messages[0]['addressing'])

    def test_legacy_pending_reply_and_new_mention_keep_both_grounds(self):
        for source, root, mid in (('the-colony', 101, 111), ('moltbook', 201, 211)):
            with self.subTest(source=source):
                config = settings(self.folder)[source]
                board = FixtureBoard(source, config)
                board.comments[0]['parent_id'] = uid(80)
                event = deepcopy(board.events[0])
                event['notification_type' if source == 'the-colony' else 'type'] = 'mention'
                board.events = [event]
                key = uid(mid if source == 'the-colony' else root)
                state = {'pending': {key: {'post': uid(root), 'ids': {uid(mid): 'reply_to_comment'}, 'cursor': None}}}
                batch = providers.collect(source, config, state, set(), fetch=board)
                validate(batch)
                self.assertEqual(batch.messages[0]['addressing'], 'direct+mention')

    def test_missing_parent_field_cannot_prove_a_top_level_direct_reply(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                config = settings(self.folder)[source]
                board = FixtureBoard(source, config)
                del board.comments[0]['parent_id']
                board.events = board.events[:1]
                batch = providers.collect(source, config, {}, set(), fetch=board)
                validate(batch)
                self.assertNotIn(batch.messages[0]['addressing'], ('direct', 'direct+mention'))

    def test_comment_from_another_thread_cannot_poison_cached_parent_or_addressing(self):
        source = 'moltbook'
        config = settings(self.folder)[source]
        board = FixtureBoard(source, config)
        parent = original(90, 999, author=2, body='Wrong thread parent')
        child = board.comments[0]
        child['parent_id'] = uid(90)
        board.comments = [parent, child]
        board.events = board.events[:1]
        batch = providers.collect(source, config, {}, set(), fetch=board)
        self.assertNotIn(uid(90), [o['id'] for o in batch.originals])
        self.assertNotIn(batch.messages[0]['addressing'], ('direct', 'direct+mention'))

    def test_clawdchat_missing_parent_field_is_not_direct(self):
        from boardmail import adapter_clawdchat as clawd
        from test_clawdchat import Board, event, key_file, original as clawd_original
        board, config = Board(), {'account_id': uid(1), 'api_key_file': key_file(self)}
        board.events = [event(10)]
        child = clawd_original(10)
        del child['parent_id']
        board.originals[uid(10)] = child
        batch = clawd.collect(config, {}, frozenset(), fetch=board)
        validate(batch)
        self.assertIsNone(batch.messages[0]['addressing'])
        # The original still establishes its post, even when the notification
        # omitted it; two absent IDs must not compare equal as a direct target.
        board.events[0].pop('post_id')
        batch = clawd.collect(config, {}, frozenset(), fetch=board)
        self.assertIsNone(batch.messages[0]['addressing'])
        child['parent_id'] = uid(100)
        batch = clawd.collect(config, {}, frozenset(), fetch=board)
        self.assertEqual(batch.messages[0]['addressing'], 'direct')


if __name__ == '__main__':
    unittest.main()
