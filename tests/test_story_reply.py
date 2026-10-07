"""One offline session with the reply journal, told through the CLI and through MCP. Every result is the stored
one."""
import json
from pathlib import Path
import tempfile
import unittest

from examples.fixtures import uid
import kit


START = 1_800_000_000
ME, WRITER = uid(2), uid(10)
OURS = uid(201)                                      # a post of ours
FIRST, SECOND, THIRD = uid(211), uid(212), uid(213)  # three replies to it
ANSWERS = uid(221), uid(222), uid(223)               # our answers to them, as the board numbers them


def story(step, board, clock, home):
    def file(name, text):
        # The command line reads a reply from a file. MCP takes the text itself.
        (home / name).write_bytes(text.encode())

    def publish(answer, to, text):
        """The agent posts on the board itself. Boardmail never does. Where the reply is, and how the board
        keeps it."""
        kept = board.comment(answer, OURS, ME, 'sample-agent', text, '2026-01-01T13:00:00Z', parent=to)
        return board.url(OURS, answer), kept

    board.post(OURS, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
    for minute, question in enumerate((FIRST, SECOND, THIRD), 1):
        board.comment(question, OURS, WRITER, 'sample-writer', f'Invented question {minute}.',
                      f'2026-01-01T12:0{minute}:00Z', notify='post_comment')
    step('Create the inbox', 'init', 'init')
    step('Collect three questions under our post', 'collect', 'collect')
    step('No reply has been saved yet', 'reply list', 'reply_list')

    # The first answer goes well.
    draft, text = 'An invented answer.', 'An invented answer, said better.\n'
    file('draft.txt', draft)
    file('answer.txt', text)
    saved = step('Save the answer before sending it', f'reply prepare moltbook {FIRST} --body-file draft.txt',
                 'reply_prepare', source='moltbook', id=FIRST, body=draft)
    old = saved['reply']['idempotency_key']
    step('Other text does not replace it by accident', f'reply prepare moltbook {FIRST} --body-file answer.txt',
         'reply_prepare', source='moltbook', id=FIRST, body=text)
    saved = step('Replace the draft on purpose',
                 f'reply prepare moltbook {FIRST} --body-file answer.txt --replace-key {old}',
                 'reply_prepare', source='moltbook', id=FIRST, body=text, replace_key=old)
    key = saved['reply']['idempotency_key']
    step('The key of the old draft is refused', f'reply begin moltbook {FIRST} --key {old}', 'reply_begin',
         source='moltbook', id=FIRST, key=old)
    clock.advance(60)
    step('Begin, just before sending', f'reply begin moltbook {FIRST} --key {key}', 'reply_begin',
         source='moltbook', id=FIRST, key=key)
    where, kept = publish(ANSWERS[0], FIRST, text)
    clock.advance(60)
    file('readback.txt', kept['content'])
    step('Confirm with the text read back from the board',
         f'reply confirm moltbook {FIRST} --key {key} --ref {where} --readback-file readback.txt', 'reply_confirm',
         source='moltbook', id=FIRST, key=key, ref=where, readback_body=kept['content'])

    # The second answer is interrupted: the agent sends it and never sees what the board said.
    text = 'Another invented answer.'
    file('second.txt', text)
    saved = step('Save the second answer', f'reply prepare moltbook {SECOND} --body-file second.txt', 'reply_prepare',
                 source='moltbook', id=SECOND, body=text)
    key = saved['reply']['idempotency_key']
    clock.advance(60)
    step('Begin the second one', f'reply begin moltbook {SECOND} --key {key}', 'reply_begin',
         source='moltbook', id=SECOND, key=key)
    where, kept = publish(ANSWERS[1], SECOND, text)
    kept['verification_status'] = 'pending'

    # Later the agent starts again and knows nothing of this.
    clock.advance(3600)
    step('Was anything left unfinished?', 'status', 'status')
    step('The attempts that are not settled', 'reply list', 'reply_list')
    step('The message says so too', f'show moltbook {SECOND}', 'show', source='moltbook', id=SECOND)
    step('The journal of that attempt', f'reply show moltbook {SECOND}', 'reply_show', source='moltbook', id=SECOND)
    step('A second begin does not allow a second send', f'reply begin moltbook {SECOND} --key {key}', 'reply_begin',
         source='moltbook', id=SECOND, key=key)
    # The agent looks at the thread on the board and finds a reply that may be its own.
    step('The board has not verified that reply yet', f'reply verify moltbook {SECOND} --key {key} --ref {where}',
         'reply_verify', source='moltbook', id=SECOND, key=key, ref=where)
    step('The journal keeps where it may be', f'reply show moltbook {SECOND}', 'reply_show',
         source='moltbook', id=SECOND)
    clock.advance(600)
    kept['verification_status'] = 'verified'
    step('The board has verified it now', f'reply verify moltbook {SECOND} --key {key} --ref {where}',
         'reply_verify', source='moltbook', id=SECOND, key=key, ref=where)
    step('The journal with its receipt', f'reply show moltbook {SECOND}', 'reply_show', source='moltbook', id=SECOND)

    # The third question was answered on the board before it was collected.
    where, _ = publish(ANSWERS[2], THIRD, 'An invented answer that was sent earlier.')
    step('Record a reply that is already there', f'mark replied --ref {where} moltbook {THIRD}', 'mark',
         action='replied', source='moltbook', id=THIRD, ref=where)
    file('third.txt', 'One more invented answer.')
    step('It cannot get a second reply by mistake', f'reply prepare moltbook {THIRD} --body-file third.txt',
         'reply_prepare', source='moltbook', id=THIRD, body='One more invented answer.')
    step('Nothing is left unfinished', 'reply list', 'reply_list')


class ReplyStoryTests(unittest.TestCase):
    def told(self, entry):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home / 'moltbook.key').write_text('an-invented-key\n')
        (home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key'}}}))
        board, clock = kit.Moltbook(ME, 'sample-agent'), kit.Clock(START)
        with kit.Network({board.HOST: board}), kit.fixed(clock):
            return kit.told(lambda step: story(step, board, clock, home), home, entry)

    def test_through_the_command_line(self):
        kit.check_stored(self, 'story_reply.txt', kit.transcript(self.told('cli')))

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_through_mcp(self):
        # MCP takes the text of a reply where the command line takes a file. No step answers differently.
        kit.check_both(self, self.told('cli'), self.told('mcp'), {})


if __name__ == '__main__':
    unittest.main()
