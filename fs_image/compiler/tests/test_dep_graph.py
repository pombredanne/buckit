#!/usr/bin/env python3
import sys
import unittest

from tests.temp_subvolumes import TempSubvolumes

from ..dep_graph import (
    DependencyGraph, ItemProv, ItemReq, ItemReqsProvs, ValidatedReqsProvs,
)
from ..items import (
    CopyFileItem, FilesystemRootItem, ImageItem, MakeDirsItem, PhaseOrder,
)
from ..provides import ProvidesDirectory, ProvidesDoNotAccess, ProvidesFile
from ..requires import require_directory


PATH_TO_ITEM = {
    '/': FilesystemRootItem(from_target=''),
    '/a/b/c': MakeDirsItem(from_target='', into_dir='/', path_to_make='a/b/c'),
    '/a/d/e': MakeDirsItem(from_target='', into_dir='a', path_to_make='d/e'),
    '/a/b/c/F': CopyFileItem(from_target='', source='x', dest='a/b/c/F'),
    '/a/d/e/G': CopyFileItem(from_target='', source='G', dest='a/d/e/'),
}


def _fs_root_phases(item):
    return [(FilesystemRootItem.get_phase_builder, (item,))]


class ValidateReqsProvsTestCase(unittest.TestCase):

    def test_duplicate_paths_in_same_item(self):

        class BadDuplicatePathItem(metaclass=ImageItem):
            def requires(self):
                yield require_directory('a')

            def provides(self):
                yield ProvidesDirectory(path='a')

        with self.assertRaisesRegex(AssertionError, '^Same path in '):
            ValidatedReqsProvs([BadDuplicatePathItem(from_target='t')])

    def test_duplicate_paths_provided(self):
        with self.assertRaisesRegex(
            RuntimeError, '^Both .* and .* from .* provide the same path$'
        ):
            ValidatedReqsProvs([
                CopyFileItem(from_target='', source='x', dest='y/'),
                MakeDirsItem(from_target='', into_dir='/', path_to_make='y/x'),
            ])

    def test_unmatched_requirement(self):
        item = CopyFileItem(from_target='', source='x', dest='y')
        with self.assertRaises(
            RuntimeError,
            msg='^At /: nothing in set() matches the requirement '
                f'{ItemReq(requires=require_directory("/"), item=item)}$',
        ):
            ValidatedReqsProvs([item])

    def test_paths_to_reqs_provs(self):
        self.assertEqual(
            ValidatedReqsProvs(PATH_TO_ITEM.values()).path_to_reqs_provs,
            {
                '/meta': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDoNotAccess(path='/meta'), PATH_TO_ITEM['/']
                    )},
                    item_reqs=set(),
                ),
                '/': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='/'), PATH_TO_ITEM['/']
                    )},
                    item_reqs={ItemReq(
                        require_directory('/'), PATH_TO_ITEM['/a/b/c']
                    )},
                ),
                '/a': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='a'), PATH_TO_ITEM['/a/b/c']
                    )},
                    item_reqs={ItemReq(
                        require_directory('a'), PATH_TO_ITEM['/a/d/e']
                    )},
                ),
                '/a/b': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='a/b'), PATH_TO_ITEM['/a/b/c']
                    )},
                    item_reqs=set(),
                ),
                '/a/b/c': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='a/b/c'), PATH_TO_ITEM['/a/b/c']
                    )},
                    item_reqs={ItemReq(
                        require_directory('a/b/c'), PATH_TO_ITEM['/a/b/c/F']
                    )},
                ),
                '/a/b/c/F': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesFile(path='a/b/c/F'), PATH_TO_ITEM['/a/b/c/F']
                    )},
                    item_reqs=set(),
                ),
                '/a/d': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='a/d'), PATH_TO_ITEM['/a/d/e']
                    )},
                    item_reqs=set(),
                ),
                '/a/d/e': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesDirectory(path='a/d/e'), PATH_TO_ITEM['/a/d/e']
                    )},
                    item_reqs={ItemReq(
                        require_directory('a/d/e'), PATH_TO_ITEM['/a/d/e/G']
                    )},
                ),
                '/a/d/e/G': ItemReqsProvs(
                    item_provs={ItemProv(
                        ProvidesFile(path='a/d/e/G'), PATH_TO_ITEM['/a/d/e/G']
                    )},
                    item_reqs=set(),
                ),
            }
        )


