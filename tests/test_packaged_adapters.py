"""Shipped adapters use the same local delivery boundary as supplied files."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from boardmail.adapters import Batch, collect_all
from boardmail.config import PACKAGED_ADAPTERS, load
from boardmail.store import Store


class PackagedAdapterTests(unittest.TestCase):
    def test_named_modules_deliver_once_and_local_reads_do_not_load_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sources = {name: {'account_id': 'demo_agent'} for name in PACKAGED_ADAPTERS}
            path = root / 'config.json'
            path.write_text(json.dumps({'database': 'mail.sqlite3', 'sources': sources}))
            config = load(path)
            store = Store(config['database'])
            store.initialize(config['sources'])
            for name, module_name in PACKAGED_ADAPTERS.items():
                with self.subTest(adapter=name):
                    self.assertEqual(config['sources'][name]['adapter'], name)
                    module = importlib.import_module(module_name)
                    item = {'id': 'opaque-id', 'thread_id': 'root', 'kind': 'mention',
                            'title': '', 'body': 'Public example', 'url': 'https://example.invalid/item',
                            'created_at': 1}
                    with patch.object(module, 'collect', return_value=Batch(messages=[item], state={'position': 1})):
                        result = collect_all(store, {name: config['sources'][name]})
                        self.assertFalse(result['failed'])
                        self.assertEqual(result['added'], 1)
                        store.mark(name, 'opaque-id', 'read')
                        self.assertEqual(collect_all(store, {name: config['sources'][name]})['added'], 0)
                    with patch('boardmail.adapters.importlib.import_module', side_effect=AssertionError('local read imported adapter')):
                        self.assertIsNotNone(store.show(name, 'opaque-id')['read_at'])
                        self.assertEqual(store.collection_state(name, 'demo_agent', name)[1], {'position': 1})
                        self.assertTrue(store.wait(0, 0)['messages'])
