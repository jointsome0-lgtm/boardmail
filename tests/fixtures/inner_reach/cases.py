"""Examples that pin the counting rule of scripts/inner_reach.py. Read by the count, never run.

The docstring of each test is what the count reports for it. A line that starts
with "In Name:" is what it reports when the class of that name runs the test.
"""
import importlib
import subprocess
import sys
import threading
import time
import unittest
from unittest import TestCase as Case
from unittest import mock
from unittest.mock import patch

import boardmail.store
from boardmail import adapter_clawdchat, adapters, providers
from boardmail.adapters import Batch
from boardmail.cli import main
from boardmail.mcp import create_server
from boardmail.store import Store

import support
from below.helpers import subscribed
from support import Double, filled

CLOCK = 'boardmail.store.time.time'
SCRIPT = '''
from boardmail.store import Store
Store.wait = None
'''


def fill(store):
    store.save('board', 'account', [])


def fill_each(*stores, **named):
    for store in stores:
        store.save('board', 'account', [])
    named['last'].mark('board', 'message', 'read')


def opened():
    return Store('inbox.sqlite3')


class Gated(Store):
    def connect(self, **options):
        return super().connect(**options)


class Patches(unittest.TestCase):
    def test_dotted_path_into_the_package(self):
        """patch boardmail.providers.Client"""
        with patch('boardmail.providers.Client'):
            pass

    def test_clock_by_a_dotted_package_path(self):
        """patch boardmail.store.time.time"""
        with patch('boardmail.store.time.time', return_value=1):
            pass

    def test_clock_through_a_package_module(self):
        """patch providers.time.monotonic"""
        with patch.object(providers.time, 'monotonic', return_value=1):
            pass

    def test_clock_at_the_standard_library(self):
        """not counted"""
        with patch('time.monotonic', return_value=1), patch.object(time, 'sleep'):
            pass

    def test_path_kept_in_a_name(self):
        """patch CLOCK; patch path"""
        path = 'boardmail.providers.Client'
        with patch(CLOCK, return_value=1), patch(path):
            pass

    def test_object_dict_and_multiple(self):
        """patch boardmail.providers.HOSTS; patch providers; patch providers.HOSTS; patch providers.PAGE_SIZE"""
        with patch.object(providers, 'PAGE_SIZE', 1), patch.dict(providers.HOSTS, {}):
            pass
        with patch.multiple(providers, MAX_PAGES=1), patch.dict('boardmail.providers.HOSTS', {}):
            pass

    @mock.patch.object(adapter_clawdchat, 'Client')
    def test_decorator(self, client):
        """patch adapter_clawdchat.Client"""

    def test_started_patcher(self):
        """patch providers.MAX_PAGES"""
        patcher = mock.patch.object(providers, 'MAX_PAGES', 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_object_the_package_built(self):
        """patch client.opener.open"""
        client = providers.Client('board', {})
        with patch.object(client.opener, 'open'):
            pass

    def test_imported_by_name(self):
        """patch f'boardmail.{name}.Client'; patch module._fetch; patch other.collect"""
        module = importlib.import_module('boardmail.adapter_clawdchat')
        with patch.object(module, '_fetch'):
            pass
        for name in ('boardmail.adapter_botnet', 'boardmail.adapter_colony'):
            other = importlib.import_module(name)
            with patch.object(other, 'collect'), patch(f'boardmail.{name}.Client'):
                pass

    def test_double_and_standard_library(self):
        """not counted"""
        double = Double()
        double.answers = []
        with patch.object(double, 'get'), patch.object(subprocess, 'run'), patch('support.Double.get'):
            pass


class Replacements(unittest.TestCase):
    def test_module_attribute(self):
        """assign providers.PAGE_SIZE"""
        providers.PAGE_SIZE = 1

    def test_class_attribute(self):
        """assign Store.wait; assign boardmail.store.Store.page"""
        setattr(Store, 'wait', None)
        del boardmail.store.Store.page

    def test_object_the_package_built(self):
        """assign client.opener; assign client.requests"""
        client = adapter_clawdchat.Client({})
        client.opener = None
        client.requests += 1

    def test_item_of_a_package_name(self):
        """assign providers.HOSTS['board']"""
        providers.HOSTS['board'] = 'board.example'

    def test_method_on_a_subclass(self):
        """subclass Gated"""
        create_server(Gated('inbox.sqlite3'))

    def test_batch_is_the_adapter_interface(self):
        """not counted"""
        batch = Batch([], {}, True)
        batch.error = 'board_down'
        other = adapters.Batch([], {}, True)
        other.messages = []

    def test_item_of_a_result(self):
        """not counted"""
        page = Store('inbox.sqlite3').page()
        page['messages'] = []


class StoreWrites(unittest.TestCase):
    def test_every_write_method(self):
        """store failure; store initialize; store mark; store save; store save_collection; store set_paused; store set_subscription"""
        store = Store('inbox.sqlite3')
        store.initialize()
        store.save('board', 'account', [])
        store.save_collection('board', 'account', 'board', 0, Batch([], {}, True))
        store.failure('board', 'account', 'board_down')
        store.set_paused('board', True)
        store.set_subscription('board', 'thread', True)
        store.mark('board', 'message', 'read')

    def test_built_in_place(self):
        """store initialize"""
        boardmail.store.Store('inbox.sqlite3').initialize()

    def test_store_imported_by_name(self):
        """store initialize; store mark"""
        importlib.import_module('boardmail.store').Store('inbox.sqlite3').initialize()
        store = sys.modules['boardmail.store'].Store('inbox.sqlite3')
        store.mark('board', 'message', 'read')

    def test_settings_and_connect_that_write(self):
        """store connect; store settings"""
        store = Store('inbox.sqlite3')
        store.settings(scope='all')
        with store.connect(write=True) as db:
            db.execute('DELETE FROM messages')

    def test_connect_with_spread_options(self):
        """store connect"""
        options = {'write': True}
        with Store('inbox.sqlite3').connect(**options):
            pass

    def test_reads(self):
        """not counted"""
        store = Store('inbox.sqlite3')
        store.settings()
        store.page()
        store.status()
        with store.connect() as db, store.connect(write=False), store.connect(**{'create': False}):
            db.execute('SELECT 1')

    def test_handed_to_a_thread(self):
        """store mark"""
        store = Store('inbox.sqlite3')
        threading.Thread(target=store.mark, args=('board', 'message', 'read')).start()

    def test_each_of_several(self):
        """store mark"""
        for number, store in enumerate([Store('one.sqlite3'), Store('two.sqlite3')]):
            store.mark('board', str(number), 'read')

    def test_same_names_elsewhere(self):
        """not counted"""
        session = Double()
        session.initialize()
        session.mark('board', 'message', 'read')
        providers.failure(Batch([], {}, True), OSError())

    def test_command(self):
        """not counted"""
        main(['--db', 'inbox.sqlite3', 'mark', 'board', 'message', 'read'])


class Reached(unittest.TestCase):
    def setUp(self):
        self.store = Store('inbox.sqlite3')
        self.store.initialize()

    def fill(self):
        self.store.save('board', 'account', [])

    def test_fixture_alone(self):
        """store initialize"""

    def test_helper_method(self):
        """store initialize; store save"""
        self.fill()

    def test_helper_parameter(self):
        """store initialize; store save"""
        fill(self.store)

    def test_helper_with_starred_parameters(self):
        """store initialize; store mark; store save"""
        fill_each(Store('one.sqlite3'), last=self.store)

    def test_helper_result(self):
        """store initialize; store mark"""
        opened().mark('board', 'message', 'read')

    def test_helper_in_another_module(self):
        """store initialize; store mark; store save; store set_paused"""
        filled('inbox.sqlite3').mark('board', 'message', 'read')
        support.paused(self.store)

    def test_helper_below_a_folder(self):
        """store initialize; store set_subscription"""
        subscribed('inbox.sqlite3')

    def test_nested_function(self):
        """store initialize; store mark"""
        def read(store, message):
            store.mark('board', message, 'read')
        read(self.store, 'message')

    def test_source_in_a_string(self):
        """assign Store.wait; store initialize"""
        subprocess.run([sys.executable, '-c', SCRIPT], check=True)


class Overridden(unittest.TestCase):
    """Its tests run here and in Overriding, each time with the fixtures and helpers of the class that runs them."""

    def setUp(self):
        self.store = Store('inbox.sqlite3')
        self.store.initialize()

    def prepare(self):
        return None

    def read(self):
        self.store.mark('board', 'message', 'read')

    def test_fixture_of_the_running_class(self):
        """store initialize

        In Overriding: store initialize; store set_paused
        """

    def test_helper_of_the_running_class(self):
        """store initialize

        In Overriding: store initialize; store save; store set_paused
        """
        self.prepare()


class Overriding(Overridden):
    def setUp(self):
        super().setUp()
        self.store.set_paused('board', True)

    def prepare(self):
        self.store.save('board', 'account', [])

    def test_helper_of_a_base(self):
        """store initialize; store mark; store set_paused"""
        self.read()


class Renamed(Case):
    def test_in_a_test_case_under_another_name(self):
        """store initialize"""
        Store('inbox.sqlite3').initialize()


class Marking:
    """Not a TestCase. Its test runs in the classes that add it to one."""

    def test_from_a_mixin(self):
        """In Mixed: store mark"""
        self.store.mark('board', 'message', 'read')


class Mixed(Marking, unittest.TestCase):
    def setUp(self):
        self.store = Store('inbox.sqlite3')


class Quiet:
    def touch(self):
        return None


class Kept(Quiet):
    """Adds nothing, so touch is still the one from Quiet."""


class Writing:
    def touch(self):
        Store('inbox.sqlite3').initialize()


class Ordered(Kept, Writing, unittest.TestCase):
    def test_lookup_order(self):
        """not counted"""
        self.touch()


class Limits(unittest.TestCase):
    """Where the count is known to be wrong. The first three tests do not reach inside and are counted. The rest do
    and are missed."""

    def test_helper_named_and_not_called(self):
        """store save"""
        self.assertTrue(callable(fill))

    def test_one_name_for_two_things(self):
        """store mark"""
        session = Double()
        session.mark('board', 'message', 'read')
        session = Store('inbox.sqlite3')

    def test_read_options_spread_from_a_name(self):
        """store connect"""
        options = {'write': False}
        with Store('inbox.sqlite3').connect(**options):
            pass

    def test_attribute_of_another_object(self):
        """not counted"""
        holder = Double()
        holder.store = Store('inbox.sqlite3')
        holder.store.initialize()

    def test_name_worked_out_while_the_test_runs(self):
        """not counted"""
        getattr(Store('inbox.sqlite3'), 'initialize')()

    def test_source_without_an_import(self):
        """not counted"""
        exec("Store('inbox.sqlite3').initialize()")

    def test_file_run_by_its_path(self):
        """not counted"""
        subprocess.run([sys.executable, 'startup.py'], check=True)

    def test_container_changed_in_place_or_through_another_name(self):
        """not counted"""
        providers.HOSTS.update({'board': 'board.example'})
        hosts = providers.HOSTS
        hosts['board'] = 'board.example'
