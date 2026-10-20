"""Evidence-based notebook API migration analysis; no package code is executed.

Separate, new implementation. Historical releases are evidence snapshots, not a
claim about the notebook's original environment. EXISTS is source-path presence,
not a promise that a call's signature, dependencies, or behavior is compatible.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import sys
import tempfile
import tokenize
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import nbformat
from packaging.requirements import Requirement
from packaging.version import Version, InvalidVersion


PACKAGES = ('langchain', 'langchain_classic', 'langchain_community', 'langchain_core',
            'langchain_openai', 'langchain_text_splitters')
SCHEMA_VERSION = '1.1'
BASIS_OFFICIAL = 'official_export_relationship'
BASIS_SPLIT = 'package_split_identical_definition'


def unique(items):
    return list({json.dumps(item, sort_keys=True): item for item in items}.values())


@dataclass
class Distribution:
    package: str
    version: str
    root: Path
    url: str = ''
    sha256: str = ''
    released: str = ''
    requirements: list = field(default_factory=list)

    def record(self):
        return dict(package=self.package, version=self.version, source_url=self.url,
                    sha256=self.sha256, released=self.released)


class PyPIStore:
    """Fetch authenticated-by-PyPI-hash source wheels; cache only complete downloads."""
    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.metadata_cache = {}

    def metadata(self, package, version=None):
        key = (package, version)
        if key not in self.metadata_cache:
            project = package.replace('_', '-')
            suffix = f'/{version}' if version else ''
            url = f'https://pypi.org/pypi/{project}{suffix}/json'
            request = urllib.request.Request(url, headers={'User-Agent': 'api-migration-analyzer/1'})
            with urllib.request.urlopen(request, timeout=45) as response:
                self.metadata_cache[key] = json.load(response)
        return self.metadata_cache[key]

    def versions_before(self, package, cutoff=None, constraints=()):
        metadata = self.metadata(package)
        result = []
        for number, files in metadata.get('releases', {}).items():
            try:
                version = Version(number)
            except InvalidVersion:
                continue
            usable = [f for f in files if not f.get('yanked') and
                      (not cutoff or f.get('upload_time_iso_8601', '') <= cutoff)]
            if (not version.is_prerelease and not version.is_devrelease and usable
                    and all(version in constraint for constraint in constraints)):
                result.append((version, number))
        return sorted(result, reverse=True)

    def historical_anchors(self, package, target, depth):
        target = Version(target)
        versions = [(v, n) for v, n in self.versions_before(package) if v < target]
        previous_major = next((n for v, n in versions if v.major < target.major), None)
        previous_minor = next((n for v, n in versions
                               if (v.major, v.minor) < (target.major, target.minor)), None)
        anchors = list(dict.fromkeys(n for n in (previous_major, previous_minor) if n))
        return anchors[:depth]

    def fetch(self, package, version=None):
        metadata = self.metadata(package, version)
        resolved = metadata['info']['version']
        files = metadata.get('urls', [])
        wheels = [f for f in files if f['packagetype'] == 'bdist_wheel' and not f.get('yanked')]
        if not wheels:
            raise RuntimeError(f'{package}=={resolved}: no source-inspectable wheel; sdist builds are not executed')
        wheel = sorted(wheels, key=lambda f: ('none-any.whl' not in f['filename'], f['filename']))[0]
        expected = wheel['digests']['sha256']
        destination = self.cache / f'{package}-{resolved}-{expected[:12]}'
        marker = destination / 'distribution.json'
        if not marker.exists():
            with urllib.request.urlopen(wheel['url'], timeout=90) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError('Distribution SHA256 mismatch')
            with tempfile.TemporaryDirectory(dir=self.cache) as temporary:
                staging = Path(temporary) / 'content'
                staging.mkdir()
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for member in archive.infolist():
                        path = (staging / member.filename).resolve()
                        if not path.is_relative_to(staging.resolve()):
                            raise RuntimeError('Unsafe wheel path')
                        archive.extract(member, staging)
                (staging / 'distribution.json').write_text(json.dumps({
                    'url': wheel['url'], 'sha256': expected, 'version': resolved}), encoding='utf-8')
                try:
                    staging.rename(destination)
                except FileExistsError:
                    if not marker.exists():
                        raise
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved['sha256'] != expected:
            raise RuntimeError('Cached artifact metadata mismatch')
        root = destination / package
        if not root.is_dir():
            raise RuntimeError(f'Package directory missing: {package}')
        return Distribution(package, resolved, root, wheel['url'], expected,
                            wheel.get('upload_time_iso_8601', ''), metadata['info'].get('requires_dist') or [])


@dataclass
class Symbol:
    kind: str
    path: str
    evidence: list
    target: str | None = None
    bases: list = field(default_factory=list)
    members: dict = field(default_factory=dict)


@dataclass
class Module:
    path: str
    symbols: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    stars: list = field(default_factory=list)
    exports: list | None = None
    parsed: bool = True
    evidence: list = field(default_factory=list)


@dataclass
class Resolution:
    state: str
    path: str
    symbol: Symbol | None = None
    evidence: list = field(default_factory=list)
    reason: str = ''


IGNORED_DECORATORS = ('override', 'deprecated')
NORMALIZATION = ('package name langchain_classic->langchain; ignored: docstrings and attribute docstrings, type '
                 'annotations, typing.cast target type, @override/@deprecated decorators; compared: names, bases, '
                 'arguments, defaults, other decorators, statements and expressions in bodies')


class DefinitionNormalizer(ast.NodeTransformer):
    """Drop documentation-level modernization so only implementation structure is compared."""
    def strip_body(self, node):
        # Bare string statements are docstrings or attribute docstrings; they have no runtime effect.
        body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                                             and isinstance(n.value.value, str))]
        node.body = body or [ast.Pass()]
        node.decorator_list = [d for d in node.decorator_list
                               if (dotted(d.func if isinstance(d, ast.Call) else d, {}) or '').split('.')[-1]
                               not in IGNORED_DECORATORS]

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        self.strip_body(node)
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        self.strip_body(node)
        node.returns = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        node.annotation = None
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        node.annotation = ast.Constant(None)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        # typing.cast's first argument is type-level only (str vs 'str').
        if (dotted(node.func, {}) or '').split('.')[-1] == 'cast' and len(node.args) == 2:
            node.args[0] = ast.Constant(None)
        return node


def dotted(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = dotted(node.value, bindings)
        return f'{parent}.{node.attr}' if parent else None
    if isinstance(node, ast.Subscript):
        return dotted(node.value, bindings)
    return None


class SourceWorld:
    """On-demand static index, retaining file failures and unresolved constructs."""
    def __init__(self, store=None, pins=None, distributions=None, cutoff=None, label='comparison'):
        self.store, self.pins = store, pins or {}
        self.distributions = distributions or {}
        self.cutoff, self.label = cutoff, label
        self.modules, self.errors, self.resolutions, self.module_files = {}, {}, {}, {}
        self.selection = {}
        self.candidate_cache = {}
        self.source_texts = {}

    def ensure(self, package):
        if package in self.distributions:
            return self.distributions[package]
        if package in self.errors or package not in PACKAGES or self.store is None:
            return None
        try:
            pin = self.pins.get(package)
            if not pin and self.cutoff:
                constraints = []
                for distribution in self.distributions.values():
                    for spec in distribution.requirements:
                        requirement = Requirement(spec)
                        if requirement.name.replace('-', '_') == package:
                            if requirement.marker is None or requirement.marker.evaluate({'extra': ''}):
                                constraints.append(requirement.specifier)
                choices = self.store.versions_before(package, self.cutoff, constraints)
                if not choices:
                    raise RuntimeError('No release satisfies historical cutoff and declared constraints')
                pin = None
                for _, candidate in choices[:40]:
                    metadata = self.store.metadata(package, candidate)
                    requirements = metadata['info'].get('requires_dist') or []
                    if self.compatible_snapshot(package, candidate, requirements):
                        pin = candidate
                        break
                if pin is None:
                    raise RuntimeError('No compatible historical dependency snapshot in the bounded 40-release search')
                self.selection[package] = {'rule': 'latest available by evidence release time',
                                           'cutoff': self.cutoff, 'constraints': list(map(str, constraints)),
                                           'dependency_constraints_checked': True}
            self.distributions[package] = self.store.fetch(package, pin)
            return self.distributions[package]
        except Exception as error:
            self.errors[package] = str(error)
            return None

    def compatible_snapshot(self, package, version, requirements):
        """Reject optional legacy redirects to a dependency from an incompatible generation."""
        groups = {}
        specs = list(requirements)
        for distribution in self.distributions.values():
            specs.extend(distribution.requirements)
        selected = {p: d.version for p, d in self.distributions.items()}
        selected[package] = version
        for spec in specs:
            requirement = Requirement(spec)
            dependency = requirement.name.replace('-', '_')
            if dependency not in PACKAGES or (requirement.marker and not requirement.marker.evaluate({'extra': ''})):
                continue
            groups.setdefault(dependency, []).append(requirement.specifier)
        for dependency, constraints in groups.items():
            chosen = selected.get(dependency) or self.pins.get(dependency)
            if chosen:
                if not all(Version(chosen) in constraint for constraint in constraints):
                    return False
            elif not self.store.versions_before(dependency, self.cutoff, constraints):
                return False
        return True

    def module_file(self, name):
        parts = name.split('.')
        distribution = self.ensure(parts[0])
        if not distribution:
            return None
        if parts[0] not in self.module_files:
            files = {}
            for file in distribution.root.rglob('*.py'):
                relative = file.relative_to(distribution.root)
                suffix = relative.parent.parts if file.name == '__init__.py' else relative.with_suffix('').parts
                module_name = '.'.join((parts[0], *suffix))
                if module_name not in files or file.name == '__init__.py':
                    files[module_name] = file
            self.module_files[parts[0]] = files
        # Preserve Python identifier case even on a case-insensitive host filesystem.
        return self.module_files[parts[0]].get(name)

    def proof(self, distribution, file, node, kind, **extra):
        return dict(kind=kind, snapshot=self.label, package=distribution.package,
                    version=distribution.version, source_url=distribution.url,
                    artifact_sha256=distribution.sha256,
                    file=str(file.relative_to(distribution.root.parent)).replace('\\', '/'),
                    line=getattr(node, 'lineno', 1), end_line=getattr(node, 'end_lineno', 1), **extra)

    def module(self, name):
        if name in self.modules:
            return self.modules[name]
        file = self.module_file(name)
        if file is None:
            return None
        result = Module(name)
        self.modules[name] = result
        distribution = self.distributions[name.split('.')[0]]
        proof = lambda node, kind, **extra: self.proof(distribution, file, node, kind, **extra)
        try:
            if file not in self.source_texts:
                with tokenize.open(file) as stream:
                    self.source_texts[file] = stream.read()
            tree = ast.parse(self.source_texts[file])
        except (OSError, UnicodeError, SyntaxError) as error:
            result.parsed = False
            result.issues.append(proof(ast.Pass(), 'parse_error', message=str(error)))
            return result
        result.evidence = [proof(tree, 'module')]
        bindings, constants = {}, {}
        def bind(local, symbol):
            result.symbols[local] = symbol
            bindings[local] = symbol.target or symbol.path
        def literal(node):
            if isinstance(node, ast.Name):
                return constants.get(node.id)
            try:
                return ast.literal_eval(node)
            except (ValueError, TypeError):
                return None
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split('.')[0]
                    target = alias.name if alias.asname else local
                    bind(local, Symbol('alias', f'{name}.{local}',
                         [proof(node, 'import_alias', old_path=f'{name}.{local}', new_path=target)], target))
            elif isinstance(node, ast.ImportFrom):
                package = name if file.name == '__init__.py' else name.rpartition('.')[0]
                if node.level:
                    parts = package.split('.')
                    if node.level > len(parts):
                        result.issues.append(proof(node, 'unresolved_relative_import'))
                        continue
                    imported = '.'.join(parts[:len(parts) - node.level + 1] + ([node.module] if node.module else []))
                else:
                    imported = node.module or ''
                for alias in node.names:
                    if alias.name == '*':
                        result.stars.append((imported, proof(node, 'star_reexport')))
                        continue
                    local = alias.asname or alias.name
                    target = f'{imported}.{alias.name}'
                    bind(local, Symbol('alias', f'{name}.{local}',
                         [proof(node, 'reexport', old_path=f'{name}.{local}', new_path=target)], target))
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                path = f'{name}.{node.name}'
                kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
                symbol = Symbol(kind, path, [proof(node, 'definition')])
                if kind == 'class':
                    symbol.bases = [dotted(base, bindings) for base in node.bases]
                    symbol.bases = [base if base and ('.' in base or base in ('object',)) else f'{name}.{base}'
                                    for base in symbol.bases]
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            decorators = [dotted(d, bindings) for d in member.decorator_list]
                            member_kind = 'attribute' if 'property' in decorators else 'method'
                            symbol.members[member.name] = Symbol(member_kind, f'{path}.{member.name}',
                                                                [proof(member, 'member_definition')])
                        elif isinstance(member, (ast.Assign, ast.AnnAssign)) and member.value is not None:
                            targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                            for target in targets:
                                if isinstance(target, ast.Name):
                                    symbol.members[target.id] = Symbol('attribute', f'{path}.{target.id}',
                                                                       [proof(member, 'member_assignment')])
                        elif isinstance(member, (ast.If, ast.Try)):
                            result.issues.append(proof(member, 'conditional_members', owner=path))
                            names = Locals()
                            names.visit(member)
                            for local in names.names:
                                symbol.members[local] = Symbol('unknown', f'{path}.{local}',
                                                               [proof(member, 'conditional_member_binding')])
                bind(node.name, symbol)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name) or node.value is None:
                        continue
                    value = literal(node.value)
                    constants[target.id] = value
                    path = f'{name}.{target.id}'
                    destination = dotted(node.value, bindings) if isinstance(node.value, (ast.Name, ast.Attribute)) else None
                    if destination:
                        if '.' not in destination:
                            destination = f'{name}.{destination}'
                        bind(target.id, Symbol('alias', path,
                             [proof(node, 'assignment_alias', old_path=path, new_path=destination)], destination))
                    else:
                        bind(target.id, Symbol('attribute', path, [proof(node, 'assignment')]))
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                # TYPE_CHECKING imports are not runtime exports and cannot establish presence.
                if not (isinstance(node, ast.If) and dotted(node.test, bindings) in ('typing.TYPE_CHECKING', 'TYPE_CHECKING')):
                    result.issues.append(proof(node, 'conditional_exports'))
                    names = Locals()
                    names.visit(node)
                    for local in names.names:
                        bind(local, Symbol('unknown', f'{name}.{local}', [proof(node, 'conditional_binding')]))
                        constants.pop(local, None)
        exports = constants.get('__all__')
        if isinstance(exports, (list, tuple)) and all(isinstance(x, str) for x in exports):
            result.exports = list(exports)
        forwarder = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '__getattr__'), None)
        recognized = False
        if forwarder and len(forwarder.args.args) == 1:
            parameter = forwarder.args.args[0].arg
            body = [n for n in forwarder.body if not (isinstance(n, ast.Expr) and
                    isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))]
            # LangChain's official create_importer protocol, including documented deprecations.
            for node in tree.body:
                if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                    continue
                factory = dotted(node.value.func, bindings)
                if factory not in ('langchain._api.create_importer', 'langchain._api.module_import.create_importer',
                                   'langchain_classic._api.create_importer',
                                   'langchain_classic._api.module_import.create_importer'):
                    continue
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                direct = (len(body) == 1 and isinstance(body[0], ast.Return) and
                          isinstance(body[0].value, ast.Call) and isinstance(body[0].value.func, ast.Name) and
                          body[0].value.func.id in names and len(body[0].value.args) == 1 and
                          isinstance(body[0].value.args[0], ast.Name) and body[0].value.args[0].id == parameter)
                if not direct:
                    continue
                for keyword in node.value.keywords:
                    if keyword.arg not in ('deprecated_lookups', 'module_lookup'):
                        continue
                    mapping = literal(keyword.value)
                    if not isinstance(mapping, dict):
                        continue
                    for local, destination in mapping.items():
                        if isinstance(local, str) and isinstance(destination, str):
                            old, new = f'{name}.{local}', f'{destination}.{local}'
                            kind = 'official_deprecated_export' if keyword.arg == 'deprecated_lookups' else 'lazy_reexport'
                            bind(local, Symbol('alias', old,
                                 [proof(node, kind, old_path=old, new_path=new), proof(forwarder, 'getattr_forwarder')], new))
                    recognized = True
            # General literal-map importlib __getattr__: require matching lookup/import/getattr data flow.
            for block in body:
                if not isinstance(block, ast.If) or not isinstance(block.test, ast.Compare):
                    continue
                test = block.test
                if not (isinstance(test.left, ast.Name) and test.left.id == parameter and
                        len(test.ops) == 1 and isinstance(test.ops[0], ast.In) and
                        isinstance(test.comparators[0], ast.Name)):
                    continue
                lookup = test.comparators[0].id
                mapping = constants.get(lookup)
                if not isinstance(mapping, dict) or len(block.body) != 2:
                    continue
                assign, returned = block.body
                if not (isinstance(assign, ast.Assign) and len(assign.targets) == 1 and
                        isinstance(assign.targets[0], ast.Name) and isinstance(assign.value, ast.Call) and
                        dotted(assign.value.func, bindings) == 'importlib.import_module' and len(assign.value.args) == 1):
                    continue
                sub = assign.value.args[0]
                if not (isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id == lookup
                        and isinstance(sub.slice, ast.Name) and sub.slice.id == parameter):
                    continue
                call = returned.value if isinstance(returned, ast.Return) else None
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'getattr'
                        and len(call.args) == 2 and isinstance(call.args[0], ast.Name)
                        and call.args[0].id == assign.targets[0].id and isinstance(call.args[1], ast.Name)
                        and call.args[1].id == parameter):
                    continue
                for local, destination in mapping.items():
                    if isinstance(local, str) and isinstance(destination, str):
                        old, new = f'{name}.{local}', f'{destination}.{local}'
                        bind(local, Symbol('alias', old, [proof(block, 'lazy_reexport', old_path=old, new_path=new)], new))
                recognized = True
            # import_attr(attr, literal_map.get(attr), __spec__.parent), used by
            # official lazy-export modules; include the helper's source definition.
            if len(body) in (3, 4):
                first, second, returned = body[0], body[1], body[-1]
                if (isinstance(first, ast.Assign) and len(first.targets) == 1 and
                        isinstance(first.targets[0], ast.Name) and isinstance(first.value, ast.Call) and
                        isinstance(first.value.func, ast.Attribute) and first.value.func.attr == 'get' and
                        isinstance(first.value.func.value, ast.Name) and len(first.value.args) == 1 and
                        isinstance(first.value.args[0], ast.Name) and first.value.args[0].id == parameter and
                        isinstance(second, ast.Assign) and len(second.targets) == 1 and
                        isinstance(second.targets[0], ast.Name) and isinstance(second.value, ast.Call)):
                    lookup = first.value.func.value.id
                    mapping = constants.get(lookup)
                    call = second.value
                    helper = dotted(call.func, bindings)
                    valid_args = (len(call.args) == 3 and isinstance(call.args[0], ast.Name) and
                                  call.args[0].id == parameter and isinstance(call.args[1], ast.Name) and
                                  call.args[1].id == first.targets[0].id and isinstance(call.args[2], ast.Attribute) and
                                  isinstance(call.args[2].value, ast.Name) and call.args[2].value.id == '__spec__' and
                                  call.args[2].attr == 'parent')
                    returns_value = (isinstance(returned, ast.Return) and isinstance(returned.value, ast.Name)
                                     and returned.value.id == second.targets[0].id)
                    cache_only = len(body) == 3
                    if len(body) == 4:
                        cache = body[2]
                        cache_only = (isinstance(cache, ast.Assign) and len(cache.targets) == 1 and
                                      isinstance(cache.targets[0], ast.Subscript) and
                                      isinstance(cache.targets[0].value, ast.Call) and
                                      isinstance(cache.targets[0].value.func, ast.Name) and
                                      cache.targets[0].value.func.id == 'globals' and
                                      isinstance(cache.targets[0].slice, ast.Name) and
                                      cache.targets[0].slice.id == parameter and isinstance(cache.value, ast.Name) and
                                      cache.value.id == second.targets[0].id)
                    if (isinstance(mapping, dict) and valid_args and returns_value and cache_only and
                            helper == 'langchain_core._import_utils.import_attr'):
                        helper_resolution = self.resolve(helper)
                        if helper_resolution.state == 'found' and helper_resolution.symbol.kind == 'function':
                            parent = name if file.name == '__init__.py' else name.rpartition('.')[0]
                            for local, destination in mapping.items():
                                if not isinstance(local, str) or not isinstance(destination, str):
                                    continue
                                old = f'{name}.{local}'
                                new = f'{parent}.{local}' if destination == '__module__' else f'{parent}.{destination}.{local}'
                                bind(local, Symbol('alias', old,
                                     [proof(forwarder, 'relative_lazy_reexport', old_path=old, new_path=new),
                                      *helper_resolution.evidence], new))
                            recognized = True
            # Name-dispatch lazy exports returning a helper's imported object.
            # The helper must contain only a direct import and return, not a factory call.
            functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            branch = body[0] if body and isinstance(body[0], ast.If) else None
            while isinstance(branch, ast.If):
                tests = branch.test.values if isinstance(branch.test, ast.BoolOp) and isinstance(branch.test.op, ast.Or) else [branch.test]
                names = []
                for test in tests:
                    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == parameter
                            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                            and isinstance(test.comparators[0], ast.Constant) and isinstance(test.comparators[0].value, str)):
                        names.append(test.comparators[0].value)
                returned = branch.body[0] if len(branch.body) == 1 else None
                call = returned.value if isinstance(returned, ast.Return) else None
                helper = functions.get(call.func.id) if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and not call.args and not call.keywords else None
                helper_body = [n for n in helper.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                               and isinstance(n.value.value, str))] if helper else []
                if names and len(names) == len(tests) and len(helper_body) == 2:
                    imported, returned = helper_body
                    if (isinstance(imported, ast.ImportFrom) and isinstance(returned, ast.Return)
                            and isinstance(returned.value, ast.Name) and not helper.decorator_list):
                        for alias in imported.names:
                            if alias.name == '*' or (alias.asname or alias.name) != returned.value.id:
                                continue
                            parent = name if file.name == '__init__.py' else name.rpartition('.')[0]
                            parts = parent.split('.')
                            module_name = ('.'.join(parts[:len(parts) - imported.level + 1] + ([imported.module] if imported.module else []))
                                           if imported.level else imported.module)
                            for local in names:
                                old, new = f'{name}.{local}', f'{module_name}.{alias.name}'
                                bind(local, Symbol('alias', old, [proof(branch, 'dispatch_reexport', old_path=old, new_path=new),
                                                                 proof(helper, 'import_return_helper')], new))
                            recognized = True
                branch = branch.orelse[0] if len(branch.orelse) == 1 and isinstance(branch.orelse[0], ast.If) else None
            if not recognized:
                result.issues.append(proof(forwarder, 'unresolved_dynamic_exports'))
        return result

    def resolve(self, path, visiting=None):
        if path in self.resolutions:
            return self.resolutions[path]
        visiting = set() if visiting is None else set(visiting)
        if path in visiting:
            return Resolution('unknown', path, reason='Alias or inheritance cycle')
        visiting.add(path)
        root = path.split('.')[0]
        if not self.ensure(root):
            return Resolution('error' if root in self.errors else 'unknown', path,
                              reason=self.errors.get(root, 'External package is outside selected scope'))
        parts = path.split('.')
        for count in range(len(parts), 0, -1):
            module = self.module('.'.join(parts[:count]))
            if module is None:
                continue
            if not module.parsed:
                return Resolution('error', path, evidence=module.issues, reason='Source file parsing failed')
            rest = parts[count:]
            if not rest:
                return Resolution('found', path, Symbol('module', module.path, module.evidence), module.evidence)
            symbol = module.symbols.get(rest[0])
            if symbol is None:
                for destination, proof in module.stars:
                    target_module = self.module(destination)
                    if target_module and (rest[0] in target_module.exports if target_module.exports is not None
                                          else not rest[0].startswith('_')):
                        found = self.resolve(destination + '.' + '.'.join(rest), visiting)
                        if found.state == 'found':
                            return Resolution('found', path, found.symbol, [proof, *found.evidence])
                return Resolution('unknown' if module.issues or module.stars else 'not_found', path,
                                  evidence=module.issues, reason='Export not statically verified')
            if symbol.kind == 'unknown':
                result = Resolution('unknown', path, evidence=symbol.evidence, reason='Binding depends on unresolved source control flow')
            elif symbol.kind == 'alias':
                destination = symbol.target + ('.' + '.'.join(rest[1:]) if len(rest) > 1 else '')
                found = self.resolve(destination, visiting)
                result = Resolution(found.state, path, found.symbol, unique(symbol.evidence + found.evidence), found.reason)
            elif len(rest) == 1:
                result = Resolution('found', path, symbol, symbol.evidence)
            elif symbol.kind == 'class' and len(rest) == 2:
                result = self.member(symbol, rest[1], visiting)
                result.path = path
            else:
                result = Resolution('unknown', path, reason='Non-module attribute chain is not inferred')
            self.resolutions[path] = result
            return result
        return Resolution('not_found', path, reason='Module path absent from the selected artifact')

    def mro(self, symbol, visiting=None):
        visiting = set() if visiting is None else set(visiting)
        if symbol.path in visiting:
            return None
        visiting.add(symbol.path)
        parents, sequences, evidence = [], [], []
        for base in symbol.bases:
            if base in ('object', 'abc.ABC', 'typing.Generic', 'typing.Protocol'):
                continue
            found = self.resolve(base)
            if found.state != 'found' or found.symbol.kind != 'class':
                return None
            result = self.mro(found.symbol, visiting)
            if result is None:
                return None
            order, proofs = result
            parents.append(found.symbol)
            sequences.append(list(order))
            evidence.extend(found.evidence + proofs)
        sequences.append(parents)
        order = [symbol]
        while any(sequences):
            sequences = [s for s in sequences if s]
            candidate = next((s[0] for s in sequences
                              if all(s[0].path not in [x.path for x in other[1:]] for other in sequences)), None)
            if candidate is None:
                return None
            order.append(candidate)
            for sequence in sequences:
                if sequence[0].path == candidate.path:
                    sequence.pop(0)
        return order, unique(evidence)

    def member(self, owner, name, visiting=None):
        if name in owner.members:
            symbol = owner.members[name]
            if symbol.kind == 'unknown':
                return Resolution('unknown', symbol.path, evidence=symbol.evidence, reason='Conditional member binding')
            return Resolution('found', symbol.path, symbol, owner.evidence + symbol.evidence)
        order = self.mro(owner)
        if order is None:
            return Resolution('unknown', f'{owner.path}.{name}', reason='Base class or C3 order unresolved')
        classes, evidence = order
        for ancestor in classes[1:]:
            if name in ancestor.members:
                symbol = ancestor.members[name]
                if symbol.kind == 'unknown':
                    return Resolution('unknown', symbol.path, evidence=symbol.evidence, reason='Conditional inherited member binding')
                proof = dict(kind='inherited_member', owner=owner.path, defined_on=ancestor.path,
                             mro=[c.path for c in classes])
                return Resolution('found', f'{owner.path}.{name}', symbol,
                                  unique(owner.evidence + [proof] + evidence + symbol.evidence))
        return Resolution('not_found', f'{owner.path}.{name}', reason='Member absent from resolved class hierarchy')

    def candidates(self, path, limit=30):
        """Candidate search never certifies a migration; parse only files containing the leaf."""
        leaf = path.rsplit('.', 1)[-1]
        if leaf in self.candidate_cache:
            return [candidate for candidate in self.candidate_cache[leaf] if candidate != path][:limit]
        candidates = set()
        for package in PACKAGES:
            distribution = self.ensure(package)
            if distribution is None:
                continue
            for file in distribution.root.rglob('*.py'):
                try:
                    if file not in self.source_texts:
                        with tokenize.open(file) as stream:
                            self.source_texts[file] = stream.read()
                    if not re.search(r'\b' + re.escape(leaf) + r'\b', self.source_texts[file]):
                        continue
                except (OSError, UnicodeError):
                    relative = file.relative_to(distribution.root)
                    parts = relative.parent.parts if file.name == '__init__.py' else relative.with_suffix('').parts
                    self.module('.'.join((package, *parts)))  # record the file failure
                    continue
                relative = file.relative_to(distribution.root)
                parts = relative.parent.parts if file.name == '__init__.py' else relative.with_suffix('').parts
                module = self.module('.'.join((package, *parts)))
                if module and leaf in module.symbols:
                    candidate = f'{module.path}.{leaf}'
                    found = self.resolve(candidate)
                    if found.state == 'found' and found.symbol.kind in ('class', 'function'):
                        candidates.add(candidate)
        self.candidate_cache[leaf] = sorted(candidates)
        return [candidate for candidate in self.candidate_cache[leaf] if candidate != path][:limit]

    def report(self):
        issues = [issue for module in self.modules.values() for issue in module.issues]
        return dict(label=self.label, versions={p: d.version for p, d in self.distributions.items()},
                    distributions=[d.record() for d in self.distributions.values()],
                    errors=self.errors, dependency_selection=self.selection,
                    index_scope='on-demand reachable source modules, not an exhaustive package index',
                    parsed_module_count=sum(m.parsed for m in self.modules.values()),
                    parse_errors=[i for i in issues if i['kind'] == 'parse_error'],
                    unresolved=[i for i in issues if i['kind'] != 'parse_error'])


class MigrationAnalyzer:
    def __init__(self, comparison, histories=None, history_pins=None, history_depth=2, search_candidates=True):
        self.comparison = comparison
        self.histories = list(histories or [])
        self.history_pins, self.history_depth = history_pins or {}, history_depth
        self.history_searched, self.history_errors, self.memo = set(), [], {}
        self.search_candidates = search_candidates
        self.signatures = {}

    def load_history(self, package):
        if package in self.history_searched:
            return
        self.history_searched.add(package)
        if self.comparison.store is None or any(package in h.distributions for h in self.histories):
            return
        distribution = self.comparison.ensure(package)
        if distribution is None:
            return
        try:
            versions = ([self.history_pins[package]] if package in self.history_pins else
                        self.comparison.store.historical_anchors(package, distribution.version, self.history_depth))
            for version in versions:
                snapshot = self.comparison.store.fetch(package, version)
                history = SourceWorld(self.comparison.store, self.history_pins.copy(), {package: snapshot},
                                      snapshot.released, f'historical:{package}=={version}')
                self.histories.append(history)
        except Exception as error:
            self.history_errors.append({'package': package, 'error': str(error)})

    def check(self, path):
        if path in self.memo:
            return self.memo[path]
        result = dict(old_path=path, status='UNVERIFIED', replacement=None, replacement_candidates=[],
                      evidence=[], reason='', symbol_kind=None, verification_attempts=[], basis=None)
        current = self.comparison.resolve(path)
        result['verification_attempts'].append({'snapshot': self.comparison.label, 'state': current.state,
                                                'reason': current.reason})
        if current.state == 'found':
            result.update(status='EXISTS', symbol_kind=current.symbol.kind, evidence=current.evidence,
                          reason='Path is statically verified in the selected comparison distribution')
        elif current.state == 'error':
            result.update(status='ANALYSIS_ERROR', reason=current.reason, evidence=current.evidence)
        else:
            self.load_history(path.split('.')[0])
            alternatives = {}
            historical_kinds, historical_evidence = set(), []
            for history in self.histories:
                if path.split('.')[0] not in history.distributions:
                    continue
                old = history.resolve(path)
                result['verification_attempts'].append({'snapshot': history.label, 'state': old.state,
                                                        'reason': old.reason, 'source_errors': dict(history.errors)})
                if old.state != 'found':
                    continue
                historical_kinds.add(old.symbol.kind)
                historical_evidence.extend(old.evidence)
                # An actual source alias edge proves a relationship. Leaf equality does not.
                for edge in old.evidence:
                    previous, new = edge.get('old_path'), edge.get('new_path')
                    if not previous or not new or not (path == previous or path.startswith(previous + '.')):
                        continue
                    suffix = path[len(previous):]
                    destination = new + suffix
                    if destination == path:
                        continue
                    target = self.comparison.resolve(destination)
                    if target.state != 'found' or target.symbol.kind != old.symbol.kind:
                        continue
                    if old.symbol.kind == 'method':
                        # Prove owner mapping separately, in addition to both member hierarchies.
                        owner = path.rpartition('.')[0]
                        owner_old = history.resolve(owner)
                        owner_new = self.comparison.resolve(destination.rpartition('.')[0])
                        if not (owner_old.state == owner_new.state == 'found' and
                                owner_old.symbol.kind == owner_new.symbol.kind == 'class' and
                                previous == owner):
                            continue
                    # Canonical identity merges reexports of the same implementation.
                    key = target.symbol.path
                    proof = unique(old.evidence + [dict(kind='comparison_destination', path=destination)] + target.evidence)
                    alternatives.setdefault(key, (destination, proof, old.symbol.kind))
            split = None if alternatives else self.package_split(path)
            if split:
                destination, evidence, kind = split
                result.update(status='MOVED', replacement=destination, replacement_candidates=[destination],
                              evidence=evidence, symbol_kind=kind, basis=BASIS_SPLIT,
                              reason='langchain to langchain_classic package split: the historical definition and the '
                                     'comparison definition have identical normalized source AST (see evidence '
                                     'normalization); this does not establish behavioral or signature compatibility')
            elif len(alternatives) == 1:
                destination, evidence, kind = next(iter(alternatives.values()))
                result.update(status='MOVED', replacement=destination, replacement_candidates=[destination],
                              evidence=evidence, symbol_kind=kind, basis=BASIS_OFFICIAL,
                              reason='Historical official export relationship and comparison destination verified; '
                                     'this does not establish behavioral or signature compatibility')
            elif alternatives:
                result.update(replacement_candidates=sorted(v[0] for v in alternatives.values()),
                              reason='Conflicting evidence destinations; no replacement selected')
            else:
                if len(historical_kinds) == 1:
                    result['symbol_kind'] = next(iter(historical_kinds))
                result['evidence'] = unique(historical_evidence + current.evidence)
                # A known historical method must never search other classes by its leaf name.
                is_method = any(h.resolve(path).state == 'found' and h.resolve(path).symbol.kind == 'method'
                                for h in self.histories if path.split('.')[0] in h.distributions)
                if self.search_candidates and not is_method:
                    result['replacement_candidates'] = self.comparison.candidates(path)
                result['reason'] = 'No verified old-to-new relationship; absence and same-name candidates are not change evidence'
                failures = [a for a in result['verification_attempts']
                            if a['state'] == 'error' or a.get('source_errors')]
                failures += [e for e in self.history_errors if e['package'] == path.split('.')[0]]
                if failures:
                    result.update(status='ANALYSIS_ERROR', reason='Required historical evidence could not be analyzed',
                                  analysis_errors=failures)
        self.memo[path] = result
        return result

    def definition_signature(self, world, proof):
        """AST of the defining node with langchain_classic normalized to langchain; None if unreadable."""
        distribution = world.distributions.get(proof.get('package'))
        if distribution is None:
            return None
        file = distribution.root.parent / proof['file']
        key = (str(file), proof['line'], proof['end_line'])
        if key not in self.signatures:
            signature = None
            try:
                with tokenize.open(file) as stream:
                    tree = ast.parse(stream.read().replace('langchain_classic', 'langchain'))
                for node in ast.walk(tree):
                    if (isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.lineno == proof['line'] and node.end_lineno == proof['end_line']):
                        signature = ast.dump(DefinitionNormalizer().visit(node))
                        break
            except (OSError, UnicodeError, SyntaxError):
                signature = None
            self.signatures[key] = signature
        return self.signatures[key]

    def same_definition(self, old_world, old, new_world, new):
        """Proof that langchain's own definition was carried unchanged into langchain_classic."""
        old_proofs = [e for e in old.symbol.evidence if e.get('kind') in ('definition', 'member_definition')]
        new_proofs = [e for e in new.symbol.evidence if e.get('kind') in ('definition', 'member_definition')]
        if not old_proofs or not new_proofs:
            return None
        old_proof, new_proof = old_proofs[-1], new_proofs[-1]
        if not (old_proof['package'] == 'langchain' and new_proof['package'] == 'langchain_classic'):
            return None
        old_sig = self.definition_signature(old_world, old_proof)
        if old_sig is None or old_sig != self.definition_signature(new_world, new_proof):
            return None
        return dict(kind='package_split_definition_match', normalization=NORMALIZATION,
                    old_package=old_proof['package'], old_version=old_proof['version'],
                    old_file=old_proof['file'], old_line=old_proof['line'], old_end_line=old_proof['end_line'],
                    new_package=new_proof['package'], new_version=new_proof['version'],
                    new_file=new_proof['file'], new_line=new_proof['line'], new_end_line=new_proof['end_line'])

    def package_split(self, path):
        """langchain.X -> langchain_classic.X only when langchain's own definition is unchanged."""
        root, _, rest = path.partition('.')
        if root != 'langchain' or not rest or 'langchain_classic' not in PACKAGES:
            return None
        destination = 'langchain_classic.' + rest
        for history in self.histories:
            if 'langchain' not in history.distributions:
                continue
            old = history.resolve(path)
            if old.state != 'found' or old.symbol.kind not in ('class', 'function', 'method'):
                continue
            target = self.comparison.resolve(destination)
            if target.state != 'found' or target.symbol.kind != old.symbol.kind:
                continue
            if old.symbol.kind == 'method':
                owner = path.rpartition('.')[0]
                owner_old, owner_new = history.resolve(owner), self.comparison.resolve(destination.rpartition('.')[0])
                if not (owner_old.state == owner_new.state == 'found' and
                        owner_old.symbol.kind == owner_new.symbol.kind == 'class'):
                    continue
                owner_match = self.same_definition(history, owner_old, self.comparison, owner_new)
                if owner_match is None:
                    continue
                member_match = self.same_definition(history, old, self.comparison, target)
                if member_match is None:
                    # A member inherited from one external package is accepted only if both sides inherit it from that package.
                    old_pkgs = {e['package'] for e in old.symbol.evidence if e.get('kind') == 'member_definition'}
                    new_pkgs = {e['package'] for e in target.symbol.evidence if e.get('kind') == 'member_definition'}
                    if not old_pkgs or old_pkgs != new_pkgs or old_pkgs & {'langchain', 'langchain_classic'}:
                        continue
                matches = [owner_match] + ([member_match] if member_match else [])
            else:
                match = self.same_definition(history, old, self.comparison, target)
                if match is None:
                    continue
                matches = [match]
            proof = unique(old.evidence + matches + [dict(kind='comparison_destination', path=destination)] + target.evidence)
            return destination, proof, old.symbol.kind
        return None

    def is_class(self, path):
        return self.check(path)['symbol_kind'] == 'class'


