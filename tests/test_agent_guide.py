"""The agent guide is an introduction that is written by hand, and under it a line for each command that is made
from the command table. So the guide says nothing about a command that the command does not say itself."""
import importlib.util
from pathlib import Path
import re
import unittest


TESTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('agent_guide', TESTS.parent / 'scripts/agent_guide.py')
guide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guide)
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


if __name__ == '__main__':
    unittest.main()
