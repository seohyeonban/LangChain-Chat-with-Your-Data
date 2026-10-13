"""Offline synthetic tests for the langchain -> langchain_classic package-split rule (v2 only)."""
import tempfile
import unittest
from pathlib import Path

from api_migration_analyzer_v2 import (
    Distribution, SourceWorld, MigrationAnalyzer, BASIS_SPLIT, PACKAGES,
)

OLD_THING = '''
class Same:
    x: int = 1
    """attribute doc"""
    def run(self, a):
        return a + 1
    def helper(self):
        return 1

class Changed:
    def run(self, a):
        return a + 1
    def other(self):
        return 1

class OnlyOldName:
    def run(self):
        return "old"
'''

NEW_THING = '''
from typing import override

class Same:
    """Class docs added during modernization."""
    x: int | None = 1
    """attribute doc, reworded"""
    @override
    def run(self, a: int) -> int:
        """Method docs."""
        return a + 1
    def helper(self):
        return 1

class Changed:
    def run(self, a):
        return a + 2
    def other(self):
        return 1

class OnlyOldName:
    def run(self):
        return "something different"
'''


class PackageSplitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        history = {'langchain': {'__init__.py': '', 'thing.py': OLD_THING,
                                 'shim.py': 'from langchain_community.impl import Reader\n'}}
        current = {'langchain': {'__init__.py': ''},
                   'langchain_classic': {'__init__.py': '', 'thing.py': NEW_THING,
                                         'shim.py': 'from langchain_community.impl import Reader\n'},
                   'langchain_community': {'__init__.py': '', 'impl.py': 'class Reader:\n def run(self): pass\n'}}
        history['langchain_community'] = {'__init__.py': '', 'impl.py': 'class Reader:\n def run(self): pass\n'}
        self.analyzer = MigrationAnalyzer(self.world(current, 'current', '2.0'),
                                          [self.world(history, 'historical', '1.0')], search_candidates=False)

    def tearDown(self):
        self.temp.cleanup()

    def world(self, files, label, version):
        distributions = {}
        for package, entries in files.items():
            root = self.root / label / package
            root.mkdir(parents=True)
            for name, source in entries.items():
                (root / name).write_text(source, encoding='utf-8')
            distributions[package] = Distribution(package, version, root, 'synthetic://fixture', 'synthetic-hash')
        return SourceWorld(distributions=distributions, label=label)

    def test_package_is_supported(self):
        self.assertIn('langchain_classic', PACKAGES)

    def test_modernization_only_is_moved_with_split_basis(self):
        result = self.analyzer.check('langchain.thing.Same')
        self.assertEqual(result['status'], 'MOVED')
        self.assertEqual(result['replacement'], 'langchain_classic.thing.Same')
        self.assertEqual(result['basis'], BASIS_SPLIT)
        match = [e for e in result['evidence'] if e['kind'] == 'package_split_definition_match']
        self.assertEqual(len(match), 1)
        self.assertIn('normalization', match[0])

    def test_member_with_identical_owner_and_body_is_moved(self):
        result = self.analyzer.check('langchain.thing.Same.run')
        self.assertEqual(result['status'], 'MOVED')
        self.assertEqual(result['replacement'], 'langchain_classic.thing.Same.run')
        self.assertEqual(result['symbol_kind'], 'method')

    def test_changed_implementation_is_not_moved(self):
        for path in ('langchain.thing.Changed', 'langchain.thing.Changed.run'):
            result = self.analyzer.check(path)
            self.assertEqual(result['status'], 'UNVERIFIED', path)
            self.assertIsNone(result['replacement'], path)

    def test_same_name_with_different_body_is_not_evidence(self):
        result = self.analyzer.check('langchain.thing.OnlyOldName')
        self.assertEqual(result['status'], 'UNVERIFIED')
        self.assertIsNone(result['replacement'])

    def test_definition_living_in_another_package_is_not_a_split(self):
        # Re-exports of another package are handled by the existing relationship rule, never the split rule.
        result = self.analyzer.check('langchain.shim.Reader')
        self.assertNotEqual(result['basis'], BASIS_SPLIT)
        self.assertIsNone(self.analyzer.package_split('langchain.shim.Reader'))
        if result['status'] == 'MOVED':
            self.assertEqual(result['replacement'], 'langchain_community.impl.Reader')

    def test_missing_in_classic_stays_unverified(self):
        result = self.analyzer.check('langchain.thing.DoesNotExist')
        self.assertEqual(result['status'], 'UNVERIFIED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
