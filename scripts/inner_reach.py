#!/usr/bin/env python3
"""Count the tests that reach inside the package.

    python scripts/inner_reach.py            the count per test file
    python scripts/inner_reach.py --list     every counted test with its reasons
    python scripts/inner_reach.py --update   store a count that went down

Each row is one test file: its tests, how many of them reach inside, how many
replace a package name (rules 1 and 2 below) and how many use a Store write
path (rule 3). A test can do both.

The stored count is tests/inner_reach.txt: the counted tests by name, and their
number. tests/test_inner_reach.py fails when the tests that reach inside are
not exactly the stored ones. So the stored number always follows the count
down, and one counted test cannot be traded for another. --update only drops
the tests that stopped reaching inside: a new test never reaches inside. When
a counted test is renamed or moved, edit its stored line by hand.

The rule
--------

A test is a test method of a TestCase class in tests/test_*.py, counted once
for every class that runs it. It reaches inside when the code it runs on the
test side does one of three things.

1. It patches a package name. That is patch("boardmail. ...") with any dotted
   path into the package, or patch.object, patch.dict or patch.multiple on a
   target that starts at the package. The clock counts when it is reached
   through a package path: patch.object(providers.time, "monotonic") and
   patch("boardmail.store.time.time") count, patch("time.monotonic") does not.

2. It replaces a package name without mock. That is an assignment, del,
   setattr or delattr on an attribute of something that starts at the package:
   providers.PAGE_SIZE = 1, Store.wait = fake, client.opener = fake for a
   client the package built. It is also an assignment to an item of an
   imported package name, providers.HOSTS["x"] = y, and a class that inherits
   from a package class and defines a method, because the package then runs
   the test's method in place of its own.

3. It uses a Store write path: initialize, save, save_collection, failure,
   set_paused, set_subscription, prepare_collection or mark, called or handed
   to something else to call; settings with an argument; connect with write
   or create. The receiver has to be a Store, as in Store(path).save(...) or
   self.store.mark(...).

Nothing else is counted. A test that calls any other package function, even
one that writes, is not counted by this rule.

"Starts at the package" and "is a Store" are decided by reading the source,
not by running it. A name starts at the package when it is imported from
boardmail, by an import statement or by name through importlib or sys.modules.
So does a string that spells a dotted path into the package, as far as its
fixed start shows. A name is a Store when it is the Store class of the
package. Both carry over to what the source builds from such a name: an
attribute, an item, a call, a name assigned from one, a loop or with target, a
class that inherits from one, the result of a test-side function that returns
one, and a parameter of a test-side function that some caller fills with one.
An attribute of self carries over to the family of its class: the test-side
bases, the test-side classes that inherit from it, and their bases. Batch from
boardmail.adapters is the one exception: building and filling a Batch is
adapter interface v1, an allowed seam.

"The code it runs on the test side" is the test method, the fixture methods of
its class (setUp and its relatives), the decorators and class-level statements
of its class and of the test-side bases, the module-level statements of its
file and of the test-side files that file imports, and every function, class
or constant under tests/ or examples/ that this code refers to by name,
followed to any depth. self.name and super().name mean what they mean in the
class that runs the test. A plain name = value statement at module or class
level counts only for the tests that refer to the name, and the block under
if __name__ == "__main__" does not count. Python source kept in a string, for
a child process or an adapter file, counts as code of the place that holds the
string. A string is taken for source when it mentions the package, parses as
Python and imports something.

The count reads names, so it can be wrong in known ways. It counts a helper
that is referred to but never called. It follows names without regard to
order, branch or caller, so one name used for two things is taken for both in
every test that uses it. It misses a package object that reaches test code by
a route it does not follow: an attribute of some other object, an argument the
package passes to a test callback, an argument of a helper that is called
through a variable or on some other object, a name worked out while the test
runs, a name brought in by a star import, source handed to exec or eval
without an import of its own, a test-side file that is run or loaded by its
path. It misses a package container changed in place by a method call or
through another name, as in providers.HOSTS.update(...) or
hosts = providers.HOSTS; hosts["x"] = y. The files under
tests/fixtures/inner_reach hold an example for each case of the rule and for
some of these limits, and tests/test_inner_reach.py checks them.
"""
import argparse
import ast
from itertools import takewhile
from pathlib import Path
import sys
import textwrap
import warnings

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
STORED = TESTS / 'inner_reach.txt'
SIDE = {'': TESTS, 'examples.': ROOT / 'examples'}     # import prefix -> folder of test-side code
PACKAGE = 'boardmail'
INTERFACE = ('boardmail.adapters.Batch',)
WRITES = ('initialize', 'save', 'save_collection', 'failure', 'set_paused',
          'set_subscription', 'prepare_collection', 'mark')
