#!/usr/bin/env python3
import fcntl
import contextlib
import os
import unittest
import tempfile
import subvolume_garbage_collector as sgc


class SubvolumeGarbageCollectorTestCase(unittest.TestCase):

    def _restore_path(self):
        os.environ['PATH'] = self._old_path

    def setUp(self):
        # Mock out `sudo btrfs subvolume delete` for the garbage-collector,
        # so that the test doesn't require us to set up & clean up btrfs
        # volumes.  Everything else is easily tested in a tempdir.
        self._old_path = os.environ.pop('PATH', None)
        self.addCleanup(self._restore_path)
        fake_sudo_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 'fake_sudo/'
        )
        os.environ['PATH'] = f'{fake_sudo_path}:{self._old_path}'

    def _touch(self, *path):
        with open(os.path.join(*path), 'a'):
            pass

    def test_list_subvolumes(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual([], sgc.list_subvolumes(td))

            self._touch(td, 'ba:nana')  # Not a directory
            self.assertEqual([], sgc.list_subvolumes(td))

            os.mkdir(os.path.join(td, 'apple'))  # No colon
            self.assertEqual([], sgc.list_subvolumes(td))

            os.mkdir(os.path.join(td, 'p:i'))
            os.mkdir(os.path.join(td, 'e:'))
            os.mkdir(os.path.join(td, ':x'))
            self.assertEqual({'p:i', 'e:', ':x'}, set(sgc.list_subvolumes(td)))

    def test_list_refcounts(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual({}, dict(sgc.list_refcounts(td)))

            self._touch(td, 'foo:bar')  # No .json
            self._touch(td, 'borf.json')  # No :
            self.assertEqual({}, dict(sgc.list_refcounts(td)))

            banana_json = os.path.join(td, 'ba:nana.json')
            os.mkdir(banana_json)  # Not a file
            with self.assertRaisesRegex(RuntimeError, 'not a regular file'):
                dict(sgc.list_refcounts(td))
            os.rmdir(banana_json)

            self._touch(banana_json)  # This is a real refcount file now
            self.assertEqual({'ba:nana': 1}, dict(sgc.list_refcounts(td)))

            # This is pathological, but it doesn't seem worth detecting.
            os.link(banana_json, os.path.join(td, 'ap:ple.json'))
            self.assertEqual(
                {'ba:nana': 2, 'ap:ple': 2}, dict(sgc.list_refcounts(td))
            )

            os.unlink(banana_json)
            self.assertEqual({'ap:ple': 1}, dict(sgc.list_refcounts(td)))

    # Not bothering with a direct test for `parse_args` because (a) it is
    # entirely argparse declarations, and that module has decent validation,
    # (b) we test it indirectly in `test_has_new_subvolume` and others.

    def test_has_new_subvolume(self):

        # Instead of creating a fake namespace, actually parse some args
        def name_ver_json(name, ver, json):
            return sgc.parse_args([
                '--refcounts-dir', 'fake',
                '--subvolumes-dir', 'fake',
                '--new-subvolume-name', name,
                '--new-subvolume-version', ver,
                '--new-subvolume-json', json,
            ])

        self.assertFalse(sgc.has_new_subvolume(name_ver_json(*[None] * 3)))
        self.assertTrue(sgc.has_new_subvolume(name_ver_json('x', 'y', 'z')))

        for bad_example in [
            ('x', 'y', None),
            ('x', None, 'z'),
            (None, 'y', 'z'),
            (None, None, 'z'),
            (None, 'y', None),
            ('x', None, None),
        ]:
            with self.assertRaisesRegex(
                RuntimeError, 'pass all 3 .* or pass none'
            ):
                sgc.has_new_subvolume(name_ver_json(*bad_example))

    @contextlib.contextmanager
    def _gc_test_case(self):
        # NB: I'm too lazy to test that `refs_dir` is created if missing.
        with tempfile.TemporaryDirectory() as refs_dir, \
             tempfile.TemporaryDirectory() as subs_dir:

            # Track subvolumes + refcounts that will get garbage-collected
            # separately from those that won't.
            gcd_subs = set()
            kept_subs = set()
            gcd_refs = set()
            kept_refs = set()

            # Subvolume without a refcount
            os.mkdir(os.path.join(subs_dir, 'no:refs'))
            gcd_subs.add('no:refs')

            # Subvolume, whose refcount is 1
            self._touch(refs_dir, '1:link.json')
            os.mkdir(os.path.join(subs_dir, '1:link'))
            gcd_refs.add('1:link.json')
            gcd_subs.add('1:link')

            # Some refcount files with a link count of 2
            self._touch(refs_dir, '2link:1.json')
            os.link(
                os.path.join(refs_dir, '2link:1.json'),
                os.path.join(refs_dir, '2link:2.json'),
            )
            kept_refs.add('2link:1.json')
            kept_refs.add('2link:2.json')

            # Subvolumes for both of the 2-link refcount files
            os.mkdir(os.path.join(subs_dir, '2link:1'))
            os.mkdir(os.path.join(subs_dir, '2link:2'))
            kept_subs.add('2link:1')
            kept_subs.add('2link:2')

            # Some refcount files with a link count of 3
            three_link = os.path.join(refs_dir, '3link:1.json')
            self._touch(three_link)
            os.link(three_link, os.path.join(refs_dir, '3link:2.json'))
            os.link(three_link, os.path.join(refs_dir, '3link:3.json'))
            kept_refs.add('3link:1.json')
            kept_refs.add('3link:2.json')
            kept_refs.add('3link:3.json')

            # Make a subvolume for 1 of them, it won't get GC'd
            os.mkdir(os.path.join(subs_dir, '3link:2'))
            kept_subs.add('3link:2')

            self.assertEqual(kept_refs | gcd_refs, set(os.listdir(refs_dir)))
            self.assertEqual(kept_subs | gcd_subs, set(os.listdir(subs_dir)))

            yield sgc.argparse.Namespace(
                gcd_subs=gcd_subs,
                kept_subs=kept_subs,
                gcd_refs=gcd_refs,
                kept_refs=kept_refs,
                refs_dir=refs_dir,
                subs_dir=subs_dir,
            )

    def _gc_only(self, n):
        sgc.subvolume_garbage_collector([
            '--refcounts-dir', n.refs_dir,
            '--subvolumes-dir', n.subs_dir,
        ])

    def test_garbage_collect_subvolumes(self):
        for fn in [
            lambda n: sgc.garbage_collect_subvolumes(n.refs_dir, n.subs_dir),
            self._gc_only,
        ]:
            with self._gc_test_case() as n:
                fn(n)
                self.assertEqual(n.kept_refs, set(os.listdir(n.refs_dir)))
                self.assertEqual(n.kept_subs, set(os.listdir(n.subs_dir)))

    def test_no_gc_due_to_lock(self):
        with self._gc_test_case() as n:
            fd = os.open(n.subs_dir, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                self._gc_only(n)

                # Sneak in a test that new subvolume creation fails when
                # its refcount already exists.
                with tempfile.TemporaryDirectory() as json_dir, \
                     self.assertRaisesRegex(
                         RuntimeError, 'Refcount already exists:',
                     ):
                    sgc.subvolume_garbage_collector([
                        '--refcounts-dir', n.refs_dir,
                        '--subvolumes-dir', n.subs_dir,
                        # This refcount was created by `_gc_test_case`.
                        '--new-subvolume-name', '3link',
                        '--new-subvolume-version', '1',
                        '--new-subvolume-json', os.path.join(json_dir, 'OUT'),
                    ])

            finally:
                    os.close(fd)

            self.assertEqual(
                n.kept_refs | n.gcd_refs, set(os.listdir(n.refs_dir))
            )
            self.assertEqual(
                n.kept_subs | n.gcd_subs, set(os.listdir(n.subs_dir))
            )

    def test_garbage_collect_and_make_new_subvolume(self):
        with self._gc_test_case() as n, \
             tempfile.TemporaryDirectory() as json_dir:
            sgc.subvolume_garbage_collector([
                '--refcounts-dir', n.refs_dir,
                '--subvolumes-dir', n.subs_dir,
                '--new-subvolume-name', 'new',
                '--new-subvolume-version', 'subvol',
                '--new-subvolume-json', os.path.join(json_dir, 'OUT'),
            ])
            self.assertEqual(['OUT'], os.listdir(json_dir))
            self.assertEqual(
                n.kept_refs | {'new:subvol.json'}, set(os.listdir(n.refs_dir)),
            )
            self.assertEqual(n.kept_subs, set(os.listdir(n.subs_dir)))
