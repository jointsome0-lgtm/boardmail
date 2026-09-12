"""Addressing regressions across persisted provider state and public originals."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from boardmail import providers
from boardmail.adapters import validate
from examples.fixtures import FixtureClient, original, settings, uid


class AddressingIntegrationTests(unittest.TestCase):
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

    def test_fruitflies_configured_alias_alone_is_an_incoming_mention(self):
        from boardmail import adapter_fruitflies as fruit
        from test_fruitflies import post
        with patch.object(fruit, '_fetch', side_effect=[[], [post(10, '@Ally please inspect')], []]):
            batch = fruit.collect({'account_id': 'alice', 'mention_aliases': ['Ally']}, {}, frozenset())
        validate(batch)
        self.assertEqual([(m['id'], m['addressing']) for m in batch.messages], [(uid(10), 'mention')])


if __name__ == '__main__':
    unittest.main()