class DependencyGraphTestCase(unittest.TestCase):

    def test_item_predecessors(self):
        dg = DependencyGraph(PATH_TO_ITEM.values())
        self.assertEqual(
            _fs_root_phases(PATH_TO_ITEM['/']), list(dg.ordered_phases()),
        )
        ns = dg._prep_item_predecessors('fake_subvol_path')
        self.assertEqual(ns.item_to_predecessors, {
            PATH_TO_ITEM[k]: {PATH_TO_ITEM[v] for v in vs} for k, vs in {
                '/a/b/c': {'/'},
                '/a/d/e': {'/a/b/c'},
                '/a/b/c/F': {'/a/b/c'},
                '/a/d/e/G': {'/a/d/e'},
            }.items()
        })
        self.assertEqual(ns.predecessor_to_items, {
            PATH_TO_ITEM[k]: {PATH_TO_ITEM[v] for v in vs} for k, vs in {
                '/': {'/a/b/c'},
                '/a/b/c': {'/a/d/e', '/a/b/c/F'},
                '/a/b/c/F': set(),
                '/a/d/e': {'/a/d/e/G'},
                '/a/d/e/G': set(),
            }.items()
        })
        self.assertEqual(ns.items_without_predecessors, {PATH_TO_ITEM['/']})


class DependencyOrderItemsTestCase(unittest.TestCase):

    def test_gen_dependency_graph(self):
        dg = DependencyGraph(PATH_TO_ITEM.values())
        self.assertEqual(
            _fs_root_phases(PATH_TO_ITEM['/']), list(dg.ordered_phases()),
        )
        self.assertIn(
            tuple(dg.gen_dependency_order_items('fake_subvol_path')),
            {
                tuple(PATH_TO_ITEM[p] for p in paths) for paths in [
                    # A few orders are valid, don't make the test fragile.
                    ['/a/b/c', '/a/b/c/F', '/a/d/e', '/a/d/e/G'],
                    ['/a/b/c', '/a/d/e', '/a/b/c/F', '/a/d/e/G'],
                    ['/a/b/c', '/a/d/e', '/a/d/e/G', '/a/b/c/F'],
                ]
            },
        )

    def test_cycle_detection(self):

        def requires_provides_directory_class(requires_dir, provides_dir):

            class RequiresProvidesDirectory(metaclass=ImageItem):
                def requires(self):
                    yield require_directory(requires_dir)

                def provides(self):
                    yield ProvidesDirectory(path=provides_dir)

            return RequiresProvidesDirectory

        # Everything works without a cycle
        first = FilesystemRootItem(from_target='')
        second = requires_provides_directory_class('/', 'a')(from_target='')
        third = MakeDirsItem(from_target='', into_dir='a', path_to_make='b/c')
        dg = DependencyGraph([second, first, third])
        self.assertEqual(_fs_root_phases(first), list(dg.ordered_phases()))
        self.assertEqual(
            [second, third],
            list(dg.gen_dependency_order_items('fake_subvol_path')),
        )

        # Let's change `second` to get a cycle
        dg = DependencyGraph([
            requires_provides_directory_class('a/b', 'a')(from_target=''),
            first, third,
        ])
        self.assertEqual(_fs_root_phases(first), list(dg.ordered_phases()))
        with self.assertRaisesRegex(AssertionError, '^Cycle in '):
            list(dg.gen_dependency_order_items('fake_subvol_path'))

    def test_phase_order(self):

        class FakeRemovePaths:
            get_phase_builder = 'kittycat'

            def phase_order(self):
                return PhaseOrder.REMOVE_PATHS

        first = FilesystemRootItem(from_target='')
        second = FakeRemovePaths()
        third = MakeDirsItem(from_target='', into_dir='/', path_to_make='a/b')
        dg = DependencyGraph([second, first, third])
        self.assertEqual(
            _fs_root_phases(first) + [
                (FakeRemovePaths.get_phase_builder, (second,)),
            ],
            list(dg.ordered_phases()),
        )
        # We had a phase other than PARENT_LAYER, so the dependency sorting
        # will need to inspect the resulting subvolume -- let it be empty.
        with TempSubvolumes(sys.argv[0]) as temp_subvolumes:
            subvol = temp_subvolumes.create('subvol')
            self.assertEqual([third], list(dg.gen_dependency_order_items(
                subvol.path().decode(),
            )))


if __name__ == '__main__':
    unittest.main()
