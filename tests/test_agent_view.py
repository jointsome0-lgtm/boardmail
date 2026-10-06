"""What an agent reads before its first call is the stored text, and the size report counts it."""
import argparse
import difflib
import functools
import importlib.util
from pathlib import Path
import re
import unittest


TESTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('agent_view', TESTS.parent / 'scripts/agent_view.py')
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)
NO_EXTRA = 'From the source checkout, install .[mcp] to test the optional MCP interface'


def command_line(renamed=None, section=None, **given):
    """A small command line. given holds, by call, the arguments of that call to change or add."""
    def use(call, **usual):
        return {**usual, **given.get(call, {})}

    root = argparse.ArgumentParser(**use('root', prog='tool', description='About the tool.', epilog='The end.'))
    if renamed:
        root._optionals.title = renamed
    holder = root.add_argument_group(**section) if section else root
    holder.add_argument('--loud', **use('flag', action='store_true', help='Say more'))
    holder.add_argument('--after', **use('option', type=int, default=0, help='Start after this number'))
    either = root.add_mutually_exclusive_group(**use('either'))
    either.add_argument('--one', action='store_true')
    either.add_argument('--other', action='store_true')
    below = root.add_subparsers(**use('commands', dest='command'))
    first = below.add_parser('first', **use('first', help='Do the first thing', description='First.'))
    first.add_argument('name', **use('name', help='A name'))
    below.add_parser('second', **use('second'))
    return root


