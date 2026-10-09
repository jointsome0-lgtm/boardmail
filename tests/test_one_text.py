"""A command has one text, and both entry points show it: the command line with its names as they are typed, and a
tool with its names as they are called.

The commands of ONE_TEXT have their one text. The others still have a text for each of the two readers.
"""
import unittest

from boardmail import mcp, table
import kit
from test_agent_view import view


ONE_TEXT = {'init', 'collect', 'status', 'settings', 'pause', 'resume', 'subscribe', 'unsubscribe', 'subscriptions',
            'tags', 'tag_show', 'tag_add', 'tag_remove'}
# What a command without the one text says to each reader apart. Its description is the fourth, and it may have none.
APART = ('summary', 'epilog', 'tool')


def texts(command, typed):
    """The one text of a command for a reader, and that of each of its arguments by name. None is an argument
    that says nothing."""
    return table.shown(command.text, command, typed), {
        argument.name: None if argument.text is None else table.shown(argument.text, command, typed)
        for argument in command.arguments}


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
    def test_a_command_has_one_text_or_a_text_for_each_reader(self):
        self.assertEqual({name for name, command in table.COMMANDS.items() if command.text is not None}, ONE_TEXT)
        for name, command in table.COMMANDS.items():
            with self.subTest(command=name):
                apart = [getattr(command, field) for field in APART]
                if name in ONE_TEXT:
                    self.assertEqual([*apart, command.description], [None] * 4)
                    self.assertEqual({(argument.help, argument.tool) for argument in command.arguments} - {(None, None)},
                                     set())
                    # An option is named in braces, so that a tool does not read how it is typed. And a text has
                    # no per cent sign, which the parser of the command line reads as its own.
                    written = [command.text, *(argument.text or '' for argument in command.arguments)]
                    self.assertEqual([text for text in written if '--' in text or '%' in text], [])
                else:
                    self.assertNotIn(None, apart)
                    self.assertEqual({argument.text for argument in command.arguments} - {None}, set())

    def test_the_help_page_of_a_command_is_its_one_text_as_it_is_typed(self):
        pages = view.cli_tree()
        for name in sorted(ONE_TEXT):
            command = table.COMMANDS[name]
            page = pages[' '.join(['boardmail', *kit.words({'tool': 'boardmail_' + name, 'arguments': {}})])]
            text, arguments = texts(command, typed=True)
            with self.subTest(command=name):
                self.assertEqual(page['description'], text)
                self.assertNotIn('epilog', page)
                # The text has no lines of its own, so the page breaks it into lines.
                self.assertEqual(page['formatter'], 'HelpFormatter')
                # The line of the command in the list of commands is the first sentence, without its full stop.
                self.assertEqual(page['summary'] + '.', text[:len(page['summary']) + 1])
                self.assertNotIn('. ', page['summary'])
                said = {**page.get('arguments', {}), **page.get('options', {})}
                self.assertEqual({argument.name: said[argument.typed].get('help') for argument in command.arguments},
                                 arguments)

    def test_the_schema_of_a_tool_says_the_one_text_of_each_argument_as_it_is_called(self):
        for name in sorted(ONE_TEXT):
            command = table.COMMANDS[name]
            with self.subTest(command=name):
                properties = mcp.input_schema(command)['properties']
                self.assertEqual({name: said.get('description') for name, said in properties.items()},
                                 texts(command, typed=False)[1])

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_the_description_of_a_tool_is_the_one_text_of_its_command_as_it_is_called(self):
        tools = view.mcp_tree()
        for name in sorted(ONE_TEXT):
            command = table.COMMANDS[name]
            with self.subTest(command=name):
                self.assertEqual(tools['tool boardmail_' + name]['description'], texts(command, typed=False)[0])


if __name__ == '__main__':
    unittest.main()