@dataclass(frozen=True)
class Reference:
    path: str
    kind: str | None = None


@dataclass(frozen=True)
class Instance:
    owner: str


@dataclass(frozen=True)
class Unknown:
    origin: str
    reason: str


class Locals(ast.NodeVisitor):
    def __init__(self):
        self.names = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node):
        self.names.update(a.asname or a.name.split('.')[0] for a in node.names)

    def visit_ImportFrom(self, node):
        self.names.update(a.asname or a.name for a in node.names if a.name != '*')

    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Lambda(self, node):
        pass

    def visit_ListComp(self, node):
        pass

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_ExceptHandler(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.names.add(node.rest)
        self.generic_visit(node)


class NotebookExtractor(ast.NodeVisitor):
    """Track only proven constructors, and retain lexical shadowing/unknowns."""
    def __init__(self, analyzer, symbols, file, cell, source):
        self.analyzer, self.symbols = analyzer, symbols
        self.file, self.cell, self.source = file, cell, source
        self.rows, self.class_outer = [], None

    @staticmethod
    def target(path):
        return path.split('.')[0] in PACKAGES

    def emit(self, node, kind, value):
        if isinstance(value, Reference) and self.target(value.path):
            result = dict(self.analyzer.check(value.path))
        elif isinstance(value, Unknown) and self.target(value.origin):
            result = dict(old_path=None, status='UNVERIFIED', replacement=None, replacement_candidates=[],
                          evidence=[], reason=value.reason, inference_origin=value.origin, inference='UNKNOWN')
        else:
            return
        result.update(file=self.file, cell=self.cell, line=node.lineno, column=node.col_offset,
                      end_line=node.end_lineno, end_column=node.end_col_offset, type=kind,
                      code=ast.get_source_segment(self.source, node))
        self.rows.append(result)

    def value(self, node):
        if isinstance(node, ast.Name):
            return self.symbols.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.value(node.value)
            if isinstance(parent, Unknown):
                return parent
            if isinstance(parent, Instance):
                return Reference(f'{parent.owner}.{node.attr}', 'member')
            if isinstance(parent, Reference):
                known = self.analyzer.memo.get(parent.path, {})
                if parent.kind == 'member' or known.get('symbol_kind') in ('function', 'method', 'attribute'):
                    return Unknown(parent.path, 'Attribute on a non-module value cannot be inferred')
                return Reference(f'{parent.path}.{node.attr}', 'member' if known.get('symbol_kind') == 'class' else None)
        if isinstance(node, ast.Call):
            function = self.value(node.func)
            if isinstance(function, Unknown):
                return function
            if isinstance(function, Reference) and self.target(function.path):
                if self.analyzer.is_class(function.path):
                    return Instance(function.path)
                return Unknown(function.path, 'Return type is unknown; call target is not a verified class')
        return None

    def bind(self, target, value=None):
        if isinstance(target, ast.Name):
            self.symbols[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.bind(item)

    def visit_Import(self, node):
        for alias in node.names:
            self.symbols[alias.asname or alias.name.split('.')[0]] = Reference(alias.name if alias.asname else alias.name.split('.')[0])
            self.emit(node, 'IMPORT', Reference(alias.name))

    def visit_ImportFrom(self, node):
        if node.level:
            for alias in node.names:
                self.symbols[alias.asname or alias.name] = None
            return
        for alias in node.names:
            if alias.name == '*':
                for name, value in self.symbols.items():
                    self.symbols[name] = Unknown(value.path, 'Wildcard import may shadow this binding') if isinstance(value, Reference) else None
                self.emit(node, 'IMPORT', Unknown(node.module or '', 'Wildcard notebook import is not inferred'))
                continue
            reference = Reference(f'{node.module}.{alias.name}')
            self.symbols[alias.asname or alias.name] = reference
            self.emit(node, 'IMPORT', reference)

    def visit_Assign(self, node):
        self.visit(node.value)
        value = self.value(node.value)
        for target in node.targets:
            self.bind(target, value)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
            self.bind(node.target, self.value(node.value))

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.bind(node.target, self.value(node.value))

    def visit_AugAssign(self, node):
        self.visit(node.value)
        self.bind(node.target)

    def visit_Delete(self, node):
        for target in node.targets:
            self.bind(target)

    def visit_Call(self, node):
        value = self.value(node.func)
        self.emit(node, 'METHOD_CALL' if isinstance(node.func, ast.Attribute) else 'API_CALL', value)
        self.generic_visit(node)

    def scoped_function(self, node, body):
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default:
                self.visit(default)
        outer, enclosing_class = self.symbols, self.class_outer
        self.symbols = (self.class_outer if self.class_outer is not None else outer).copy()
        locals_ = Locals()
        for stmt in body:
            locals_.visit(stmt)
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        args += [a for a in (node.args.vararg, node.args.kwarg) if a]
        names = locals_.names | {a.arg for a in args}
        if hasattr(node, 'name'):
            names.add(node.name)
        for name in names:
            self.symbols[name] = None
        self.class_outer = None
        for stmt in body:
            self.visit(stmt)
        self.symbols, self.class_outer = outer, enclosing_class

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.scoped_function(node, node.body)
        self.symbols[node.name] = None

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.scoped_function(node, [node.body])

    def visit_ClassDef(self, node):
        for expr in [*node.bases, *node.decorator_list]:
            self.visit(expr)
        outer, enclosing_class = self.symbols, self.class_outer
        self.class_outer, self.symbols = outer.copy(), outer.copy()
        self.class_outer[node.name] = None
        for stmt in node.body:
            self.visit(stmt)
        self.symbols, self.class_outer = outer, enclosing_class
        self.symbols[node.name] = None

    @staticmethod
    def merged(paths):
        result = {}
        for key in set().union(*(set(path) for path in paths)):
            values = [path.get(key) for path in paths]
            if all(value == values[0] for value in values):
                result[key] = values[0]
            else:
                origin = next((v.path if isinstance(v, Reference) else v.owner if isinstance(v, Instance)
                               else v.origin for v in values if isinstance(v, (Reference, Instance, Unknown))), None)
                result[key] = Unknown(origin, 'Binding depends on control flow') if origin else None
        return result

    def visit_If(self, node):
        self.visit(node.test)
        outer = self.symbols
        paths = []
        for body in (node.body, node.orelse):
            self.symbols = outer.copy()
            for stmt in body:
                self.visit(stmt)
            paths.append(self.symbols)
        self.symbols = outer
        outer.update(self.merged(paths))

    visit_While = visit_If

    def visit_For(self, node):
        self.visit(node.iter)
        before = self.symbols.copy()
        self.bind(node.target)
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt)
        self.symbols.update(self.merged([before, self.symbols.copy()]))

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            value = self.value(item.context_expr)
            origin = value.owner if isinstance(value, Instance) else value.origin if isinstance(value, Unknown) else None
            self.bind(item.optional_vars, Unknown(origin, 'Context manager return type unknown') if origin else None)
        for stmt in node.body:
            self.visit(stmt)

    visit_AsyncWith = visit_With

    def visit_Try(self, node):
        outer = self.symbols
        before = outer.copy()
        for stmt in [*node.body, *node.orelse]:
            self.visit(stmt)
        paths = [self.symbols.copy()]
        assignments = Locals()
        for stmt in node.body:
            assignments.visit(stmt)
        for handler in node.handlers:
            self.symbols = before.copy()
            for name in assignments.names:
                self.symbols[name] = None
            if handler.name:
                self.symbols[handler.name] = None
            for stmt in handler.body:
                self.visit(stmt)
            paths.append(self.symbols)
        self.symbols = outer
        outer.update(self.merged(paths))
        for stmt in node.finalbody:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_Global(self, node):
        for name in node.names:
            self.symbols[name] = None

    visit_Nonlocal = visit_Global

    def visit_ListComp(self, node):
        outer = self.symbols
        self.symbols = outer.copy()
        for generator in node.generators:
            self.visit(generator.iter)
            self.bind(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for field in ('elt', 'key', 'value'):
            if hasattr(node, field):
                self.visit(getattr(node, field))
        self.symbols = outer

    visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp

    def visit_Match(self, node):
        self.visit(node.subject)
        outer, paths = self.symbols, [self.symbols.copy()]
        for case in node.cases:
            self.symbols = outer.copy()
            names = Locals()
            names.visit(case.pattern)
            for name in names.names:
                self.symbols[name] = None
            if case.guard:
                self.visit(case.guard)
            for stmt in case.body:
                self.visit(stmt)
            paths.append(self.symbols)
        self.symbols = outer
        outer.update(self.merged(paths))


def analyze_notebooks(paths, analyzer):
    results, notebooks, diagnostics = [], [], []
    for path in paths:
        path = Path(path)
        symbols, rows = {}, []
        try:
            raw = path.read_bytes()
            notebook = nbformat.reads(raw.decode('utf-8-sig'), as_version=4)
            notebooks.append({'file': str(path), 'sha256': hashlib.sha256(raw).hexdigest()})
        except Exception as error:
            diagnostics.append({'file': str(path), 'status': 'ANALYSIS_ERROR', 'reason': str(error)})
            continue
        for cell, content in enumerate(notebook.cells):
            if content.cell_type != 'code':
                continue
            source = content.source
            lines = source.splitlines()
            if any(line.lstrip().startswith('%%') for line in lines):
                diagnostics.append(dict(file=str(path), cell=cell, status='ANALYSIS_ERROR', reason='Cell magic is not Python-analyzed'))
                symbols.clear()
                continue
            normalized = '\n'.join('' if line.lstrip().startswith(('%', '!', '?')) else line for line in lines)
            try:
                tree = ast.parse(normalized)
            except SyntaxError as error:
                diagnostics.append(dict(file=str(path), cell=cell, line=error.lineno, status='ANALYSIS_ERROR', reason=error.msg))
                symbols.clear()
                continue
            extractor = NotebookExtractor(analyzer, symbols, str(path), cell, normalized)
            extractor.visit(tree)
            rows.extend(extractor.rows)
        results.extend(rows)
    versions = {p: d.version for p, d in analyzer.comparison.distributions.items()}
    for row in results:
        row['comparison_versions'] = versions
        row['evidence_versions'] = sorted({(e['package'], e['version']) for e in row['evidence'] if 'package' in e})
    by_path = {}
    for row in results:
        if row['old_path']:
            by_path.setdefault(row['old_path'], row)
    return dict(schema_version=SCHEMA_VERSION, generated_at=datetime.now(timezone.utc).isoformat(),
                analyzer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), python_version=sys.version,
                comparison_basis='Static paths in selected official PyPI artifacts; historical sources supply relationship evidence',
                historical_scope='Evidence snapshots, not a reconstruction of the original notebook environment',
                status_definitions={'EXISTS': 'Comparison path verified; runtime usability not asserted',
                                    'MOVED': 'Historical export relationship and comparison destination verified',
                                    'UNVERIFIED': 'Insufficient evidence; does not mean obsolete',
                                    'ANALYSIS_ERROR': 'Required input or source analysis failed'},
                locations={'cell': 'zero-based notebook cell index', 'line': 'one-based', 'column': 'zero-based UTF-8 byte offset'},
                notebooks=notebooks, requested_comparison_versions=analyzer.comparison.pins,
                comparison_versions=versions, comparison_index=analyzer.comparison.report(),
                historical_indexes=[h.report() for h in analyzer.histories], history_errors=analyzer.history_errors,
                moved_by_basis=dict(Counter(r.get('basis') for r in results if r['status'] == 'MOVED')),
                basis_definitions={BASIS_OFFICIAL: 'Official historical export/alias edge plus verified comparison destination',
                                   BASIS_SPLIT: 'langchain -> langchain_classic: identical normalized defining-source AST '
                                                '(' + NORMALIZATION + '); weaker than an official per-symbol declaration'},
                summary=dict(Counter(r['status'] for r in results)), occurrence_count=len(results),
                unique_path_count=len(by_path), unique_path_summary=dict(Counter(r['status'] for r in by_path.values())),
                unresolved_occurrence_count=sum(r.get('inference') == 'UNKNOWN' for r in results),
                diagnostic_error_count=len(diagnostics), diagnostics=diagnostics, results=results)


def version_options(values, parser):
    pins = {}
    for value in values:
        package, separator, version = value.partition('=')
        package = package.replace('-', '_')
        if not separator or package not in PACKAGES:
            parser.error('Version option must be supported PACKAGE=VERSION')
        try:
            Version(version)
        except InvalidVersion:
            parser.error('Invalid version: ' + version)
        pins[package] = version
    return pins


def main():
    parser = argparse.ArgumentParser(description='Verify notebook API migrations using official historical and comparison sources')
    parser.add_argument('notebook', nargs='+', type=Path, help='.ipynb files or directories')
    parser.add_argument('-o', '--output', type=Path, default=Path('api_migration_result.json'))
    parser.add_argument('--package-version', action='append', default=[], metavar='PACKAGE=VERSION')
    parser.add_argument('--source-version', action='append', default=[], metavar='PACKAGE=VERSION',
                        help='Historical evidence anchor; not an assertion of notebook original version')
    parser.add_argument('--history-depth', type=int, choices=range(0, 3), default=2,
                        help='Bounded automatic historical anchor selection when no source version is supplied')
    parser.add_argument('--cache-dir', type=Path, default=Path(tempfile.gettempdir()) / 'api_migration_source_cache')
    parser.add_argument('--no-candidate-search', action='store_true', help='Skip unverified same-name fallback; evidence verification still runs')
    args = parser.parse_args()
    pins = version_options(args.package_version, parser)
    history = version_options(args.source_version, parser)
    paths = []
    for item in args.notebook:
        paths.extend(sorted(item.glob('*.ipynb')) if item.is_dir() else [item])
    paths = list(dict.fromkeys(paths))
    if not paths:
        parser.error('No notebooks found')
    store = PyPIStore(args.cache_dir)
    analyzer = MigrationAnalyzer(SourceWorld(store, pins), history_pins=history,
                                 history_depth=args.history_depth, search_candidates=not args.no_candidate_search)
    report = analyze_notebooks(paths, analyzer)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), 'summary': report['summary'],
                      'occurrences': report['occurrence_count'], 'unique_paths': report['unique_path_count'],
                      'unique_path_summary': report['unique_path_summary'], 'diagnostic_errors': report['diagnostic_error_count']},
                     ensure_ascii=True))


if __name__ == '__main__':
    main()
