#!/usr/bin/env python3
"""The part of AGENT_GUIDE.md that is made from the command table.

    python scripts/agent_guide.py            print that part
    python scripts/agent_guide.py --update   write it into the guide

The guide is an introduction that is written by hand and, under the line MARK,
one line for each command: the command as it is typed, and the first sentence
of its one text, which is also its line in the list of commands of the command
line. So the guide says nothing about a command that the command does not say
itself. tests/test_agent_guide.py fails when the stored guide has anything
else under that line.
"""
import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
STORED = ROOT / 'AGENT_GUIDE.md'
COMMAND = 'python scripts/agent_guide.py --update'
MARK = f'<!-- The lines below are made from the command table. To make them again: {COMMAND} -->'


def made():
    """One line for each command of the table, in the order of the table."""
    from boardmail import table

    return ''.join(f'- `{table.shown("{" + name + "}", command, typed=True)}`: {table.page(command).summary}.\n'
                   for name, command in table.COMMANDS.items())


def guide(stored):
    """A guide with the part under MARK made again. What stands above the line is kept as it is."""
    by_hand, mark, _ = stored.partition(MARK)
    if not mark:
        raise ValueError(f'{STORED.name} has no line that says: {MARK}')
    return f'{by_hand}{MARK}\n\n{made()}'


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Print the part of AGENT_GUIDE.md that is made from the command table. '
                    'The top of this file says what it holds.')
    parser.add_argument('--update', action='store_true', help='Write that part into AGENT_GUIDE.md')
    args = parser.parse_args(argv)
    if args.update:
        STORED.write_text(guide(STORED.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
    else:
        print(made(), end='')
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))    # The table of this checkout, also where another Boardmail is installed.
    raise SystemExit(main())
