"""Addressing regressions across persisted provider state and public originals."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boardmail import providers
from boardmail.adapters import validate
from boardmail.store import Store
from examples.fixtures import FixtureClient, original, settings, uid


class AddressingIntegrationTests(unittest.TestCase):
    def test_unseen_parent_keeps_body_visible_before_later_notification(self):
        for source, reply_type in (('the-colony', 'reply_to_comment'), ('moltbook', 'comment_reply')):
            for later_type in (reply_type, 'mention'):
                with self.subTest(source=source, later_type=later_type), tempfile.TemporaryDirectory() as directory:
                    config = settings()[source]
                    client = FixtureClient(source, config)
                    client.comments = [client.comments[0]]
                    child = client.comments[0]
                    child['parent_id'] = uid(90)
                    client.events = [client.events[0]]
                    store = Store(Path(directory) / 'inbox.sqlite3')
                    store.initialize()

                    def collect():
                        known, state, revision = store.collection_state(source, client.owner, source)
                        batch = providers.collect(source, config, state, known, client_factory=lambda *_: client)
                        validate(batch)
                        return store.save_collection(source, client.owner, source, revision, batch)

                    self.assertEqual(collect(), (1, False))
                    page = store.page(scope='addressed')
                    self.assertEqual([m['id'] for m in page['messages']], [child['id']])
                    self.assertIsNone(page['messages'][0]['addressing'])
                    self.assertEqual(page['thread_activity'], [])
                    checkpoint = page['next_after']
                    store.mark(source, child['id'], 'read')
                    stored = store.show(source, child['id'])
                    later = deepcopy(client.events[0])
                    later['id'] = uid(9999)
                    later['notification_type' if source == 'the-colony' else 'type'] = later_type
                    client.events.insert(0, later)
                    self.assertEqual(collect(), (0, False))
                    self.assertEqual(store.show(source, child['id']), stored)
                    self.assertEqual(store.page(checkpoint, scope='addressed')['scanned'], 0)

    def test_moltbook_parent_without_identity_cannot_hide_nested_reply(self):
        for author in (None, {}):
            with self.subTest(author=author):
                source = 'moltbook'
                config = settings()[source]
                client = FixtureClient(source, config)
                child = client.comments[0]
                child['parent_id'] = uid(90)
                parent = original(90, 201)
                parent['author'] = author
                client.comments = [parent, child]
                client.events = client.events[:1]
                batch = providers.collect(source, config, {}, set(), client_factory=lambda *_: client)
                validate(batch)
                self.assertIsNone(batch.messages[0]['addressing'])

    def test_legacy_pending_reply_and_new_mention_keep_both_grounds(self):
        for source, root, mid in (('the-colony', 101, 111), ('moltbook', 201, 211)):
            with self.subTest(source=source):
                config = settings()[source]
                client = FixtureClient(source, config)
                client.comments[0]['parent_id'] = uid(80)
                event = deepcopy(client.events[0])
                event['notification_type' if source == 'the-colony' else 'type'] = 'mention'
                client.events = [event]
                key = uid(mid if source == 'the-colony' else root)
                state = {'pending': {key: {'post': uid(root), 'ids': {uid(mid): 'reply_to_comment'}, 'cursor': None}}}
                batch = providers.collect(source, config, state, set(), client_factory=lambda *_: client)
                validate(batch)
                self.assertEqual(batch.messages[0]['addressing'], 'direct+mention')

    def test_missing_parent_field_cannot_prove_a_top_level_direct_reply(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                config = settings()[source]
                client = FixtureClient(source, config)
                del client.comments[0]['parent_id']
                client.events = client.events[:1]
                batch = providers.collect(source, config, {}, set(), client_factory=lambda *_: client)
                validate(batch)
                self.assertNotIn(batch.messages[0]['addressing'], ('direct', 'direct+mention'))

    def test_comment_from_another_thread_cannot_poison_cached_parent_or_addressing(self):
        source = 'moltbook'
        config = settings()[source]
        client = FixtureClient(source, config)
        parent = original(90, 999, author=2, body='Wrong thread parent')
        child = client.comments[0]
        child['parent_id'] = uid(90)
        client.comments = [parent, child]
        client.events = client.events[:1]
        batch = providers.collect(source, config, {}, set(), client_factory=lambda *_: client)
        self.assertNotIn(uid(90), [o['id'] for o in batch.originals])
        self.assertNotIn(batch.messages[0]['addressing'], ('direct', 'direct+mention'))

    def test_clawdchat_missing_parent_field_is_not_direct(self):
        from boardmail import adapter_clawdchat as clawd
        from test_clawdchat import FixtureClient as ClawdClient, event, original as clawd_original
        client = ClawdClient()
        client.events = [event(10)]
        child = clawd_original(10)
        del child['parent_id']
        client.originals[uid(10)] = child
        with patch.object(clawd, 'Client', return_value=client):
            batch = clawd.collect({'account_id': uid(1)}, {}, frozenset())
        validate(batch)
        self.assertIsNone(batch.messages[0]['addressing'])
        # The original still establishes its post, even when the notification
        # omitted it; two absent IDs must not compare equal as a direct target.
        client.events[0].pop('post_id')
        with patch.object(clawd, 'Client', return_value=client):
            batch = clawd.collect({'account_id': uid(1)}, {}, frozenset())
        self.assertIsNone(batch.messages[0]['addressing'])
        child['parent_id'] = uid(100)
        with patch.object(clawd, 'Client', return_value=client):
            batch = clawd.collect({'account_id': uid(1)}, {}, frozenset())
        self.assertEqual(batch.messages[0]['addressing'], 'direct')


if __name__ == '__main__':
    unittest.main()