FIXTURES = ('setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'asyncSetUp', 'asyncTearDown',
            'setUpModule', 'tearDownModule')
WRAPPERS = ('getattr', 'vars', 'copy', 'deepcopy', 'closing', 'iter', 'next', 'list', 'tuple',
            'sorted', 'reversed', 'enumerate', 'zip')
IMPORTERS = ('import_module', '__import__')
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
CARRIERS = (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.For, ast.AsyncFor, ast.comprehension, ast.With,
            ast.AsyncWith, ast.ClassDef, ast.Return, ast.Yield, ast.YieldFrom, ast.Call)
KINDS = ('package', 'store')
UNREADABLE = (SyntaxError, ValueError, RecursionError, MemoryError)     # what Python raises for no source of its own


def dotted(node):
    """a.b.c for a plain chain of names, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return '.'.join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def inside(name):
    return name == PACKAGE or name.startswith(PACKAGE + '.')


def text(node):
    """The string a node spells out, if it is one."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def spoken(node):
    """An expression the way a reason quotes it."""
    try:
        return text(node) or ast.unparse(node)
    except RecursionError:
        return dotted(node) or type(node).__name__


def own(node):
    """self.x or cls.x."""
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in ('self', 'cls'))


def flat(target):
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from flat(element)
    else:
        yield target.value if isinstance(target, ast.Starred) else target


def defined(statement):
    """The names a module-level or class-level statement defines."""
    if isinstance(statement, (*FUNCTIONS, ast.ClassDef)):
        return (statement.name,)
    if isinstance(statement, ast.Assign):
        return tuple(target.id for target in statement.targets if isinstance(target, ast.Name))
    return ()


def launch_only(statement):
    """Whether a statement is the block that runs only when its file is launched as a script."""
    if not isinstance(statement, ast.If) or not isinstance(statement.test, ast.Compare):
        return False
    sides = [statement.test.left, *statement.test.comparators]
    return '__name__' in map(dotted, sides) and '__main__' in map(text, sides)


def find(chain, name):
    """The first statement that defines a method or class attribute in a chain of (source, class)."""
    for source, node in chain:
        for statement in node.body:
            if name in defined(statement):
                return source, statement
    return None


