"""What an agent reads before its first call is the stored text, and the size report counts it."""
import argparse
import difflib
import functools
import importlib.util
import json
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

    @unittest.skipIf(view.mcp_missing(), NO_EXTRA)
    def test_a_command_takes_the_same_arguments_through_both_entry_points(self):
        from boardmail import cli

        def typed(parser, words=()):
            """Each command of a parser: the names that it keeps its arguments under, and those that it requires."""
            below = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
            if below:
                return {name: found for word, under in below[0].choices.items()
                        for name, found in typed(under, (*words, word)).items()}
            taken = [action for action in parser._actions if not isinstance(action, argparse._HelpAction)]
            return {'_'.join(words): ({action.dest for action in taken},
                                      {action.dest for action in taken if action.required})}

        commands = typed(cli.parser())
        tools = {key.removeprefix('tool boardmail_'): entry['inputSchema']
                 for key, entry in view.mcp_tree().items() if key.startswith('tool ')}
        self.assertEqual(sorted(commands), sorted(tools))
        for name, schema in tools.items():
            self.assertEqual(commands[name], (set(schema['properties']), set(schema['required'])), name)

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
            'size': {'tools': 1, 'tool descriptions': 0, 'input schemas': schema, 'server instructions': 0,
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
        mcp = {'tools': 2, 'tool descriptions': 1234, 'input schemas': 56, 'server instructions': 890,
               'tool catalog as JSON': 2345}
        results = {'pages of check, list and wait': (3, 4567), 'results of expand': (1, 89)}
        report = view.report(cli, mcp, results)
        for label, number in {**mcp, **cli}.items():
            self.assertRegex(report, rf'(?m)^  {re.escape(label)} +{number:,}\b')
        self.assertRegex(report, r'(?m)^  printed help +[\d,]+ characters on \d+ pages at 80 columns, Python 3\.\d+$')
        for label, (number, together) in results.items():
            self.assertRegex(report, rf'(?m)^  {re.escape(label)} +{number:,} with +{together:,} characters$')
        self.assertIn(view.EXTRA, view.report(cli, None, results))

    def test_results_of_a_small_story(self):
        def compact(value):
            return len(json.dumps(value, separators=(',', ':')))

        brief = {'root': {'id': '9', 'status': 'stored'}}
        first = {'id': '1', 'body': 'Three words here.', 'brief': brief}
        second = {'id': '2', 'body': 'None.'}
        source, thread = {'source': 'example', 'status': 'ok'}, {'thread_id': '9', 'count': 2}
        page = {'event': 'messages', 'messages': [first, second], 'sources': [source, source],
                'thread_activity': [thread]}
        empty = {'event': 'messages', 'messages': [], 'sources': [source], 'thread_activity': []}
        context, expanded = {'event': 'context', 'target': first}, {'event': 'expanded', 'items': [first, second]}
        others = [{'event': 'error', 'error': 'message_not_found'}, {'event': 'timeout', 'sources': [source]},
                  {'event': 'message', 'message': first}]
        steps = [('list\n --after 0', page), ('wait --timeout 0', empty), ('context example 1', context),
                 ('expand example 9', expanded), *(('show example 1', other) for other in others)]
        story = '\n'.join(f'== {number}. Step {number}\n$ boardmail {typed}\nexit code 0\n{json.dumps(result, indent=2)}\n'
                          for number, (typed, result) in enumerate(steps, 1))
        self.assertEqual(view.told(story), [(f'boardmail {typed}', result) for typed, result in steps])
        self.assertEqual(view.results_size([story, story]), {
            'pages of check, list and wait': (4, 2 * (compact(page) + compact(empty))),
            'messages on those pages': (4, 2 * (compact(first) + compact(second))),
            'bodies of those messages': (4, 2 * (len('Three words here.') + len('None.'))),
            'brief context of those messages': (2, 2 * compact(brief)),
            'source rows on those pages': (6, 6 * compact(source)),
            'thread summaries on those pages': (2, 2 * compact(thread)),
            'results of context': (2, 2 * compact(context)),
            'results of expand': (2, 2 * compact(expanded)),
        })
        with self.assertRaisesRegex(ValueError, 'not a command with its exit code'):
            view.told('== 1. A step without its command\nexit code 0\n{}\n')

    def test_results_are_counted_from_the_stored_stories(self):
        """The report reads the stories as the story tests wrote them: every step, and every page as a page."""
        stories = [path.read_text(encoding='utf-8') for path in view.STORIES]
        steps = [step for story in stories for step in view.told(story)]
        self.assertEqual(len(steps), sum(len(re.findall(r'(?m)^== \d+\. ', story)) for story in stories))
        named = {'check': [], 'list': [], 'wait': [], 'context': [], 'expand': []}
        for command, result in steps:
            words = command.split()
            self.assertEqual(words[0], 'boardmail')
            if words[1] in named and 'error' not in result and result['event'] != 'timeout':
                named[words[1]].append(result)
        pages = named['check'] + named['list'] + named['wait']
        for name, results in named.items():
            self.assertTrue(results, f'The stories hold no result of {name}')
        self.assertEqual({page['event'] for page in pages}, {'messages'})
        self.assertEqual({result['event'] for result in named['context']}, {'context'})
        self.assertEqual({result['event'] for result in named['expand']}, {'expanded'})
        size = view.results_size()
        counted = {kind: number for kind, (number, together) in size.items()}
        self.assertEqual(counted, {
            'pages of check, list and wait': len(pages),
            'messages on those pages': sum(len(page['messages']) for page in pages),
            'bodies of those messages': sum(len(page['messages']) for page in pages),
            'brief context of those messages': sum('brief' in message for page in pages for message in page['messages']),
            'source rows on those pages': sum(len(page['sources']) for page in pages),
            'thread summaries on those pages': sum(len(page['thread_activity']) for page in pages),
            'results of context': len(named['context']),
            'results of expand': len(named['expand']),
        })
        for kind, (number, together) in size.items():
            self.assertGreater(number, 0, kind)
            self.assertGreater(together, 0, kind)


if __name__ == '__main__':
    unittest.main()
