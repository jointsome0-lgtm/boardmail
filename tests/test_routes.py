"""A call that a result names has one shape, and the call that an error names is one that is taken.

A route is a tool and its arguments. The stored stories are searched for every route that a result has. Then one
session makes each error that names a call as its next step, through the CLI and through MCP, and makes that call
as the error wrote it.
"""
from contextlib import closing
import errno
import json
from pathlib import Path
import shlex
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from boardmail import cli, errors, mcp, replies, table
from examples.fixtures import uid
import kit


STORIES = ('story_inbox.txt', 'story_older.txt', 'story_reply.txt')
# The fields of a result that hold a route.
FIELDS = {'expand', 'next', 'read', 'replay', 'show'}
# What a route was written with, and what an error named its next step in.
GONE = {'command', 'recovery', 'identifier_hint'}
START = 1_800_000_000
ME, WRITER = uid(2), uid(10)
OURS = uid(201)                     # a post of ours
FIRST, SECOND = uid(211), uid(212)  # two replies to it
AFTER = 'After '                    # how the step is called that makes the call of an error


def stored(name):
    """The results of a stored story."""
    told = (kit.TESTS / name).read_text(encoding='utf-8').split('\n== ')
    return [json.loads(step[step.index('\n{\n'):]) for step in told]


def routes(value, field=None):
    """Every route under a result, with the field that holds it. A route is known by the field that it is in, and
    by a field that only a route has."""
    if isinstance(value, dict):
        if field in FIELDS or value.keys() & {'tool', 'command', 'arguments'}:
            yield field, value
        else:
            for key, inner in value.items():
                yield from routes(inner, key)
    elif isinstance(value, list):
        for inner in value:
            yield from routes(inner, field)