def script(source):
    """The tree of Python source kept in a string, when it is source that imports something."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        try:
            tree = ast.parse(textwrap.dedent(source))
        except UNREADABLE:
            return None
    return tree if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)) else None


class Source:
    """One module of test-side code: a file, or Python source kept in a string."""

    def __init__(self, name, tree, registry, package=''):
        self.name, self.tree, self.registry = name, tree, registry
        self.imports = {}      # local name -> dotted path it was imported as
        self.loads = {}        # import statement -> dotted paths of the modules it may load
        self.names = {}        # module-level name -> the statement that defines it
        self.loose = []        # module-level statements that define no name: what importing the module runs
        self.place = {}        # node -> (outermost function, innermost class, innermost function) around it
        self.nested = {}       # (outermost function, name) -> function defined inside it
        self.classes = []
        self.heirs = {}        # class -> (source, class) for every test-side class that inherits from it
        self.kinds = {}        # (scope, name) -> kinds the name carries; a scope is a module, class or function
        self.scripts = {}      # string node -> Source of the Python source it holds
        self.carriers = []     # nodes that can give a name a kind
        self.lineages = {}
        self.families = {}
        self.index()
        for node in ast.walk(tree):
            if isinstance(node, CARRIERS):
                self.carriers.append(node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                base, paths = '', []
                if isinstance(node, ast.ImportFrom):
                    above = package.split('.') if package and node.level else []
                    base = '.'.join([*above[:max(0, len(above) - node.level + 1)], *filter(None, [node.module])])
                    paths.append(base)
                for alias in node.names:
                    whole = isinstance(node, ast.ImportFrom) or alias.asname
                    path = f'{base}.{alias.name}'.lstrip('.') if isinstance(node, ast.ImportFrom) else alias.name
                    self.imports[alias.asname or alias.name.split('.')[0]] = path if whole else path.split('.')[0]
                    paths.append(path)
                self.loads[node] = [path[:end] for path in paths
                                    for end in (*(at for at, sign in enumerate(path) if sign == '.'), len(path))]
            elif text(node) and PACKAGE in node.value:
                held = script(node.value)
                if held is not None:
                    self.scripts[node] = Source(f'{name}:{node.lineno}', held, registry)
        for statement in tree.body:
            for found in defined(statement):
                self.names[found] = statement
            if not defined(statement) and not launch_only(statement):
                self.loose.append(statement)

    def index(self):
        pending = [(self.tree, None, None, None)]
        while pending:
            node, function, cls, innermost = pending.pop()
            for child in ast.iter_child_nodes(node):
                self.place[child] = (function, cls, innermost)
                if isinstance(child, ast.ClassDef):
                    self.classes.append(child)
                    pending.append((child, function, child, innermost))
                elif isinstance(child, (*FUNCTIONS, ast.Lambda)):
                    if function is not None and not isinstance(child, ast.Lambda):
                        self.nested[(function, child.name)] = child
                    pending.append((child, function or child, cls, child))
                else:
                    pending.append((child, function, cls, innermost))

    def every(self):
        yield self
        for held in self.scripts.values():
            yield from held.every()

    # What a name refers to.

    def origin(self, node):
        """The dotted path an expression was imported as, when the source spells it out."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        head = None
        if isinstance(node, ast.Name):
            head = self.imports.get(node.id)
        elif isinstance(node, ast.Call) and node.args:
            importer = (dotted(node.func) or '').rpartition('.')[2]
            head = text(node.args[0]) if importer in IMPORTERS else None
            if head and importer == '__import__' and len(node.args) < 4 and not node.keywords:
                head = head.partition('.')[0]
        elif isinstance(node, ast.Subscript) and dotted(node.value) == 'sys.modules':
            head = text(node.slice)
        return '.'.join([head, *reversed(parts)]) if head else None

    def lookup(self, name):
        """The (source, statement) on the test side that a name or chain of names refers to, if any."""
        source, seen = self, set()
        while name and (source, name) not in seen:
            seen.add((source, name))
            head, _, rest = name.partition('.')
            if not rest and head in source.names:
                return source, source.names[head]
            if head not in source.imports:
                return None
            path = source.imports[head] + ('.' + rest if rest else '')
            module, _, member = path.rpartition('.')
            other = source.registry.get(module)
            if other is not None and member in other.names:
                return other, other.names[member]
            if other is None or member not in other.imports:
                other = source.registry.get(path)
                return (other, other.tree) if other is not None else None
            source, name = other, member
        return None

    def lineage(self, cls):
        """A class and its bases on the test side, in the order Python looks a name up in them."""
        pending, opened = [(self, cls)], set()
        while pending:
            source, node = pending[-1]
            if node in source.lineages:
                pending.pop()
                continue
            opened.add(node)    # A class that is being worked out is not its own base.
            bases = [found for found in map(source.lookup, map(dotted, node.bases))
                     if found is not None and isinstance(found[1], ast.ClassDef)
                     and (found[1] in found[0].lineages or found[1] not in opened)]
            waiting = [base for base in bases if base[1] not in base[0].lineages]
            if waiting:
                pending.extend(waiting)
                continue
            rows, merged = [*(list(base[0].lineages[base[1]]) for base in bases), bases], [(source, node)]
            while any(rows):
                rows = [row for row in rows if row]
                head = next((row[0] for row in rows
                             if all(row[0][1] is not other[1] for rest in rows for other in rest[1:])), rows[0][0])
                merged.append(head)
                rows = [[relative for relative in row if relative[1] is not head[1]] for row in rows]
            source.lineages[node] = merged
            pending.pop()
        return self.lineages[cls]

    def family(self, cls):
        """A class with its bases on the test side, and the classes that inherit from it with theirs."""
        if cls not in self.families:
            self.families[cls] = [relative for source, heir in ((self, cls), *self.heirs.get(cls, ()))
                                  for relative in source.lineage(heir)]
        return self.families[cls]

    def member(self, cls, name):
        """The statement that defines a method or class attribute, searched through test-side bases."""
        return find(self.lineage(cls), name)

    def held(self, node):
        """How far up from the class around it an attribute is looked for: 0 for self.x or cls.x,
        1 for super().x, None for anything else."""
        if not isinstance(node, ast.Attribute) or self.place[node][1] is None:
            return None
        if own(node):
            return 0
        return 1 if isinstance(node.value, ast.Call) and dotted(node.value.func) == 'super' else None

    def meanings(self, node, up):
        """Every definition that self.x or super().x stands for in some class that runs the code."""
        cls, found = self.place[node][1], []
        for source, heir in ((self, cls), *self.heirs.get(cls, ())):
            chain = source.lineage(heir)
            at = next(index for index, (_, other) in enumerate(chain) if other is cls)
            target = find(chain[at + 1:] if up else chain, node.attr)
            if target is not None and all(target[1] is not other for _, other in found):
                found.append(target)
        return found

    # Which names start at the package, and which are a Store.

    def carries(self, scope, name, kind):
        return kind in self.kinds.get((scope, name), ())

    def add(self, scope, name, kind):
        held = self.kinds.setdefault((scope, name), set())
        if kind in held:
            return False
        held.add(kind)
        return True

    def rooted(self, node, kind):
        """Whether the source shows an expression to start at the package, or to be a Store."""
        pending = [node]
        while pending:
            node = pending.pop()
            origin = self.origin(node)
            if origin in INTERFACE:
                continue
            if origin is not None and inside(origin) and (kind == 'package' or origin.endswith('.Store')):
                return True
            function, cls, _ = self.place[node]
            if isinstance(node, (ast.Name, ast.Attribute)):
                found = self.lookup(dotted(node))
                if found is not None and any(found[0].carries(found[0].tree, name, kind) for name in defined(found[1])):
                    return True
            if isinstance(node, ast.Name):
                scope = function if function is not None else cls
                if scope is not None and self.carries(scope, node.id, kind) or self.carries(self.tree, node.id, kind):
                    return True
            elif isinstance(node, ast.Attribute):
                if own(node) and cls is not None:
                    if any(source.carries(relative, node.attr, kind) for source, relative in self.family(cls)):
                        return True
                elif kind == 'store' and node.attr == 'Store' and self.rooted(node.value, 'package'):
                    return True
                else:
                    pending.append(node.value)
            elif isinstance(node, ast.Call):
                name = (dotted(node.func) or '').rpartition('.')[2]
                if name in IMPORTERS:
                    pending += node.args[:1] if kind == 'package' else []
                else:
                    pending += [node.func, *(node.args if name in WRAPPERS else [])]
            elif isinstance(node, ast.Subscript):
                if dotted(node.value) != 'sys.modules':
                    pending.append(node.value)
                elif kind == 'package':
                    pending.append(node.slice)
            elif isinstance(node, ast.Constant):
                if kind == 'package' and inside((text(node) or '').rstrip('.')):
                    return True
            elif isinstance(node, (ast.Await, ast.Starred, ast.NamedExpr, ast.FormattedValue, ast.DictComp)):
                pending.append(node.value)
            elif isinstance(node, ast.BinOp):
                pending.append(node.left)
            elif isinstance(node, ast.JoinedStr):
                pending += node.values[:1]
            elif isinstance(node, ast.IfExp):
                pending += [node.body, node.orelse]
            elif isinstance(node, (ast.BoolOp, ast.Dict)):
                pending += node.values
            elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                pending += node.elts
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                pending.append(node.elt)
            elif isinstance(node, ast.Lambda):
                pending.append(node.body)
        return False

    def bind(self, target, kind):
        """Give an assignment target a kind. True when that is news."""
        function, cls, _ = self.place[target]
        changed = False
        for item in flat(target):
            if isinstance(item, ast.Name):
                changed |= self.add(function if function is not None else cls or self.tree, item.id, kind)
            elif own(item) and cls is not None:
                changed |= self.add(cls, item.attr, kind)
        return changed

    def spread(self):
        """One pass that carries kinds from values to the names they are given. True when a name gained one."""
        changed = False
        for node in self.carriers:
            for kind in KINDS:
                changed |= self.carry(node, kind)
        return changed

    def carry(self, node, kind):
        function, cls, innermost = self.place[node]
        changed = False
        if isinstance(node, ast.Assign):
            for target in node.targets:
                pairs = [(target, node.value)]
                if (isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List))
                        and len(target.elts) == len(node.value.elts)):
                    pairs = zip(target.elts, node.value.elts)
                for left, right in pairs:
                    if self.rooted(right, kind):
                        changed |= self.bind(left, kind)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            if node.value is not None and self.rooted(node.value, kind):
                changed |= self.bind(node.target, kind)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            if self.rooted(node.iter, kind):
                changed |= self.bind(node.target, kind)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and self.rooted(item.context_expr, kind):
                    changed |= self.bind(item.optional_vars, kind)
        elif isinstance(node, ast.ClassDef):
            if any(self.rooted(base, kind) for base in node.bases):
                changed |= self.add(function if function is not None else cls or self.tree, node.name, kind)
        elif isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)):
            # A function that returns such a value is taken for one where it is called.
            if isinstance(innermost, FUNCTIONS) and node.value is not None and self.rooted(node.value, kind):
                if innermost is function:
                    changed |= self.add(self.place[function][1] or self.tree, function.name, kind)
                else:
                    changed |= self.add(function, innermost.name, kind)
        elif isinstance(node, ast.Call):
            # A parameter of a test-side function carries what some caller passes for it. Where a
            # star hides which argument fills which parameter, every pairing it allows is taken.
            plain = list(takewhile(lambda argument: not isinstance(argument, ast.Starred), node.args))
            for source, definition, taken, scope in self.callees(node):
                arguments = definition.args
                ordered = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
                positional = ordered[taken:]
                named = [argument.arg for argument in (*arguments.args, *arguments.kwonlyargs)
                         if argument.arg not in ordered[:taken]]
                passed = [*zip(positional, plain),
                          *((key.arg, key.value) for key in node.keywords if key.arg in named)]
                takers = positional[len(plain):] + ([arguments.vararg.arg] if arguments.vararg else [])
                passed += [(name, value) for name in takers
                           for value in (*plain[len(positional):], *node.args[len(plain):])]
                passed += [(name, key.value) for name in named for key in node.keywords if key.arg is None]
                if arguments.kwarg:
                    passed += [(arguments.kwarg.arg, key.value) for key in node.keywords if key.arg not in named]
                for name, value in passed:
                    if self.rooted(value, kind):
                        changed |= source.add(scope, name, kind)
        return changed

    def callees(self, call):
        """(source, function, parameters the call does not fill, scope of its names) for every
        test-side function a call may run."""
        function, cls, _ = self.place[call]
        target = call.func
        if isinstance(target, ast.Name) and (function, target.id) in self.nested:
            return [(self, self.nested[(function, target.id)], 0, function)]
        up = self.held(target)
        if up is not None:
            found, taken = self.meanings(target, up), 1
        else:
            found, taken = [self.lookup(dotted(target))], 0
            if found[0] is not None and isinstance(found[0][1], ast.ClassDef):
                found, taken = [found[0][0].member(found[0][1], '__init__')], 1
        return [(source, definition,
                 0 if any(dotted(decorator) == 'staticmethod' for decorator in definition.decorator_list) else taken,
                 definition)
                for source, definition in filter(None, found) if isinstance(definition, FUNCTIONS)]

    # What one piece of code does, and what it refers to.

    def patch_form(self, call):
        """'' for patch(...), 'object', 'dict' or 'multiple' for those forms, None for any other call."""
        path = self.origin(call.func) or ''
        if path == 'unittest.mock.patch':
            return ''
        head, _, form = path.rpartition('.')
        return form if head == 'unittest.mock.patch' and form in ('object', 'dict', 'multiple') else None

    def reasons(self, node):
        """What a piece of code does that reaches inside, as short texts."""
        found = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                form = self.patch_form(sub)
                first = sub.args[0] if sub.args else next(
                    (key.value for key in sub.keywords if key.arg in ('target', 'in_dict')), None)
                plain = dotted(sub.func) in ('setattr', 'delattr')
                if first is not None and (form is not None or plain) and self.rooted(first, 'package'):
                    named = text(sub.args[1]) if len(sub.args) > 1 and (form == 'object' or plain) else None
                    found.add(('assign ' if plain else 'patch ') + spoken(first) + (f'.{named}' if named else ''))
                elif isinstance(sub.func, ast.Attribute) and self.rooted(sub.func.value, 'store') and (
                        sub.func.attr == 'settings' and (sub.args or sub.keywords)
                        or sub.func.attr == 'connect' and any(
                            key.arg in ('write', 'create', None)
                            and not (isinstance(key.value, ast.Constant) and not key.value.value)
                            for key in sub.keywords)):
                    found.add('store ' + sub.func.attr)
            elif isinstance(sub, ast.Attribute):
                if not isinstance(sub.ctx, ast.Load):
                    if self.rooted(sub.value, 'package'):
                        found.add('assign ' + spoken(sub))
                elif sub.attr in WRITES and self.rooted(sub.value, 'store'):
                    found.add('store ' + sub.attr)
            elif isinstance(sub, ast.Subscript):
                if (not isinstance(sub.ctx, ast.Load) and inside(self.origin(sub.value) or '')
                        and self.origin(sub.value) not in INTERFACE):
                    found.add('assign ' + spoken(sub))
            elif isinstance(sub, ast.ClassDef):
                if (any(self.rooted(base, 'package') for base in sub.bases)
                        and any(isinstance(statement, FUNCTIONS) for statement in sub.body)):
                    found.add('subclass ' + sub.name)
        return found

    def helpers(self, node, running):
        """The test-side code a piece of code refers to by name, loads by an import or holds as a script,
        and whether the answer depends on running, the (source, class) that runs the test."""
        found, varies = [], False
        for sub in ast.walk(node):
            target, up = None, self.held(sub)
            if sub in self.scripts:
                target = self.scripts[sub], self.scripts[sub].tree
            elif sub in self.loads:
                found += [(self.registry[name], statement) for name in self.loads[sub] if name in self.registry
                          for statement in self.registry[name].loose]
            elif up is not None:
                varies = True
                cls, chain = self.place[sub][1], running[0].lineage(running[1])
                at = next((index for index, (_, other) in enumerate(chain) if other is cls), None)
                if at is None:
                    chain, at = self.lineage(cls), 0
                target = find(chain[at + 1:] if up else chain, sub.attr)
            elif isinstance(sub, ast.Attribute) or isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                target = self.lookup(dotted(sub))
                if target is not None and isinstance(target[1], ast.Module):
                    target = None
            if target is not None:
                found.append(target)
        return found, varies


