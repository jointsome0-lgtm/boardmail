import unittest
import json
from hashlib import sha256
from unittest.mock import patch
from urllib.error import URLError
from boardmail import adapter_fourclaw as adapter
from boardmail.adapters import validate

THREAD = "00000000-0000-4000-8000-000000000001"


def post(author, body, op=False):
    return (f'<div class="claw-post {"op" if op else "reply"}">'
            f'<div class="claw-post-header"><span class="claw-post-name"><span>{author}</span></span>'
            '<time datetime="2026-09-07T12:00:00.001Z">date</time></div>'
            f'<div class="claw-post-body">{body}</div></div>')


def page(owner="Other", replies=None):
    replies = replies or []
    keys = []
    for reply in replies:
        digest = sha256(reply.encode()).hexdigest()[:32]
        key = f'{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:]}'
        keys.append('["$","div",' + json.dumps(key) + ',{"className":"claw-post reply"}]')
    script = '<script>self.__next_f.push(' + json.dumps([1, ''.join(keys)]) + ')</script>'
    return '<div class="claw-section-title">A title</div>' + post(owner, "Opening", True) + ''.join(replies) + script


class FourclawTests(unittest.TestCase):
    def collect(self, html, state=None, known=frozenset(), **extra):
        with patch.object(adapter, '_fetch', return_value=html):
            batch = adapter.collect(dict(account_id="Reader", watched_threads=[THREAD], **extra), state or {}, known)
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

    def test_owned_op_and_idempotence_after_reordering(self):
        first = post('Other', 'First reply')
        second = post('Another', 'Second reply')
        initial = self.collect(page('Reader', [first, second]))
        self.assertEqual([m['kind'] for m in initial.messages], ['reply_to_post'] * 2)
        replay = self.collect(page('Reader', [second, first]), known={m['id'] for m in initial.messages})
        self.assertEqual(replay.messages, [])

    def test_unavailable_stays_eligible_and_does_not_starve(self):
        threads = [f'00000000-0000-4000-8000-{n:012d}' for n in range(1, 7)]
        settings = dict(account_id='Reader', watched_threads=threads)
        seen = []
        def fetch(thread):
            seen.append(thread)
            if thread == threads[0]: raise URLError('secret upstream prose')
            return page(replies=[post('Other', '@Reader hi')])
        with patch.object(adapter, '_fetch', side_effect=fetch):
            first = adapter.collect(settings, {}, set())
            second = adapter.collect(settings, first.state, {m['id'] for m in first.messages})
        self.assertEqual(first.error, 'fourclaw_network_error')
        self.assertEqual(len(first.messages), 3)
        self.assertTrue(set(threads).issubset(seen))
        self.assertEqual(seen.count(threads[0]), 2)
        self.assertEqual(set(second.state), {'next_thread'})
        self.assertNotIn('secret', str(first))

    def test_invalid_public_page_is_not_mail(self):
        batch = self.collect('<html>Please sign in<script>@Reader body</script></html>')
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')
        self.assertEqual(batch.messages, [])
        recovered = self.collect(page(replies=[post('Other', '@Reader recovered')]), batch.state)
        self.assertEqual(len(recovered.messages), 1)

    def test_missing_public_reply_ids_fails_closed(self):
        html = page(replies=[post('Other', '@Reader hi')])
        batch = self.collect(html.split('<script>')[0])
        self.assertEqual(batch.messages, [])
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')

    def test_redirects_refused(self):
        self.assertIsNone(adapter._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_bad_thread_cannot_change_host(self):
        with patch.object(adapter, '_fetch') as fetch:
            batch = adapter.collect(dict(account_id='Reader', watched_threads=['../elsewhere']), {}, set())
        self.assertEqual(batch.error, 'invalid_config')
        fetch.assert_not_called()

    def test_malformed_later_thread_preserves_earlier_mail(self):
        settings = dict(account_id='Reader', watched_threads=[THREAD, '00000000-0000-4000-8000-000000000002'])
        with patch.object(adapter, '_fetch', side_effect=[page(replies=[post('Other', '@Reader hi')]), '<html>gone</html>']):
            batch = adapter.collect(settings, {}, set())
        self.assertEqual(len(batch.messages), 1)
        self.assertEqual(batch.error, 'fourclaw_invalid_public_page')
        validate(batch)


if __name__ == '__main__':
    unittest.main()
