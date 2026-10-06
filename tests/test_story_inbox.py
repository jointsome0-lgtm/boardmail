"""One offline session with an inbox, told through the CLI and through MCP. Every result is the stored one."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from examples.fixtures import uid
import kit


EXAMPLES = Path(__file__).resolve().parent.parent / 'examples'
START = 1_800_000_000
ME, WRITER, OTHER = uid(2), uid(10), uid(11)
OURS, THEIRS = uid(201), uid(301)                    # a post of ours, and one we only follow
FIRST, ANSWER, SECOND = uid(211), uid(221), uid(212)  # a reply to our post, our answer to it, the reply to that
ASIDE, NAMED = uid(311), uid(312)                    # two comments under the post we follow


def story(step, board, clock):
    board.post(OURS, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
    board.post(THEIRS, WRITER, 'sample-writer', 'Another discussion', 'An invented post of theirs.',
               '2026-01-01T13:00:00Z')
    board.comment(FIRST, OURS, WRITER, 'sample-writer', 'An invented reply to the post.', '2026-01-01T12:05:00Z',
                  notify='post_comment')

    step('Nothing is there yet', 'status', 'status')
    step('Create the inbox', 'init', 'init')
    step('It cannot be created twice', 'init', 'init')
    step('The reading preferences of a new inbox', 'settings', 'settings')
    step('No source has been read yet', 'status', 'status')

    step('Collect for the first time', 'collect', 'collect')
    step('Read what arrived', 'list', 'list')
    step('One message, whole', f'show moltbook {FIRST}', 'show', source='moltbook', id=FIRST)
    step('What it answers, read from the board', f'context moltbook {FIRST}', 'context', source='moltbook', id=FIRST)
    step('The same from the inbox alone', f'context --local moltbook {FIRST}', 'context',
         source='moltbook', id=FIRST, local=True)
    step('A message that is not in the inbox', 'show example 9', 'show', source='example', id='9')
    step('An id the board cannot have', 'context moltbook nine', 'context', source='moltbook', id='nine')

    step('Mark it read', f'mark read moltbook {FIRST}', 'mark', action='read', source='moltbook', id=FIRST)
    step('It needs an answer', f'mark needs-reply moltbook {FIRST}', 'mark',
         action='needs-reply', source='moltbook', id=FIRST)
    # The agent answers on the board itself. Boardmail only records where the answer is.
    board.comment(ANSWER, OURS, ME, 'sample-agent', 'An invented answer of ours.', '2026-01-01T12:10:00Z', parent=FIRST)
    step('Answered, but where?', f'mark replied moltbook {FIRST}', 'mark',
         action='replied', source='moltbook', id=FIRST)
    step('Record the answer', f'mark replied --ref {board.url(OURS, ANSWER)} moltbook {FIRST}', 'mark',
         action='replied', source='moltbook', id=FIRST, ref=board.url(OURS, ANSWER))
    step('It needs no answer any more', f'mark clear-reply moltbook {FIRST}', 'mark',
         action='clear-reply', source='moltbook', id=FIRST)
    step('Read the other message', 'mark read example 1', 'mark', action='read', source='example', id='1')
    step('Keep it unread after all', 'mark unread example 1', 'mark', action='unread', source='example', id='1')

    step('No tags yet', 'tags', 'tags')
    step('Tag the thread', f'tag add follow-up moltbook {OURS}', 'tag_add',
         tag='follow-up', source='moltbook', thread=OURS)
    step('Tag one message', 'tag add later example --message 1', 'tag_add', tag='later', source='example', id='1')
    step('A name a tag cannot have', f'tag add Follow.Up moltbook {OURS}', 'tag_add',
         tag='Follow.Up', source='moltbook', thread=OURS)
    step('The tags in use', 'tags', 'tags')
    step('What one tag holds', 'tag show follow-up', 'tag_show', tag='follow-up')
    step('Read by tag', 'list --tag follow-up', 'list', tag='follow-up')
    step('Take a tag off', 'tag remove later example --message 1', 'tag_remove', tag='later', source='example', id='1')
    step('What is unread and has no tag', 'list --unread --scope all --untagged', 'list',
         unread=True, scope='all', untagged=True)

    step('Nothing is followed yet', 'subscriptions', 'subscriptions')
    step('Follow a thread of someone else', f'subscribe moltbook {THEIRS}', 'subscribe',
         source='moltbook', thread=THEIRS)
    step('The thread of the other message has no id to follow', 'subscribe example 42', 'subscribe',
         source='example', thread='42')
    step('And its board has no subscriptions', f'subscribe example {THEIRS}', 'subscribe',
         source='example', thread=THEIRS)
    step('What is followed', 'subscriptions', 'subscriptions')

    clock.advance(600)
    board.comment(SECOND, OURS, WRITER, 'sample-writer', 'An invented reply to our answer.', '2026-01-01T12:20:00Z',
                  parent=ANSWER, notify='comment_reply')
    board.comment(ASIDE, THEIRS, OTHER, 'another-writer', 'An invented comment for nobody in particular.',
                  '2026-01-01T13:05:00Z')
    board.comment(NAMED, THEIRS, WRITER, 'sample-writer', 'What does @sample-agent think of this invented point?',
                  '2026-01-01T13:10:00Z', parent=ASIDE)
    page = step('Collect again and read what is new', 'check --after 2', 'check', after=2)
    latest = page['next_after']
    step('Mail that has arrived does not wait', 'wait --after 2 --timeout 0', 'wait', after=2, timeout=0)
    step('No mail after the last one', f'wait --after {latest} --timeout 0', 'wait', after=latest, timeout=0)
    step('The whole thread with its context', f'expand --through {latest} moltbook {OURS}', 'expand',
         source='moltbook', thread=OURS, through=latest)
    step('A thread needs its source', f'list --thread {OURS}', 'list', thread=OURS)
    # The result named a thread with activity and said how to read it. The arguments are used as they came.
    activity, = page['thread_activity']
    replay, grow = activity['replay']['arguments'], activity['expand']['arguments']
    step('Read the thread activity as the result suggests',
         'list --source {source} --thread {thread} --after {after} --through {through} --limit {limit} '
         '--scope {scope} --context {context}'.format(**replay), 'list', **replay)
    step('The same with its context', 'expand --after {after} --through {through} --limit {limit} {source} {thread}'
         .format(**grow), 'expand', **grow)

    step('Read everything, without context', 'settings --scope all --context none', 'settings',
         scope='all', context='none')
    step('What the new preferences show', 'list --after 2', 'list', after=2)
    step('A reset takes nothing else', 'settings --reset --scope all', 'settings', reset=True, scope='all')
    step('Back to the defaults', 'settings --reset', 'settings', reset=True)

    clock.advance(600)
    board.down = True
    step('The board is down', 'collect', 'collect')
    step('Is everything fresh?', 'status --require-fresh', 'status', require_fresh=True)
    step('Leave the board alone for now', 'pause moltbook', 'pause', source='moltbook')
    step('A source that is not configured', 'pause elsewhere', 'pause', source='elsewhere')
    step('Collect without it', 'collect', 'collect')
    board.down = False
    step('Read it again', 'resume moltbook', 'resume', source='moltbook')
    step('Stop following the thread', f'unsubscribe moltbook {THEIRS}', 'unsubscribe', source='moltbook', thread=THEIRS)
    step('What is still followed there', 'subscriptions --source moltbook', 'subscriptions', source='moltbook')
    clock.advance(60)
    step('Collect once more', 'collect', 'collect')
    clock.advance(300)
    step('Still fresh five minutes later', 'status --require-fresh', 'status', require_fresh=True)
    step('Not by a stricter limit', 'status --require-fresh --stale-after 120', 'status',
         require_fresh=True, stale_after=120)


# What MCP answers differently, by the title of the step. The tool catalog says what a tag name may be, so a bad
# name is refused as an argument before the tag command sees it.
ONLY_MCP = {'A name a tag cannot have': {
    'event': 'error', 'error': 'invalid_arguments', 'next_action': 'check_command_help_and_returned_message_ids',
    'history_complete': False}}


class InboxStoryTests(unittest.TestCase):
    def told(self, entry):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        for name in ('custom_board.py', 'custom_feed.json'):
            shutil.copyfile(EXAMPLES / name, home / name)
        (home / 'moltbook.key').write_text('an-invented-key\n')
        (home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key', 'mention_aliases': ['@sample-agent']},
            'example': {'account_id': 'demo-agent', 'adapter': 'custom_board.py', 'feed_file': 'custom_feed.json',
                        'batch_size': 1}}}))
        board, clock = kit.Moltbook(ME, 'sample-agent'), kit.Clock(START)
        with kit.Network({board.HOST: board}), kit.fixed(clock):
            return kit.told(lambda step: story(step, board, clock), home, entry)

    def test_through_the_command_line(self):
        kit.check_stored(self, 'story_inbox.txt', kit.transcript(self.told('cli')))

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_through_mcp(self):
        kit.check_both(self, self.told('cli'), self.told('mcp'), ONLY_MCP)


if __name__ == '__main__':
    unittest.main()