def load(folders):
    """Every module of test-side code by the name it is imported under, with kinds carried through."""
    registry = {}
    for prefix, folder in folders.items():
        for path in sorted(folder.rglob('*.py')):
            parts = path.relative_to(folder).with_suffix('').parts
            package = (prefix + '.'.join(parts[:-1])).rstrip('.')
            name = package if parts[-1] == '__init__' else (prefix + '.'.join(parts))
            try:
                tree = ast.parse(path.read_bytes())
            except UNREADABLE:
                continue    # No test runs a file that Python cannot read.
            if name:
                registry[name] = Source(name, tree, registry, package)
    sources = [held for source in registry.values() for held in source.every()]
    for source in sources:
        for cls in source.classes:
            for owner, base in source.lineage(cls)[1:]:
                owner.heirs.setdefault(base, []).append((source, cls))
    while any([source.spread() for source in sources]):
        pass
    return registry


def analyse(folders=None, modules=None):
    """{file: {Class.test: sorted reasons}} for every test, whether it reaches inside or not."""
    registry = load(folders or SIDE)
    known = {}

    def reach(seeds, running):
        found, pending, seen = set(), list(seeds), set()
        while pending:
            source, node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            entry = known.get(node) or known.get((node, running[1]))
            if entry is None:
                helpers, varies = source.helpers(node, running)
                entry = known[(node, running[1]) if varies else node] = (source.reasons(node), helpers)
            found |= entry[0]
            pending.extend(entry[1])
        return found

    result = {}
    for name in sorted(modules or (name for name in registry if name.startswith('test_') and '.' not in name)):
        source = registry[name]
        tests = result.setdefault(name + '.py', {})
        module = [(source, statement) for statement in source.loose]
        module += [(source, source.names[fixture]) for fixture in FIXTURES if fixture in source.names]
        for cls in source.tree.body:
            if not isinstance(cls, ast.ClassDef) or not any(
                    (dotted(base) or '').endswith('TestCase')
                    for _, node in source.lineage(cls) for base in node.bases):
                continue
            chain = source.lineage(cls)
            shared = module + [(owner, statement) for owner, node in chain for statement in node.body
                               if not defined(statement) and not isinstance(statement, ast.Pass)]
            shared += [found for found in (source.member(cls, fixture) for fixture in FIXTURES) if found is not None]
            methods = {}
            for owner, node in chain:
                for statement in node.body:
                    if isinstance(statement, FUNCTIONS) and statement.name.startswith('test'):
                        methods.setdefault(statement.name, (owner, node, statement))
            for test, (owner, home, statement) in sorted(methods.items()):
                # A class decorator wraps the tests its class has: its own and the ones it inherits.
                wrapped = [(other, decorator) for other, node in chain
                           if any(home is relative for _, relative in other.lineage(node))
                           for decorator in node.decorator_list]
                tests[f'{cls.name}.{test}'] = sorted(reach([*shared, *wrapped, (owner, statement)], (source, cls)))
    return result


