import unittest
import json
from http.client import IncompleteRead
from urllib.error import URLError
from boardmail import adapter_fourclaw as adapter
from boardmail.adapters import validate
from examples.fixtures import FakeBoard

THREAD = "00000000-0000-4000-8000-000000000001"


def post(author, body, op=False):
    return (f'<div class="claw-post {"op" if op else "reply"}">'
            f'<div class="claw-post-header"><span class="claw-post-name"><span>{author}</span></span>'
            '<time datetime="2026-09-07T12:00:00.001Z">date</time></div>'
            f'<div class="claw-post-body">{body}</div></div>')


def page(owner="Other", replies=None, ids=None):
    replies = replies or []
    keys = []
    ids = ids or [f'10000000-0000-4000-8000-{n:012d}' for n in range(len(replies))]
    for key in ids:
        keys.append('["$","div",' + json.dumps(key) + ',{"className":"claw-post reply"}]')
    script = '<script>self.__next_f.push(' + json.dumps([1, ''.join(keys)]) + ')</script>'
    return '<div class="claw-section-title">A title</div>' + post(owner, "Opening", True) + ''.join(replies) + script


def thread(asked):
    """The thread whose page a request asks for."""
    return asked.url.removeprefix(adapter.HOST + '/t/')


def threads(pages):
    """An invented board that has these pages, by thread. A page that is an exception is how a request for it fails."""
    return FakeBoard(lambda asked: pages[thread(asked)])


class FourclawTests(unittest.TestCase):
    def collect(self, html, state=None, known=frozenset(), **extra):
        batch = adapter.collect(dict(account_id="Reader", watched_threads=[THREAD], **extra), state or {}, known,
                                fetch=threads({THREAD: html}))
        validate(batch)
        return batch

    def test_personal_scope_and_visible_text(self):
        html = page(replies=[post("Other", "@reader hello<br/>world &amp; friends"),
                            post("Other", "@Reader_suffix unrelated"), post("Reader", "@Reader own")])
        html += '<script>"@Reader private-looking payload"</script>'
        batch = self.collect(html)
        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.messages[0]['body'], '@reader hello\nworld & friends')
        self.assertEqual(batch.messages[0]['kind'], 'mention')
        self.assertEqual(batch.messages[0]['url'], f'{adapter.HOST}/t/{THREAD}')
        self.assertNotIn('discovery', batch.messages[0], 'A watched page is no subscription')

    def test_owned_op_and_idempotence_after_reordering(self):
        first = post('Other', 'First reply')
        second = post('Another', 'Second reply')
        ids = ['10000000-0000-4000-8000-000000000001', '10000000-0000-4000-8000-000000000002']
        initial = self.collect(page('Reader', [first, second], ids))
        self.assertEqual([m['kind'] for m in initial.messages], ['reply_to_post'] * 2)
        replay = self.collect(page('Reader', [second, post('Other', 'Edited first reply')], ids[::-1]), known={m['id'] for m in initial.messages})
        self.assertEqual(replay.messages, [])

    def test_unavailable_stays_eligible_and_does_not_starve(self):
        watching = [f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 7)]
        settings = dict(account_id='Reader', watched_threads=watching)
        board = threads({watched: page(replies=[post('Other', '@Reader hi')]) for watched in watching}
                        | {watching[0]: URLError('secret upstream prose')})
        first = adapter.collect(settings, {}, set(), fetch=board)
        second = adapter.collect(settings, first.state, {m['id'] for m in first.messages}, fetch=board)
        self.assertEqual({asked.board for asked in board.asked}, {'fourclaw'})
        seen = [thread(asked) for asked in board.asked]
        self.assertEqual(first.error, 'fourclaw_network_error')
        self.assertEqual(len(first.messages), 3)
        self.assertTrue(set(watching).issubset(seen))
        self.assertEqual(seen.count(watching[0]), 2)
        self.assertEqual(set(second.state), {'next_thread'})
        self.assertNotIn('secret', str(first))

    def test_invalid_public_page_is_not_mail(self):
        batch = self.collect('<html>Please sign in<script>@Reader body</script></html>')
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')
        self.assertEqual(batch.messages, [])
        recovered = self.collect(page(replies=[post('Other', '@Reader recovered')]), batch.state)
        self.assertEqual(len(recovered.messages), 1)

    def test_broken_response_preserves_confirmed_mail(self):
        settings = dict(account_id='Reader', watched_threads=[THREAD, '00000000-0000-4000-8000-000000000002'])
        board = FakeBoard([page(replies=[post('Other', '@Reader hi')]), IncompleteRead(b'partial')])
        batch = adapter.collect(settings, {}, set(), fetch=board)
        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.error, 'fourclaw_network_error')
        self.assertEqual(batch.unavailable, 1)
        validate(batch)

    def test_missing_public_reply_ids_fails_closed(self):
        html = page(replies=[post('Other', '@Reader hi')])
        batch = self.collect(html.split('<script>')[0])
        self.assertEqual(batch.messages, [])
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')

    def test_bad_thread_cannot_change_host(self):
        board = threads({})
        batch = adapter.collect(dict(account_id='Reader', watched_threads=['../elsewhere']), {}, set(), fetch=board)
        self.assertEqual(batch.error, 'invalid_config')
        self.assertEqual(board.asked, [])

    def test_malformed_later_thread_preserves_earlier_mail(self):
        settings = dict(account_id='Reader', watched_threads=[THREAD, '00000000-0000-4000-8000-000000000002'])
        board = FakeBoard([page(replies=[post('Other', '@Reader hi')]), '<div class><p>gone</p></div>'])
        batch = adapter.collect(settings, {}, set(), fetch=board)
        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')
        validate(batch)


if __name__ == '__main__':
    unittest.main()