class AgentViewTests(unittest.TestCase):
    def assertStored(self, part, current):
        stored = view.STORED[part].read_text(encoding='utf-8')
        if current != stored:
            changes = difflib.unified_diff(stored.splitlines(), current.splitlines(), 'stored', 'now', n=1, lineterm='')
            self.fail(f'What an agent reads changed. If that is meant, run {view.COMMAND} and review the difference '
                      f'in {view.STORED[part].name}.\n' + '\n'.join(changes))

    def assertAllDiffer(self, changes, written):
        seen = {}
        for change in changes:
            first = seen.setdefault(written(change), change)
            self.assertIs(first, change, 'The view is the same text for both')

    def test_command_line(self):
        self.assertStored('cli', view.cli_view())

    @unittest.skipIf(view.mcp_missing(), NO_EXTRA)
    def test_mcp_server(self):
        self.assertStored('mcp', view.mcp_view())

    def test_outline_tells_values_apart(self):
        said = 'One sentence here. ' * 6 + 'The last one.'
        values = [0, '0', None, 'null', 'None', True, 'true', 1, 1.0, '1', '1.0', 10**100, 2 * 10**100, str(10**100),
                  '', {}, '{}', [], '[]', 'a', ' a', 'a ', 'a\n', 'a\nb', 'a\n\nb',
                  'a\n b', 'a\rb', '|', '>', '- a', ['a'], ['a', 'b'], 'a, b', ['a, b'], [['a'], ['b']], [['a', 'b']],
                  {'a': 'b'}, 'a: b', {'a': 'b: c'}, {'a: b': 'c'}, {'a': {'b': 'c'}}, {'a': ['b']}, {'a': None},
                  {'a': 'null'}, [{'a': 'b'}], [{'a': 'b c'}], {'a': 'b c'}, {'a b': 'c'}, {'a': '|'}, {'a': 'b\n'},
                  [10**100], [2 * 10**100], {'a': 10**100}, {'a': str(10**100)},
                  said, said.replace('. ', '.\n', 1), said.replace('. ', '.  ', 1), said + ' ', [said], {'a': said},
                  {'a': 'b', 'c': said}, {'a': 'b\nc: ' + said}]
        self.assertAllDiffer([[value] for value in values], lambda held: '\n'.join(view.lines('key:', held[0])))
        self.assertEqual('\n'.join(view.lines('key:', said)),
                         'key: >\n' + '\n'.join(['  One sentence here.'] * 6 + ['  The last one.']))

    def test_command_line_view_holds_what_the_parser_was_given(self):
        tree = view.cli_tree(command_line())
        self.assertEqual(tree['tool'], {
            'description': 'About the tool.',
            'options': {'--loud': {'help': 'Say more', 'takes': 'no value'},
                        '--after AFTER': {'help': 'Start after this number', 'type': 'int', 'default': 0},
                        '--one': {'takes': 'no value', 'not with': ['--other']},
                        '--other': {'takes': 'no value', 'not with': ['--one']}},
            'commands': {'first': 'Do the first thing', 'second': None}, 'command required': False,
            'epilog': 'The end.', 'formatter': 'HelpFormatter'})
        self.assertEqual(tree['tool first'], {'summary': 'Do the first thing', 'description': 'First.',
                                              'arguments': {'name': {'help': 'A name'}}})
        self.assertEqual(tree['tool second'], {})
        self.assertEqual(tree['size'], {'help pages': 3, 'commands that run': 2, 'help text': len(
            'About the tool.' 'The end.' 'Say more' 'Start after this number' 'Do the first thing' 'First.' 'A name')})
        changes = [
            {}, {'renamed': 'Tool options'}, {'section': {'title': 'Reading'}},
            {'section': {'title': 'Reading', 'description': 'What to read.'}},
            {'root': {'prog': 'other'}}, {'root': {'usage': 'tool COMMAND'}}, {'root': {'description': 'About it.'}},
            {'root': {'epilog': 'An end.'}}, {'root': {'add_help': False}},
            {'root': {'formatter_class': argparse.RawTextHelpFormatter}},
            {'root': {'formatter_class': functools.partial(argparse.HelpFormatter, max_help_position=40)}},
            {'flag': {'help': 'Say less'}}, {'flag': {'default': None}}, {'flag': {'default': True}},
            {'flag': {'action': 'store_false'}}, {'flag': {'action': 'count'}}, {'flag': {'help': argparse.SUPPRESS}},
            {'option': {'help': 'Start after that'}}, {'option': {'default': 1}}, {'option': {'default': None}},
            {'option': {'default': '0'}}, {'option': {'default': argparse.SUPPRESS}}, {'option': {'type': float}},
            {'option': {'type': None}}, {'option': {'metavar': 'N'}}, {'option': {'metavar': ''}},
            {'option': {'dest': 'start'}},
            {'option': {'nargs': '?'}}, {'option': {'nargs': '?', 'const': 5}}, {'option': {'nargs': 1}},
            {'option': {'nargs': 2}}, {'option': {'choices': [0, 1]}}, {'option': {'choices': []}},
            {'option': {'required': True}}, {'option': {'action': 'append'}},
            {'either': {'required': True}},
            {'commands': {'help': 'What to do'}}, {'commands': {'metavar': 'COMMAND'}},
            {'commands': {'title': 'Commands'}}, {'commands': {'title': 'Commands', 'description': 'Choose one.'}},
            {'commands': {'required': True}},
            {'first': {'help': 'Do one thing'}}, {'first': {'description': 'One.'}}, {'first': {'prog': 'tool 1'}},
            {'first': {'epilog': 'See also second.'}}, {'first': {'usage': 'tool first NAME'}},
            {'first': {'aliases': ['1st']}}, {'first': {'add_help': False}},
            {'name': {'help': 'One name'}}, {'name': {'metavar': 'NAME'}}, {'name': {'nargs': '?'}},
            {'name': {'nargs': '*'}}, {'name': {'nargs': '+'}}, {'name': {'nargs': '?', 'default': 'x'}},
            {'name': {'choices': ['a', 'b']}}, {'name': {'type': int}},
            {'second': {'help': 'Do the second thing'}}, {'second': {'description': 'Second.'}},
        ]
        self.assertAllDiffer(changes, lambda change: view.written('cli', view.cli_tree(command_line(**change))))

    @unittest.skipIf(view.mcp_missing(), NO_EXTRA)
    def test_mcp_view_holds_what_the_server_was_given(self):
        from mcp.server import Server
        from mcp.types import ListPromptsResult, ListToolsResult, Tool, ToolAnnotations

        def tree(change):
            async def tools(ctx, params):
                hints = change.get('hints')
                return ListToolsResult(tools=[Tool(**{'name': 'one', 'input_schema': {'type': 'object'},
                                                      'annotations': hints and ToolAnnotations(**hints),
                                                      **change.get('tool', {})})])

            async def call(ctx, params):
                raise NotImplementedError

            return view.mcp_tree(Server(**{'name': 'board', 'version': '1', 'on_list_tools': tools,
                                           'on_call_tool': call, **change.get('server', {})}))

        async def prompts(ctx, params):
            return ListPromptsResult(prompts=[])

        schema = len('{"type":"object"}')
        self.assertEqual(tree({}), {
            'size': {'tools': 1, 'tool descriptions': 0, 'input schemas': schema,
                     'output schema, once for each tool': 0, 'server instructions': 0,
                     'tool catalog as JSON': len('[{"name":"one","inputSchema":}]') + schema},
            'server': {'name': 'board', 'version': '1'},
            'tool one': {'inputSchema': {'type': 'object'}}})
        changes = [
            {}, {'server': {'name': 'other'}}, {'server': {'version': '2'}}, {'server': {'title': 'Board'}},
            {'server': {'description': 'A board.'}}, {'server': {'instructions': 'Read first.'}},
            {'server': {'website_url': 'https://example.invalid/'}}, {'server': {'on_list_prompts': prompts}},
            {'tool': {'name': 'two'}}, {'tool': {'title': 'One'}}, {'tool': {'description': 'Does one thing.'}},
            {'tool': {'input_schema': {'type': 'object', 'properties': {}}}},
            {'tool': {'output_schema': {'type': 'object'}}}, {'tool': {'_meta': {'example.invalid/key': 'value'}}},
            {'hints': {'title': 'One'}}, {'hints': {'read_only_hint': True}}, {'hints': {'read_only_hint': False}},
            {'hints': {'destructive_hint': True}}, {'hints': {'idempotent_hint': True}},
            {'hints': {'open_world_hint': True}},
        ]
        self.assertAllDiffer(changes, lambda change: view.written('mcp', tree(change)))

    def test_size_report(self):
        cli = view.cli_tree()['size']
        mcp = {'tools': 2, 'tool descriptions': 1234, 'input schemas': 56, 'output schema': 7,
               'output schema, once for each tool': 14, 'server instructions': 890, 'tool catalog as JSON': 2345}
        report = view.report(cli, mcp)
        for label, number in {**mcp, **cli}.items():
            self.assertRegex(report, rf'(?m)^  {re.escape(label)} +{number:,}\b')
        self.assertRegex(report, r'(?m)^  printed help +[\d,]+ characters on \d+ pages at 80 columns, Python 3\.\d+$')
        self.assertIn(view.EXTRA, view.report(cli, None))


if __name__ == '__main__':
    unittest.main()
