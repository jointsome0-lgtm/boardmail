"""One offline session on an inbox file that an older release left, told through the CLI and through MCP. Every
result is the stored one.

The file is the bundled version-1 file. At first it can only be read: the file and its folder are read-only, and
no command changes a byte of it or leaves a file next to it. Then the agent writes to it without collecting, and
each thing that it writes is there for the next command. At the end the first collection brings the file up to
date.

A command that only reads sees every part of the file, whatever the file has: its connection gets an empty
stand-in for each part that is missing. The last test checks where those stand-ins are.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from examples.fixtures import uid
import kit


START = 1_800_000_000
ME, WRITER = uid(2), uid(3)
THREAD = uid(100)              # a post of ours. The older file has two replies to it:
DONE, ASKED = uid(10), uid(11)  # one that it marks as answered, and one that it does not
ANSWER = uid(221)              # our answer to the second
NEW = uid(12)                  # a third reply, which arrives when the story collects
ELSEWHERE = uid(200)           # a thread on another board
TEXT = 'An invented answer.'
# What a version-1 file does not have: two columns of messages, one of sources, and nine tables.
LATER = ('adapter_state', 'messages', 'originals', 'reader_settings', 'reply_attempts', 'reply_candidate_checks',
         'reply_candidates', 'reply_verifications', 'sources', 'subscriptions', 'thread_tags')


def story(step, board, clock, home, test):
    inbox = home / 'inbox.sqlite3'
    board.post(THREAD, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
    for minute, question in enumerate((DONE, ASKED), 1):
        board.comment(question, THREAD, WRITER, 'sample-writer', f'Invented question {minute}.',
                      f'2026-01-01T12:0{minute}:00Z', notify='post_comment')

    # Nothing can be written: not to the file, and not next to it.
    before, beside = inbox.read_bytes(), sorted(path.name for path in home.iterdir())
    os.chmod(inbox, 0o400)
    os.chmod(home, 0o500)
    try:
        step('What the older file says about itself', 'status', 'status')
        step('Its mail', 'list', 'list')
        step('Only what is unread', 'list --unread', 'list', unread=True)
        step('Mail that is there does not wait', 'wait --timeout 0', 'wait', timeout=0)
        step('No mail after the last one', 'wait --after 2 --timeout 0', 'wait', after=2, timeout=0)
        step('One message, whole', f'show moltbook {ASKED}', 'show', source='moltbook', id=ASKED)
        step('What it answers, from the inbox alone', f'context --local moltbook {ASKED}', 'context',
             source='moltbook', id=ASKED, local=True)
        step('Its thread, from the inbox alone', f'expand --local --through 2 moltbook {THREAD}', 'expand',
             source='moltbook', thread=THREAD, through=2, local=True)
        step('No tags', 'tags', 'tags')
        step('A tag that nothing has', 'tag show follow-up', 'tag_show', tag='follow-up')
        step('Read by that tag', 'list --tag follow-up', 'list', tag='follow-up')
        step('What has no tag', 'list --untagged', 'list', untagged=True)
        step('Nothing is followed', 'subscriptions', 'subscriptions')
        step('The reading preferences', 'settings', 'settings')
        step('No reply is saved', 'reply list', 'reply_list')
        step('Nor for this message', f'reply show moltbook {ASKED}', 'reply_show', source='moltbook', id=ASKED)
    finally:
        os.chmod(home, 0o700)
        os.chmod(inbox, 0o600)
    test.assertEqual(inbox.read_bytes(), before, 'A read changed the older file')
    test.assertEqual(sorted(path.name for path in home.iterdir()), beside, 'A read left a file next to it')

    # Writes that do not collect. The file stays an older one, and what is written is there for the next command.
    (home / 'answer.txt').write_text(TEXT)
    saved = step('Save an answer', f'reply prepare moltbook {ASKED} --body-file answer.txt', 'reply_prepare',
                 source='moltbook', id=ASKED, body=TEXT)
    key = saved['reply']['idempotency_key']
    clock.advance(60)
    step('Begin, just before sending', f'reply begin moltbook {ASKED} --key {key}', 'reply_begin',
         source='moltbook', id=ASKED, key=key)
    kept = board.comment(ANSWER, THREAD, ME, 'sample-agent', TEXT, '2026-01-01T13:00:00Z', parent=ASKED)
    kept['verification_status'] = 'pending'
    where = board.url(THREAD, ANSWER)
    clock.advance(60)
    step('The board has not verified the reply yet', f'reply verify moltbook {ASKED} --key {key} --ref {where}',
         'reply_verify', source='moltbook', id=ASKED, key=key, ref=where)
    step('The journal keeps where it may be', f'reply show moltbook {ASKED}', 'reply_show',
         source='moltbook', id=ASKED)
    clock.advance(600)
    kept['verification_status'] = 'verified'
    step('The board has verified it now', f'reply verify moltbook {ASKED} --key {key} --ref {where}',
         'reply_verify', source='moltbook', id=ASKED, key=key, ref=where)
    step('The message is answered', f'show moltbook {ASKED}', 'show', source='moltbook', id=ASKED)
    step('Nothing is left unfinished', 'reply list', 'reply_list')

    step('Take off a tag that nothing has', f'tag remove follow-up moltbook {THREAD}', 'tag_remove',
         tag='follow-up', source='moltbook', thread=THREAD)
    step('Tag the thread', f'tag add follow-up moltbook {THREAD}', 'tag_add',
         tag='follow-up', source='moltbook', thread=THREAD)
    step('The tags in use', 'tags', 'tags')
    step('Read by tag', 'list --tag follow-up', 'list', tag='follow-up')
    step('Read everything, without context', 'settings --scope all --context none', 'settings',
         scope='all', context='none')
    step('What the new preferences show', 'list', 'list')
    # The config calls this source research and says that it is read with the PostingBoard adapter.
    step('Follow a thread on another board', f'subscribe research {ELSEWHERE}', 'subscribe',
         source='research', thread=ELSEWHERE)
    step('What is followed', 'subscriptions', 'subscriptions')
    step('Leave the first board alone', 'pause moltbook', 'pause', source='moltbook')
    step('The older file with all of this', 'status', 'status')
    step('Read the first board again', 'resume moltbook', 'resume', source='moltbook')

    # No board is invented for the other source, so this story does not read it.
    step('Leave the other board alone', 'pause research', 'pause', source='research')
    board.comment(NEW, THREAD, WRITER, 'sample-writer', 'Invented question 3.', '2026-01-01T14:00:00Z',
                  notify='post_comment')
    clock.advance(60)
    step('The first collection brings the file up to date', 'collect', 'collect')
    step('The file after it', 'status', 'status')
    step('The old mail is as it was, and the new mail follows it', 'list', 'list')


class OlderFileStoryTests(unittest.TestCase):
    def told(self, entry):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        with closing(sqlite3.connect(home / 'inbox.sqlite3')) as db:
            db.executescript((kit.TESTS / 'fixtures/v1.sql').read_text())
        for name in ('moltbook.key', 'research.key'):
            (home / name).write_text('an-invented-key\n')
        (home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key'},
            'research': {'adapter': 'postingboard', 'account_id': uid(1), 'api_key_file': 'research.key'}}}))
        board, clock = kit.Moltbook(ME, 'sample-agent'), kit.Clock(START)
        with kit.Network({board.HOST: board}), kit.fixed(clock):
            return kit.told(lambda step: story(step, board, clock, home, self), home, entry)

    def test_through_the_command_line(self):
        kit.check_stored(self, 'story_older.txt', kit.transcript(self.told('cli')))

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_through_mcp(self):
        # MCP takes the text of a reply where the command line takes a file. No step answers differently.
        kit.check_both(self, self.told('cli'), self.told('mcp'), {})

    def test_only_a_connection_that_reads_gets_stand_ins(self):
        with kit.connections() as closed:
            self.told('cli')
        # The first command reads the older file: a stand-in for each part that it lacks, all of them in memory.
        self.assertEqual(closed[0], kit.Closed(False, LATER, True))
        # The last one reads the file after everything was written and collected. Nothing is missing.
        self.assertEqual(closed[-1], kit.Closed(False, (), True))
        self.assertTrue(any(one.writes for one in closed))
        for one in closed:
            # A stand-in on a connection that writes would hide the table that the command is about to make.
            self.assertFalse(one.writes and one.stand_ins, one)
            self.assertTrue(one.memory or not one.stand_ins, one)


if __name__ == '__main__':
    unittest.main()
