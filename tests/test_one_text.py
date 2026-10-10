"""A text of the command table names a command or an argument as its reader types or calls it, and a bound or a
default that it names is the one that holds."""
import re
import unittest

from boardmail import store, table


# status applies the default of this argument itself, so the kind of the argument has none.
APPLIED = {('status', 'stale_after'): store.STALE_AFTER}


class NameTests(unittest.TestCase):
    def test_a_name_in_braces_is_written_as_it_is_typed_and_as_it_is_called(self):
        prepare, add = table.COMMANDS['reply_prepare'], table.COMMANDS['tag_add']
        for text, command, typed, called in (
                ('Call {reply_begin} first, then {collect}.', prepare,
                 'Call reply begin first, then collect.', 'Call reply_begin first, then collect.'),
                ('{.replace_key} and {.body} of {.source}', prepare,
                 '--replace-key and --body-file of SOURCE', 'replace_key and body of source'),
                ('{.thread} or {.id}', add, 'THREAD or --message', 'thread or id'),
                ('{list.unread}, {tag_show.tag} and {reply_confirm.readback_body}', add,
                 '--unread, TAG and --readback-file', 'unread, tag and readback_body'),
                # Braces that hold no name stay as they are.
                ('{id, status: same_as_root} {} {.} { tags } {reply show}', add, None, None)):
            with self.subTest(text=text):
                self.assertEqual(table.shown(text, command, typed=True), typed or text)
                self.assertEqual(table.shown(text, command, typed=False), called or text)

    def test_a_name_that_the_table_does_not_have_is_refused(self):
        for text in ('{replies}', '{.body}', '{list.body}', '{lists.unread}'):
            for typed in (True, False):
                with self.subTest(text=text, typed=typed), self.assertRaises(KeyError):
                    table.shown(text, table.COMMANDS['tag_add'], typed=typed)


class OneTextTests(unittest.TestCase):
    def test_a_bound_or_a_default_that_a_text_names_is_the_one_that_holds(self):
        # The command line shows no schema, so a text names the bounds and the default of an argument by hand.
        named = []
        for command in table.COMMANDS.values():
            for argument in command.arguments:
                text, holds = argument.text or '', {**argument.kind}
                holds.setdefault('default', APPLIED.get((command.name, argument.name)))
                with self.subTest(command=command.name, argument=argument.name):
                    for found, limits in ((re.search(r'(\d+) to (\d+)', text), ('minimum', 'maximum')),
                                          (re.search(r'default (\d+)', text), ('default',))):
                        if found:
                            named.append((command.name, argument.name))
                            self.assertEqual([int(number) for number in found.groups()],
                                             [holds.get(limit) for limit in limits])
        self.assertIn(('wait', 'timeout'), named)
        self.assertIn(('list', 'limit'), named)


if __name__ == '__main__':
    unittest.main()
