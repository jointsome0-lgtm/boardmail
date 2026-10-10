#!/usr/bin/env python3
"""Say how much of the package is comments and docstrings, and fail where that is more than the limit.

    python scripts/comment_share.py           the share of the package
    python scripts/comment_share.py --files   the share of each module as well

Run it from the top of the source tree.

The rule
--------

The share is counted in characters, white space aside, over the modules of
boardmail/. A character stands in a comment, in the docstring of a module, a
class or a function, or in the code. So the count is the same however a text is
broken into lines, and a comment after a statement counts like one on a line
of its own.

What a comment or a docstring is for is in CONTRIBUTING.md: it says what the
code cannot show. LIMIT is no goal. It is the share that the package had when
its comments were last read through, rounded up, so that the share does not
grow unnoticed. A change that needs more raises LIMIT in the same change, where
the reason can be asked for.
"""
import argparse
import ast
import io
from pathlib import Path
import sys
import tokenize

LIMIT = 0.18


def size(text):
    return len(''.join(text.split()))


def counted(path):
    """(the characters of a file that stand in a comment or a docstring, all of its characters)"""
    text = path.read_text(encoding='utf-8')
    said = sum(size(token.string) for token in tokenize.generate_tokens(io.StringIO(text).readline)
               if token.type == tokenize.COMMENT)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and ast.get_docstring(node, clean=False) is not None:
            said += size(ast.get_source_segment(text, node.body[0]))
    return said, size(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--files', action='store_true', help='the share of each module as well')
    args = parser.parse_args()
    rows = [(path, *counted(path)) for path in sorted(Path('boardmail').rglob('*.py'))]
    if not rows:
        parser.error('no boardmail/ here: run it from the top of the source tree')
    if args.files:
        for path, said, whole in rows:
            print(f'{path}: {said} of {whole} characters, {100 * said / max(whole, 1):.1f}%')
    said, whole = sum(row[1] for row in rows), sum(row[2] for row in rows)
    print(f'boardmail: {said} of {whole} characters are comments and docstrings, {100 * said / whole:.1f}%. '
          f'The limit is {100 * LIMIT:.0f}%.')
    if said > LIMIT * whole:
        print('That is more than the limit. Take out what repeats the code, or raise LIMIT in this script where '
              'the new text says what the code cannot show.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
