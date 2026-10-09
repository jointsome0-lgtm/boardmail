"""The agent guide is an introduction that is written by hand, and under it a line for each command that is made
from the command table. So the guide says nothing about a command that the command does not say itself."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from boardmail import table
import kit
from test_agent_view import view


TESTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('agent_guide', TESTS.parent / 'scripts/agent_guide.py')
guide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guide)
# What an agent must know that no single command says: the order of a session, how to read a result, and what to
# do on an error.
BY_HAND = ('## A session', '## Results', '## Errors')
LINK = re.compile(r'\]\(([^)#\s]+)(?:#([^)\s]+))?\)')


def slug(heading):
    """The anchor that GitHub gives a heading."""
    return re.sub(r'[^\w -]', '', heading.lower()).replace(' ', '-')


class AgentGuideTests(unittest.TestCase):
    maxDiff = None    # a line that differs is shown whole

    def setUp(self):
        self.stored = guide.STORED.read_text(encoding='utf-8')

    def test_the_stored_guide_is_what_the_script_makes(self):
        self.assertEqual(self.stored.count(guide.MARK), 1)
        self.assertEqual(self.stored, guide.guide(self.stored), f'Run {guide.COMMAND}')

    def test_a_command_has_the_line_that_the_list_of_commands_gives_it(self):
        pages, made = view.cli_tree(), guide.made().splitlines()
        self.assertEqual(len(made), len(table.COMMANDS))
        for line, name in zip(made, table.COMMANDS):
            words = kit.words({'tool': 'boardmail_' + name, 'arguments': {}})
            with self.subTest(command=name):
                summary = pages[' '.join(['boardmail', *words])]['summary']
                self.assertEqual(line, f'- `{" ".join(words)}`: {summary}.')

    def test_the_introduction_has_what_no_single_command_says(self):
        by_hand = self.stored.partition(guide.MARK)[0]
        found = [by_hand.find(f'\n{heading}\n') for heading in BY_HAND]
        self.assertNotIn(-1, found)
        self.assertEqual(found, sorted(found))

    def test_a_link_of_the_guide_leads_to_a_file_and_a_heading_that_are_there(self):
        links = LINK.findall(self.stored)
        self.assertTrue(links)
        for path, anchor in links:
            with self.subTest(link=f'{path}#{anchor}'):
                target = guide.ROOT / path
                self.assertTrue(target.is_file())
                if anchor:
                    headings = re.findall(r'(?m)^#+ (.*)$', target.read_text(encoding='utf-8'))
                    self.assertIn(anchor, {slug(heading) for heading in headings})

    def test_the_script_prints_the_made_part_and_writes_it_under_what_is_by_hand(self):
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(guide.main([]), 0)
        self.assertEqual(printed.getvalue(), guide.made())
        by_hand = self.stored.partition(guide.MARK)[0]
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / 'guide.md'
            with patch.object(guide, 'STORED', copy):
                copy.write_text(f'{by_hand}{guide.MARK}\n\n- `gone`: A line of another table.\n', encoding='utf-8')
                self.assertEqual(guide.main(['--update']), 0)
                self.assertEqual(copy.read_text(encoding='utf-8'), f'{by_hand}{guide.MARK}\n\n{guide.made()}')
                # A guide without the line has no place for the made part, and is left as it is.
                copy.write_text(by_hand, encoding='utf-8')
                with self.assertRaises(ValueError):
                    guide.main(['--update'])
                self.assertEqual(copy.read_text(encoding='utf-8'), by_hand)


if __name__ == '__main__':
    unittest.main()
