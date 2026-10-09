#!/usr/bin/env python3
"""Say which lines of a file the test suite runs, with the standard library alone.

    python scripts/line_coverage.py boardmail/adapter_botnet.py boardmail/adapter_colony.py

Run it from the top of the source tree. It runs the full test discovery in this
process and prints one row for each named file: its lines, how many of them
ran, and the ones that did not.

The rule
--------

A line of a file is a line on which a statement starts. A docstring is no
statement here, and neither is global or nonlocal: they run nothing. Two
statements on one line are one line.

A line ran when the interpreter says that it ran while the tests were running:
at import, or in a test. What a test runs in another process does not count, so
a file that is only reached through `python -m boardmail` shows fewer lines than
ran.

A line that ran is not a line that was checked. This number says what the tests
cannot have checked.
"""
import argparse
import ast
from pathlib import Path
import sys
import threading
import unittest


def lines(path):
    """The lines of a file on which a statement starts."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if not isinstance(node, ast.stmt) or isinstance(node, (ast.Global, ast.Nonlocal)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        found.add(node.lineno)
    return found


def watch(paths):
    """Start to note each line of these files that runs, in this thread and in each one that starts later.
    Gives the lines by file. They fill while the process runs."""
    ran = {str(path): set() for path in paths}

    def inside(frame, event, arg):
        if event == 'line':
            ran[frame.f_code.co_filename].add(frame.f_lineno)
        return inside

    def entered(frame, event, arg):
        # Only a frame of one of the files is followed line by line, so the tests run at nearly their own speed.
        return inside if frame.f_code.co_filename in ran else None

    threading.settrace(entered)
    sys.settrace(entered)
    return ran


def spans(numbers):
    """Line numbers as text, with each run of neighbours as first-last."""
    parts, numbers = [], sorted(numbers)
    for number in numbers:
        if parts and parts[-1][1] == number - 1:
            parts[-1][1] = number
        else:
            parts.append([number, number])
    return ', '.join(str(first) if first == last else f'{first}-{last}' for first, last in parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('files', nargs='+', type=Path, help='the files to report on')
    parser.add_argument('--pattern', default='test*.py', help='the test files to run (default: all)')
    args = parser.parse_args()
    paths = [path.resolve() for path in args.files]
    ran = watch(paths)
    # As `python -m unittest discover -s tests` from here: the tests import the package and the examples by name,
    # and nothing from the folder of this script.
    sys.path[0] = str(Path.cwd())
    done = unittest.main(module=None, argv=['line_coverage', 'discover', '-s', 'tests', '-p', args.pattern], exit=False)
    sys.settrace(None)
    threading.settrace(None)
    print()
    for given, path in zip(args.files, paths):
        all_lines = lines(path)
        missed = all_lines - ran[str(path)]
        print(f'{given}: {len(all_lines) - len(missed)} of {len(all_lines)} lines ran, '
              f'{100 * (len(all_lines) - len(missed)) / len(all_lines):.1f}%. '
              + ('Not run: ' + spans(missed) if missed else 'Every line ran.'))
    return 0 if done.result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