def counted(result):
    """The tests that reach inside, each as 'file Class.test'."""
    return [f'{name} {test}' for name, tests in result.items() for test, found in tests.items() if found]


def entry(line):
    """What a stored line holds apart from its note: 'file Class.test', 'total N' or nothing."""
    return ' '.join(line.partition('#')[0].split())


def read_stored(lines=None):
    """The stored tests, each as 'file Class.test', and their stored number, from a well-formed list."""
    if lines is None:
        lines = STORED.read_text(encoding='utf-8').splitlines()
    names, total = [], None
    for number, line in enumerate(lines, 1):
        fields = entry(line).split()
        if fields and (total is not None or len(fields) != 2 or fields[0] == 'total' and not fields[1].isdecimal()):
            raise ValueError(f'{STORED.name} line {number}: expected "file Class.test" lines, then one "total N"')
        if fields[:1] == ['total']:
            total = int(fields[1])
        elif fields:
            names.append(' '.join(fields))
    if total is None:
        raise ValueError(f'{STORED.name}: the last line has to be "total N"')
    return names, total


def kept(lines, current):
    """The stored lines without the tests that are not in current and without repeats, then the number that stay."""
    rows, names, note = [], set(), ''
    for line in lines:
        name = entry(line)
        if name.startswith('total '):
            note = line[line.index('#'):] if '#' in line else ''
        elif not name or name in current and name not in names:
            rows.append(line)
            names.add(name)
    return [*rows, ' '.join(filter(None, [f'total {len(names - {""})}', note]))]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Count the tests that reach inside the package. The rule is at the top of this file.')
    parser.add_argument('--list', action='store_true', help='Also print every counted test with its reasons')
    parser.add_argument('--update', action='store_true',
                        help='Drop the tests that stopped reaching inside from tests/inner_reach.txt')
    args = parser.parse_args(argv)
    result = analyse()
    if args.list:
        for name, tests in result.items():
            for test, found in tests.items():
                if found:
                    print(f'{name} {test}: ' + '; '.join(found))
    print(f"{'file':38}{'tests':>6}{'inside':>8}{'replaces':>10}{'writes':>8}")
    totals = [0, 0, 0, 0]
    for name, tests in result.items():
        row = [len(tests), sum(1 for found in tests.values() if found),
               sum(1 for found in tests.values() if any(not reason.startswith('store ') for reason in found)),
               sum(1 for found in tests.values() if any(reason.startswith('store ') for reason in found))]
        totals = [total + number for total, number in zip(totals, row)]
        print(f'{name:38}{row[0]:>6}{row[1]:>8}{row[2]:>10}{row[3]:>8}')
    print(f"{'total':38}{totals[0]:>6}{totals[1]:>8}{totals[2]:>10}{totals[3]:>8}")
    if args.update:
        if not STORED.exists():
            print(f'{STORED} is missing. Restore it; --update only drops lines.', file=sys.stderr)
            return 1
        current, lines = counted(result), STORED.read_text(encoding='utf-8').splitlines()
        new = sorted(set(current) - set(map(entry, lines)))
        if new:
            print('\n'.join(new), file=sys.stderr)
            print('Not stored: these tests reach inside and are not in tests/inner_reach.txt. A new test never '
                  'reaches inside; --list shows why a test is counted. For a counted test that was only renamed '
                  'or moved, edit its line in tests/inner_reach.txt by hand.', file=sys.stderr)
            return 1
        STORED.write_text('\n'.join(kept(lines, current)) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
