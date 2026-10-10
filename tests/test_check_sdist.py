"""The network guard that the artifact checker runs the tests of a source distribution under."""
import contextlib
import importlib.util
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit
import warnings

from boardmail import adapter_moltbook, transport
import kit


CHECKER_PATH = Path(__file__).resolve().parents[1] / 'scripts/check_sdist.py'
spec = importlib.util.spec_from_file_location('check_sdist', CHECKER_PATH)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class CheckSdistTests(unittest.TestCase):
    def test_the_guard_forbids_a_real_connection_and_lets_an_invented_socket_through(self):
        """The tests of the source distribution run under the guard. A socket of the standard library can neither
        connect nor bind there. A test that invents the socket itself, as the kit does for a host with several
        addresses, runs as it does anywhere else."""
        url, clock = adapter_moltbook.HOST + '/api/v1/home', kit.Clock(1790000000)
        host = urlsplit(url).hostname
        with contextlib.ExitStack() as stack:
            # What the guard replaces is as it was when this test is over, also where the guard is on already.
            for name in ('connect', 'connect_ex', 'bind'):
                stack.enter_context(mock.patch.object(socket.socket, name))
            stack.enter_context(mock.patch.object(socket, 'create_connection', socket.create_connection))
            stack.enter_context(mock.patch.object(sys, '_boardmail_artifact_guard_active', False, create=True))
            exec(checker.GUARD, {})
            self.assertIs(sys._boardmail_artifact_guard_active, True)
            refused = 'forbid network connect and bind'
            for forbidden in (lambda real: real.connect(('127.0.0.1', 9)), lambda real: real.connect_ex(('127.0.0.1', 9)),
                              lambda real: real.bind(('127.0.0.1', 0))):
                with socket.socket() as real, self.assertRaisesRegex(RuntimeError, refused):
                    forbidden(real)
            # create_connection() is not replaced. It makes a socket, which cannot connect, and leaves it open.
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', ResourceWarning)
                with self.assertRaisesRegex(RuntimeError, refused):
                    socket.create_connection(('127.0.0.1', 9), 1)
            # The first address of the host takes no connection, and the second one does.
            network = kit.Network({host: lambda request: (200, {'invented': True})}, {host: [None, 0]}, clock)
            with kit.fixed(clock), network:
                self.assertEqual(transport.fetch('moltbook', url, left=5), {'invented': True})
            self.assertEqual([number for _, number, _ in network.attempts], [0, 1])


if __name__ == '__main__':
    unittest.main()