def fields(value):
    """Every name of a field under a result."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from fields(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from fields(inner)


def call(command, **arguments):
    return {'tool': 'boardmail_' + command, 'arguments': arguments}


def story(step, board, home, test):
    def follow(code, route, result):
        """An error with this code names route as its next step. Make that call as it is written."""
        test.assertEqual((result.get('error'), result.get('next')), (code, route))
        step(AFTER + code, shlex.join(kit.words(route)), route['tool'].removeprefix('boardmail_'), **route['arguments'])

    def refused(code, route, title, typed, tool, /, **arguments):
        follow(code, route, step(title, typed, tool, **arguments))

    def file(name, text):
        # The command line reads a reply from a file. MCP takes the text itself.
        (home / name).write_bytes(text.encode())
        return text

    def reply(action, message, text=None, **given):
        """The words of a reply command and the arguments of its tool."""
        options = ''.join(f' --{name} {value}' for name, value in given.items())
        if text is not None:
            option, name = ('--body-file', 'body') if action == 'prepare' else ('--readback-file', 'readback_body')
            options, given = f'{options} {option} {text}', {**given, name: (home / text).read_text()}
        return (f'reply {action} moltbook {message}{options}', 'reply_' + action), {
            'source': 'moltbook', 'id': message, **given}

    def journal(message):
        return call('reply_show', source='moltbook', id=message)

    board.post(OURS, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
    for minute, question in enumerate((FIRST, SECOND), 1):
        board.comment(question, OURS, WRITER, 'sample-writer', f'Invented question {minute}.',
                      f'2026-01-01T12:0{minute}:00Z', notify='post_comment')

    # The inbox and its sources.
    refused('database_missing', call('init'), 'Read before there is an inbox', 'list', 'list')
    refused('database_exists', call('status'), 'Create the inbox a second time', 'init', 'init')
    step('Collect two questions under our post', 'collect', 'collect')
    refused('source_not_found', call('status'), 'Pause a source that is not configured', 'pause elsewhere', 'pause',
            source='elsewhere')
    refused('message_not_found', call('list', source='moltbook', scope='all', context='none'),
            'Read a message that was never collected', f'show moltbook {uid(999)}', 'show',
            source='moltbook', id=uid(999))
    step('Save a reading preference', 'settings --scope all', 'settings', scope='all')
    with closing(sqlite3.connect(home / 'inbox.sqlite3')) as db, db:
        db.execute("UPDATE reader_settings SET value='everything'")  # no program of this package writes that
    refused('invalid_settings', call('settings', reset=True), 'Read with a saved preference that is none', 'list',
            'list')

    # The journal of the first reply, from nothing to its receipt.
    file('draft.txt', 'An invented answer.')
    file('other.txt', 'Another invented answer.')
    where, elsewhere = board.url(OURS, uid(221)), board.url(OURS, uid(222))
    typed, given = reply('begin', FIRST, key='a-key-of-no-draft')
    refused('reply_not_prepared', journal(FIRST), 'Begin a reply that was never saved', *typed, **given)
    typed, given = reply('prepare', FIRST, 'draft.txt')
    key = step('Save the first answer', *typed, **given)['reply']['idempotency_key']
    typed, given = reply('prepare', FIRST, 'other.txt')
    refused('reply_body_conflict', journal(FIRST), 'Save other text over the draft', *typed, **given)
    typed, given = reply('begin', FIRST, key='a-key-of-no-draft')
    refused('reply_key_mismatch', journal(FIRST), 'Begin with a key that is not its own', *typed, **given)
    typed, given = reply('confirm', FIRST, 'draft.txt', key=key, ref=where)
    refused('reply_not_started', journal(FIRST), 'Confirm before the begin', *typed, **given)
    typed, given = reply('begin', FIRST, key=key)
    step('Begin, just before sending', *typed, **given)
    typed, given = reply('prepare', FIRST, 'other.txt')
    refused('reply_already_started', journal(FIRST), 'Save other text after the begin', *typed, **given)
    typed, given = reply('confirm', FIRST, 'other.txt', key=key, ref=where)
    refused('reply_readback_mismatch', journal(FIRST), 'Confirm with text that was not saved', *typed, **given)
    typed, given = reply('confirm', FIRST, 'draft.txt', key=key, ref=where)
    step('Confirm with the text that was saved', *typed, **given)
    typed, given = reply('confirm', FIRST, 'draft.txt', key=key, ref=elsewhere)
    refused('reply_reference_conflict', journal(FIRST), 'Confirm it again at another address', *typed, **given)
    typed, given = reply('prepare', FIRST, 'other.txt')
    refused('reply_already_recorded', journal(FIRST), 'Save other text after the receipt', *typed, **given)

    # The second reply is begun, and nobody knows where it went.
    typed, given = reply('prepare', SECOND, 'other.txt')
    key = step('Save the second answer', *typed, **given)['reply']['idempotency_key']
    typed, given = reply('begin', SECOND, key=key)
    step('Begin the second one', *typed, **given)
    for number in range(1, replies.MAX_CANDIDATES + 1):
        typed, given = reply('verify', SECOND, key=key, ref=board.url(OURS, uid(300 + number)))
        step(f'Address {number} where it may be has no reply', *typed, **given)
    typed, given = reply('verify', SECOND, key=key, ref=board.url(OURS, uid(399)))
    refused('reply_candidate_limit', journal(SECOND), 'One address more than the journal keeps', *typed, **given)
    step('Leave the board alone for now', 'pause moltbook', 'pause', source='moltbook')
    typed, given = reply('verify', SECOND, key=key, ref=board.url(OURS, uid(301)))
    refused('source_paused', call('status'), 'Ask a board that is paused', *typed, **given)
    with patch('sqlite3.connect', side_effect=PermissionError(errno.EACCES, 'an invented refusal')):
        failed = step('A reply command cannot open the inbox file', f'reply show moltbook {SECOND}', 'reply_show',
                      source='moltbook', id=SECOND)
    test.assertIs(failed['send_allowed'], False)
    follow('local_state_error', journal(SECOND), failed)


class RouteTests(unittest.TestCase):
    def found(self):
        """Every route of the stored stories, with the story and the field that it is in."""
        for name in STORIES:
            inside = [(name, field, route) for result in stored(name) for field, route in routes(result)]
            self.assertTrue(inside, f'{name} has no route')
            yield from inside

    def test_every_route_of_the_stored_stories_is_a_tool_and_its_arguments(self):
        typed = cli.parser()
        for name, field, route in self.found():
            with self.subTest(story=name, field=field, route=route):
                self.assertEqual(sorted(route), ['arguments', 'tool'])
                self.assertIn(field, FIELDS)
                command = table.COMMANDS[route['tool'].removeprefix('boardmail_')]
                self.assertEqual(route['tool'], 'boardmail_' + command.name)
                # The command takes the call as it is written, and so does the command line.
                table.checked(command, route['arguments'])
                typed.parse_args(kit.words(route))
        for name in STORIES:
            self.assertEqual(GONE & {field for result in stored(name) for field in fields(result)}, set(), name)

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_a_tool_takes_every_route_of_the_stored_stories(self):
        from jsonschema import Draft202012Validator
        for name, field, route in self.found():
            with self.subTest(story=name, field=field, route=route):
                command = table.COMMANDS[route['tool'].removeprefix('boardmail_')]
                self.assertTrue(Draft202012Validator(mcp.input_schema(command)).is_valid(route['arguments']))


class NextCallTests(unittest.TestCase):
    def told(self, entry):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home / 'moltbook.key').write_text('an-invented-key\n')
        (home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key'}}}))
        board, clock = kit.Moltbook(ME, 'sample-agent'), kit.Clock(START)
        with kit.Network({board.HOST: board}), kit.fixed(clock):
            return kit.told(lambda step: story(step, board, home, self), home, entry)

    def check(self, steps):
        """Every code that names a call was made, and the call that it named was taken."""
        made = {step.title.removeprefix(AFTER): step for step in steps if step.title.startswith(AFTER)}
        self.assertEqual(sorted(made), sorted(code for code, entry in errors.CODES.items() if entry.next))
        for code, step in made.items():
            with self.subTest(code=code, call=step.command):
                self.assertFalse(step.outcome)
                self.assertNotIn('error', json.loads(step.text))

    def test_the_call_that_an_error_names_is_taken_on_the_command_line(self):
        self.check(self.told('cli'))

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_the_call_that_an_error_names_is_taken_through_mcp(self):
        called = self.told('mcp')
        self.check(called)
        kit.check_both(self, self.told('cli'), called, {})


if __name__ == '__main__':
    unittest.main()
