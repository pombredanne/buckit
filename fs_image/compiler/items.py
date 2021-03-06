#!/usr/bin/env python3
'''
The classes produced by ImageItem are the various types of items that can be
installed into an image.  The compiler will verify that the specified items
have all of their requirements satisfied, and will then apply them in
dependency order.

To understand how the methods `provides()` and `requires()` affect
dependency resolution / installation order, start with the docblock at the
top of `provides.py`.
'''
import enum
import hashlib
import itertools
import json
import os
import subprocess
import tempfile
import sys

from typing import Iterable, List, Mapping, NamedTuple, Optional, Set

from . import mount_item
from . import procfs_serde

from .enriched_namedtuple import (
    metaclass_new_enriched_namedtuple, NonConstructibleField,
)
from .provides import ProvidesDirectory, ProvidesDoNotAccess, ProvidesFile
from .requires import require_directory, require_file
from .subvolume_on_disk import SubvolumeOnDisk

from common import nullcontext
from subvol_utils import Subvol
from artifacts_dir import find_repo_root

# This path is off-limits to regular image operations, it exists only to
# record image metadata and configuration.  This is at the root, instead of
# in `etc` because that means that `FilesystemRoot` does not have to provide
# `etc` and determine its permissions.  In other words, a top-level "meta"
# directory makes the compiler less opinionated about other image content.
#
# NB: The trailing slash is significant, making this a protected directory,
# not a protected file.
META_DIR = 'meta/'

@enum.unique
class PhaseOrder(enum.Enum):
    '''
    With respect to ordering, there are two types of operations that the
    image compiler performs against images.

    (1) Regular additive operations are naturally ordered with respect to
        one another by filesystem dependencies.  For example: we must create
        /usr/bin **BEFORE** copying `:your-tool` there.

    (2) Everything else, including:
         - RPM installation, which has a complex internal ordering, but
           simply needs needs a definitive placement as a block of `yum`
           operations -- due to `yum`'s complexity & various scripts, it's
           not desirable to treat installs as regular additive operations.
         - Path removals.  It is simplest to perform them in bulk, without
           interleaving with other operations.  Removals have a natural
           ordering with respect to each other -- child before parent, to
           avoid tripping "assert_exists" unnecessarily.

    For the operations in (2), this class sets a justifiable deteriminstic
    ordering for black-box blocks of operations, and assumes that each
    individual block's implementation will order its internals sensibly.

    Phases will be executed in the order listed here.

    The operations in (1) are validated, dependency-sorted, and built after
    all of the phases have built.

    IMPORTANT: A new phase implementation MUST:
      - handle pre-existing protected paths via `_protected_path_set`
      - emit `ProvidesDoNotAccess` if it provides new protected paths
      - ensure that `_protected_path_set` in future phases knows how to
        discover these protected paths by inspecting the filesystem.
    See `ParentLayerItem`, `RemovePathsItem`, and `MountItem` for examples.

    Future: the complexity around protected paths is a symptom of a lack of
    a strong runtime abstraction.  Specifically, if `Subvol.run_as_root`
    used mount namespaces and read-only bind mounts to enforce protected
    paths (as is done today in `yum-from-snapshot`), then it would not be
    necessary for the compiler to know about them.
    '''
    # This actually creates the subvolume, so it must preced all others.
    PARENT_LAYER = enum.auto()
    # Precedes REMOVE_PATHS because RPM removes **might** be conditional on
    # the presence or absence of files, and we don't want that extra entropy
    # -- whereas file removes fail or succeed predictably.  Precedes
    # RPM_INSTALL somewhat arbitrarily, since _gen_multi_rpm_actions
    # prevents install-remove conflicts between features.
    RPM_REMOVE = enum.auto()
    RPM_INSTALL = enum.auto()
    # This MUST be a separate phase that comes after all the regular items
    # because the dependency sorter has no provisions for eliminating
    # something that another item `provides()`.
    #
    # By having this phase be last, we also allow removing files added by
    # RPM_INSTALL.  The downside is that this is a footgun.  The upside is
    # that e.g.  cleaning up yum log & caches can be done as an
    # `image_feature` instead of being code.  We might also use this to
    # remove e.g.  unnecessary parts of excessively monolithic RPMs.
    REMOVE_PATHS = enum.auto()


class LayerOpts(NamedTuple):
    layer_target: str
    yum_from_snapshot: str
    build_appliance: str


class ImageItem(type):
    'A metaclass for the types of items that can be installed into images.'
    def __new__(metacls, classname, bases, dct):

        # Future: `deepfrozen` has a less clunky way of doing this
        def customize_fields(kwargs):
            fn = dct.get('customize_fields')
            if fn:
                fn(kwargs)
            return kwargs

        # Some items, like RPM actions, are not sorted by dependencies, but
        # get a fixed installation order.  The absence of a phase means the
        # item is ordered via the topo-sort in `dep_graph.py`.
        class PhaseOrderBase:
            __slots__ = ()

            def phase_order(self):
                return None

        return metaclass_new_enriched_namedtuple(
            __class__,
            ['from_target'],
            metacls, classname, (PhaseOrderBase,) + bases, dct,
            customize_fields
        )


def _make_path_normal_relative(orig_d: str) -> str:
    '''
    In image-building, we want relative paths that do not start with `..`,
    so that the effects of ImageItems are confined to their destination
    paths. For convenience, we accept absolute paths, too.
    '''
    # lstrip so we treat absolute paths as image-relative
    d = os.path.normpath(orig_d).lstrip('/')
    if d == '..' or d.startswith('../'):
        raise AssertionError(f'path {orig_d} cannot start with ../')
    # This is a directory reserved for image build metadata, so we prevent
    # regular items from writing to it. `d` is never absolute here.
    # NB: This check is redundant with `ProvidesDoNotAccess(path=META_DIR)`,
    # this is just here as a fail-fast backup.
    if (d + '/').startswith(META_DIR):
        raise AssertionError(f'path {orig_d} cannot start with {META_DIR}')
    return d


def _coerce_path_field_normal_relative(kwargs, field: str):
    d = kwargs.get(field)
    if d is not None:
        kwargs[field] = _make_path_normal_relative(d)


def _make_rsync_style_dest_path(dest: str, source: str) -> str:
    '''
    rsync convention for a destination: "ends/in/slash/" means "copy
    into this directory", "does/not/end/with/slash" means "copy with
    the specified filename".
    '''

    # Normalize after applying the rsync convention, since this would
    # remove any trailing / in 'dest'.
    return _make_path_normal_relative(
        os.path.join(dest,
            os.path.basename(source)) if dest.endswith('/') else dest
    )


def _maybe_popen_zstd(path):
    'Use this as a context manager.'
    if path.endswith('.zst'):
        return subprocess.Popen([
            'zstd', '--decompress', '--stdout', path,
        ], stdout=subprocess.PIPE)
    return nullcontext()


def _open_tarfile(path):
    'Wraps tarfile.open to add .zst support. Use this as a context manager.'
    import tarfile  # Lazy since only this method needs it.
    with _maybe_popen_zstd(path) as maybe_proc:
        if maybe_proc is None:
            return tarfile.open(path)
        else:
            return tarfile.open(fileobj=maybe_proc.stdout, mode='r|')


def _hash_tarball(tarball: str, algorithm: str) -> str:
    'Returns the hex digest'
    algo = hashlib.new(algorithm)
    with open(tarball, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            algo.update(chunk)
    return algo.hexdigest()


class TarballItem(metaclass=ImageItem):
    fields = ['into_dir', 'tarball', 'hash', 'force_root_ownership']

    def customize_fields(kwargs):  # noqa: B902
        algorithm, expected_hash = kwargs['hash'].split(':')
        actual_hash = _hash_tarball(kwargs['tarball'], algorithm)
        if actual_hash != expected_hash:
            raise AssertionError(
                f'{kwargs} failed hash validation, got {actual_hash}'
            )
        _coerce_path_field_normal_relative(kwargs, 'into_dir')
        assert kwargs['force_root_ownership'] in [True, False], kwargs

    def provides(self):
        with _open_tarfile(self.tarball) as f:
            for item in f:
                path = os.path.join(
                    self.into_dir, _make_path_normal_relative(item.name),
                )
                if item.isdir():
                    # We do NOT provide the installation directory, and the
                    # image build script tarball extractor takes pains (e.g.
                    # `tar --no-overwrite-dir`) not to touch the extraction
                    # directory.
                    if os.path.normpath(
                        os.path.relpath(path, self.into_dir)
                    ) != '.':
                        yield ProvidesDirectory(path=path)
                else:
                    yield ProvidesFile(path=path)

    def requires(self):
        yield require_directory(self.into_dir)

    def build(self, subvol: Subvol):
        with _maybe_popen_zstd(self.tarball) as maybe_proc:
            subvol.run_as_root([
                'tar',
                # Future: Bug: `tar` unfortunately FOLLOWS existing symlinks
                # when unpacking.  This isn't dire because the compiler's
                # conflict prevention SHOULD prevent us from going out of
                # the subvolume since this TarballItem's provides would
                # collide with whatever is already present.  However, it's
                # hard to state that with complete confidence, especially if
                # we start adding support for following directory symlinks.
                '-C', subvol.path(self.into_dir),
                '-x',
                # Block tar's weird handling of paths containing colons.
                '--force-local',
                # The uid:gid doing the extraction is root:root, so by default
                # tar would try to restore the file ownership from the archive.
                # In some cases, we just want all the files to be root-owned.
                *(['--no-same-owner'] if self.force_root_ownership else []),
                # The next option is an extra safeguard that is redundant
                # with the compiler's prevention of `provides` conflicts.
                # It has two consequences:
                #
                #  (1) If a file already exists, `tar` will fail with an error.
                #      It is **not** an error if a directory already exists --
                #      otherwise, one would never be able to safely untar
                #      something into e.g. `/usr/local/bin`.
                #
                #  (2) Less obviously, the option prevents `tar` from
                #      overwriting the permissions of `directory`, as it
                #      otherwise would.
                #
                #      Thanks to the compiler's conflict detection, this should
                #      not come up, but now you know.  Observe us clobber the
                #      permissions without it:
                #
                #        $ mkdir IN OUT
                #        $ touch IN/file
                #        $ chmod og-rwx IN
                #        $ ls -ld IN OUT
                #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
                #        drwxr-xr-x. 2 lesha users  6 Sep 11 21:50 OUT
                #        $ tar -C IN -czf file.tgz .
                #        $ tar -C OUT -xvf file.tgz
                #        ./
                #        ./file
                #        $ ls -ld IN OUT
                #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
                #        drwx------. 2 lesha users 17 Sep 11 21:50 OUT
                #
                #      Adding `--keep-old-files` preserves `OUT`'s metadata:
                #
                #        $ rm -rf OUT ; mkdir out ; ls -ld OUT
                #        drwxr-xr-x. 2 lesha users 6 Sep 11 21:53 OUT
                #        $ tar -C OUT --keep-old-files -xvf file.tgz
                #        ./
                #        ./file
                #        $ ls -ld IN OUT
                #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
                #        drwxr-xr-x. 2 lesha users 17 Sep 11 21:54 OUT
                '--keep-old-files',
                '-f', ('-' if maybe_proc else self.tarball),
            ], stdin=(maybe_proc.stdout if maybe_proc else None))


def _generate_tarball(
    temp_dir: str, generator: bytes, generator_args: List[str],
) -> str:
    # API design notes:
    #
    #  1) The generator takes an output directory, not a file, because we
    #     prefer not to have to hardcode the extension of the output file in
    #     the TARGETS file -- that would make it more laborious to change
    #     the compression format.  Instead, the generator prints the path to
    #     the created tarball to stdout.  This does not introduce
    #     nondeterminism, since the tarball name cannot affect the result of
    #     its extraction.
    #
    #     Since TARGETS already hardcodes a content hash, requiring the name
    #     would not be far-fetched, this approach just seemed cleaner.
    #
    #  2) `temp_dir` is last since this allows the use of inline scripts via
    #     `generator_args` with e.g. `/bin/bash`.
    #
    # Future: it would be best to sandbox the generator to limit its
    # filesystem writes.  At the moment, we trust rule authors not to abuse
    # this feature and write stuff outside the given directory.
    tarball_filename = subprocess.check_output([
        generator, *generator_args, temp_dir,
    ]).decode()
    assert tarball_filename.endswith('\n'), (generator, tarball_filename)
    tarball_filename = os.path.normpath(tarball_filename[:-1])
    assert (
        not tarball_filename.startswith('/')
        and not tarball_filename.startswith('../')
    ), tarball_filename
    return os.path.join(temp_dir, tarball_filename)


def tarball_item_factory(
    exit_stack, *, generator: str = None, tarball: str = None,
    generator_args: List[str] = None, **kwargs,
):
    assert (generator is not None) ^ (tarball is not None)
    # Uses `generator` to generate a temporary `tarball` for `TarballItem`.
    # The file is deleted when the `exit_stack` context exits.
    #
    # NB: With `generator`, identical constructor arguments to this factory
    # will create different `TarballItem`s, so if we needed item
    # deduplication to work across inputs, this is broken.  However, I don't
    # believe the compiler relies on that.  If we need it, it should not be
    # too hard to share the same tarball for all generates with the same
    # command -- you'd add a global map of ('into_dir', 'command') ->
    # tarball, perhaps using weakref hooks to refcount tarballs and GC them.
    if generator:
        tarball = _generate_tarball(
            exit_stack.enter_context(tempfile.TemporaryDirectory()),
            generator,
            generator_args or [],
        )
    return TarballItem(**kwargs, tarball=tarball)


class HasStatOptions:
    '''
    Helper for setting `stat (2)` options on files, directories, etc, which
    we are creating inside the image.  Interfaces with `StatOptions` in the
    image build tool.
    '''
    __slots__ = ()
    # `mode` can be an integer fully specifying the bits, or a symbolic
    # string like `u+rx`.  In the latter case, the changes are applied on
    # top of mode 0.
    #
    # The defaut mode 0755 is good for directories, and OK for files.  I'm
    # not trying adding logic to vary the default here, since this really
    # only comes up in tests, and `image_feature` usage should set this
    # explicitly.
    fields = [('mode', 0o755), ('user', 'root'), ('group', 'root')]

    def _mode_impl(self):
        return (  # The symbolic mode must be applied after 0ing all bits.
            f'{self.mode:04o}' if isinstance(self.mode, int)
                else f'a-rwxXst,{self.mode}'
        )

    def build_stat_options(self, subvol: Subvol, full_target_path: str):
        # `chmod` lacks a --no-dereference flag to protect us from following
        # `full_target_path` if it's a symlink.  As far as I know, this
        # should never occur, so just let the exception fly.
        subvol.run_as_root(['test', '!', '-L', full_target_path])
        # -R is not a problem since it cannot be the case that we are
        # creating a directory that already has something inside it.  On
        # the plus side, it helps with nested directory creation.
        subvol.run_as_root([
            'chmod', '-R', self._mode_impl(),
            full_target_path
        ])
        subvol.run_as_root([
            'chown', '--no-dereference', '-R', f'{self.user}:{self.group}',
            full_target_path,
        ])


class CopyFileItem(HasStatOptions, metaclass=ImageItem):
    fields = ['source', 'dest']

    def customize_fields(kwargs):  # noqa: B902
        kwargs['dest'] = _make_rsync_style_dest_path(
            kwargs['dest'], kwargs['source']
        )

    def provides(self):
        yield ProvidesFile(path=self.dest)

    def requires(self):
        yield require_directory(os.path.dirname(self.dest))

    def build(self, subvol: Subvol):
        dest = subvol.path(self.dest)
        subvol.run_as_root(['cp', self.source, dest])
        self.build_stat_options(subvol, dest)


class SymlinkItem(HasStatOptions):
    fields = ['source', 'dest']

    def _customize_fields_impl(kwargs):  # noqa: B902
        _coerce_path_field_normal_relative(kwargs, 'source')

        kwargs['dest'] = _make_rsync_style_dest_path(
            kwargs['dest'], kwargs['source']
        )

    def build(self, subvol: Subvol):
        dest = subvol.path(self.dest)
        # Source is always absolute inside the image subvolume
        source = os.path.join('/', self.source)
        subvol.run_as_root(
            ['ln', '--symbolic', '--no-dereference', source, dest]
        )


class SymlinkToDirItem(SymlinkItem, metaclass=ImageItem):
    customize_fields = SymlinkItem._customize_fields_impl

    def provides(self):
        yield ProvidesDirectory(path=self.dest)

    def requires(self):
        yield require_directory(self.source)
        yield require_directory(os.path.dirname(self.dest))


class SymlinkToFileItem(SymlinkItem, metaclass=ImageItem):
    customize_fields = SymlinkItem._customize_fields_impl

    def provides(self):
        yield ProvidesFile(path=self.dest)

    def requires(self):
        yield require_file(self.source)
        yield require_directory(os.path.dirname(self.dest))


class MakeDirsItem(HasStatOptions, metaclass=ImageItem):
    fields = ['into_dir', 'path_to_make']

    def customize_fields(kwargs):  # noqa: B902
        _coerce_path_field_normal_relative(kwargs, 'into_dir')
        _coerce_path_field_normal_relative(kwargs, 'path_to_make')

    def provides(self):
        inner_dir = os.path.join(self.into_dir, self.path_to_make)
        while inner_dir != self.into_dir:
            yield ProvidesDirectory(path=inner_dir)
            inner_dir = os.path.dirname(inner_dir)

    def requires(self):
        yield require_directory(self.into_dir)

    def build(self, subvol: Subvol):
        outer_dir = self.path_to_make.split('/', 1)[0]
        inner_dir = subvol.path(os.path.join(self.into_dir, self.path_to_make))
        subvol.run_as_root(['mkdir', '-p', inner_dir])
        self.build_stat_options(
            subvol, subvol.path(os.path.join(self.into_dir, outer_dir)),
        )


# NB: When we split `items.py`, this can just be merged with `mount_item.py`.
class MountItem(metaclass=ImageItem):
    fields = [
        'mountpoint',
        ('build_source', NonConstructibleField),
        ('runtime_source', NonConstructibleField),
        ('is_directory', NonConstructibleField),
        ('is_repo_root', NonConstructibleField),
        # The next two are always None, their content moves into the above
        # `NonConstructibleField`s
        'target',
        'mount_config',
    ]

    def customize_fields(kwargs):  # noqa: B902
        target = kwargs.pop('target')
        cfg = kwargs.pop('mount_config')
        assert (target is None) ^ (cfg is None), \
            f'Exactly one of `target` or `mount_config` must be set in {kwargs}'
        if cfg is not None:
            cfg = cfg.copy()  # We must not mutate our input!
        else:
            with open(os.path.join(target, 'mountconfig.json')) as f:
                cfg = json.load(f)

        kwargs['is_repo_root'] = cfg.pop('is_repo_root', False)
        default_mountpoint = cfg.pop('default_mountpoint', None)

        mountpoint = kwargs.get('mountpoint')
        if kwargs['is_repo_root']:
            assert default_mountpoint is None, (f'default_mountpoint: '
                                                '{default_mountpoint} '
                                                'must not be set')
            assert mountpoint is None, (f'mountpoint: {mountpoint} '
                                        'must not be set')
            kwargs['mountpoint'] = find_repo_root(sys.argv[0])
            assert kwargs['mountpoint'][0] == '/', (f'repo_root: '
                                                    '{kwargs["mountpoint"]} '
                                                    'must start from /')
            kwargs['mountpoint'] = kwargs['mountpoint'][1:]
        else:
            if mountpoint is None:  # Missing or None => use default
                kwargs['mountpoint'] = default_mountpoint
                if kwargs['mountpoint'] is None:
                    raise AssertionError(f'MountItem {kwargs} lacks mountpoint')
        _coerce_path_field_normal_relative(kwargs, 'mountpoint')

        kwargs['is_directory'] = cfg.pop('is_directory')
        assert (kwargs['is_directory'] or
                not kwargs['is_repo_root']), f'cannot host_file_mount repo_root'

        build_source = cfg.pop('build_source')
        if kwargs['is_repo_root']:
            assert build_source['source'] is None, (f'source: '
                                                    '{build_source["source"]} '
                                                    'must not be set')
            build_source['source'] = os.path.join('/', kwargs['mountpoint'])

        kwargs['build_source'] = mount_item.BuildSource(
            **build_source
        )
        if kwargs['build_source'].type == 'host' and not (
            kwargs['from_target'].startswith('//fs_image/features/host_mounts')
            or kwargs['from_target'].startswith('//fs_image/compiler/test')
            or kwargs['from_target'].startswith('//fs_image/build_appliance')
        ):
            raise AssertionError(
                'Host mounts cause containers to be non-hermetic and fragile, '
                'so they must be located under `fs_image/features/host_mounts` '
                'to enable close review by the owners of `fs_image`.'
            )

        # This is supposed to be the run-time equivalent of `build_source`,
        # but for us it's just an opaque JSON blob that the runtime wants.
        # Hack: We serialize this back to JSON since the compiler expects
        # items to be hashable, and the source WILL contain dicts.
        runtime_source = cfg.pop('runtime_source', None)
        # Future: once runtime_source grows a schema, use it here?
        if (runtime_source and runtime_source.get('type') == 'host'):
            raise AssertionError(
                f'Only `build_source` may specify host mounts: {kwargs}'
            )
        kwargs['runtime_source'] = json.dumps(runtime_source, sort_keys=True)

        assert cfg == {}, f'Unparsed fields in {kwargs} mount_config: {cfg}'
        # These must be set to appease enriched_namedtuple
        kwargs['target'] = None
        kwargs['mount_config'] = None

    def provides(self):
        # For now, nesting of mounts is not supported, and we certainly
        # cannot allow regular items to write inside a mount.
        yield ProvidesDoNotAccess(path=self.mountpoint)

    def requires(self):
        # We don't require the mountpoint itself since it will be shadowed,
        # so this item just makes it with default permissions.
        yield require_directory(os.path.dirname(self.mountpoint))

    def build_resolves_targets(
        self, *,
        subvol: Subvol,
        target_to_path: Mapping[str, str],
        subvolumes_dir: str,
    ):
        mount_dir = os.path.join(
            mount_item.META_MOUNTS_DIR, self.mountpoint, mount_item.MOUNT_MARKER
        )
        for name, data in (
            # NB: Not exporting self.mountpoint since it's implicit in the path.
            ('is_directory', self.is_directory),
            ('build_source', self.build_source._asdict()),
            ('runtime_source', json.loads(self.runtime_source)),
        ):
            procfs_serde.serialize(data, subvol, os.path.join(mount_dir, name))
        source_path = self.build_source.to_path(
            target_to_path=target_to_path,
            subvolumes_dir=subvolumes_dir,
        )
        # Support mounting directories and non-directories...  This check
        # follows symlinks for the mount source, which seems correct.
        is_dir = os.path.isdir(source_path)
        assert is_dir == self.is_directory, self
        if is_dir:
            mkdir_opts = ['--mode=0755']
            if self.is_repo_root:
                mkdir_opts.append('-p')
            # NB: if is_repo_root, mkdir below will create a non-portable dir
            # like /home/username/fbsource/fbcode in subvol layer, but such
            #  a layer should never be published as a package.
            subvol.run_as_root([
                'mkdir', *mkdir_opts, subvol.path(self.mountpoint)
            ])
        else:  # Regular files, device nodes, FIFOs, you name it.
            # `touch` lacks a `--mode` argument, but the mode of this
            # mountpoint will be shadowed anyway, so let it be whatever.
            subvol.run_as_root(['touch', subvol.path(self.mountpoint)])
        mount_item.ro_rbind_mount(source_path, subvol, self.mountpoint)


def _protected_path_set(subvol: Optional[Subvol]) -> Set[str]:
    '''
    Identifies the protected paths in a subvolume.  Pass `subvol=None` if
    the subvolume doesn't yet exist (for FilesystemRoot).

    All paths will be relative to the image root, no leading /.  If a path
    has a trailing /, it is a protected directory, otherwise it is a
    protected file.

    Future: The trailing / convention could be eliminated, since any place
    actually manipulating these paths can inspect what's on disk, and act
    appropriately.  If the convention proves burdensome, this is an easy
    change -- mostly affecting this file, and `yum_from_snapshot.py`.
    '''
    paths = {META_DIR}
    if subvol is not None:
        # NB: The returned paths here already follow the trailing / rule.
        for mountpoint in mount_item.mountpoints_from_subvol_meta(subvol):
            paths.add(mountpoint.lstrip('/'))
    # Never absolute: yum-from-snapshot interprets absolute paths as host paths
    assert not any(p.startswith('/') for p in paths), paths
    return paths


def _is_path_protected(path: str, protected_paths: Set[str]) -> bool:
    # NB: The O-complexity could obviously be lots better, if needed.
    for prot_path in protected_paths:
        # Handle both protected files and directories.  This test is written
        # to return True even if `prot_path` is `/path/to/file` while `path`
        # is `/path/to/file/oops`.
        if (path + '/').startswith(
            prot_path + ('' if prot_path.endswith('/') else '/')
        ):
            return True
    return False


def _ensure_meta_dir_exists(subvol: Subvol):
    subvol.run_as_root([
        'mkdir', '--mode=0755', '--parents', subvol.path(META_DIR),
    ])


class ParentLayerItem(metaclass=ImageItem):
    fields = ['path']

    def phase_order(self):
        return PhaseOrder.PARENT_LAYER

    def provides(self):
        parent_subvol = Subvol(self.path, already_exists=True)

        protected_paths = _protected_path_set(parent_subvol)
        for prot_path in protected_paths:
            yield ProvidesDoNotAccess(path=prot_path)

        provided_root = False
        # We need to traverse the parent image as root, so that we have
        # permission to access everything.
        for type_and_path in parent_subvol.run_as_root([
            # -P is the analog of --no-dereference in GNU tools
            #
            # Filter out the protected paths at traversal time.  If one of
            # the paths has a very large or very slow mount, traversing it
            # would have a devastating effect on build times, so let's avoid
            # looking inside protected paths entirely.  An alternative would
            # be to `send` and to parse the sendstream, but this is ok too.
            'find', '-P', self.path, '(', *itertools.dropwhile(
                lambda x: x == '-o',  # Drop the initial `-o`
                itertools.chain.from_iterable([
                    # `normpath` removes the trailing / for protected dirs
                    '-o', '-path', os.path.join(self.path, os.path.normpath(p))
                ] for p in protected_paths),
            ), ')', '-prune', '-o', '-printf', '%y %p\\0',
        ], stdout=subprocess.PIPE).stdout.split(b'\0'):
            if not type_and_path:  # after the trailing \0
                continue
            filetype, abspath = type_and_path.decode().split(' ', 1)
            relpath = os.path.relpath(abspath, self.path)

            # We already "provided" this path above, and it should have been
            # filtered out by `find`.
            assert not _is_path_protected(relpath, protected_paths), relpath

            # Future: This provides all symlinks as files, while we should
            # probably provide symlinks to valid directories inside the
            # image as directories to be consistent with SymlinkToDirItem.
            if filetype in ['b', 'c', 'p', 'f', 'l', 's']:
                yield ProvidesFile(path=relpath)
            elif filetype == 'd':
                yield ProvidesDirectory(path=relpath)
            else:  # pragma: no cover
                raise AssertionError(f'Unknown {filetype} for {abspath}')
            if relpath == '.':
                assert filetype == 'd'
                provided_root = True

        assert provided_root, 'parent layer {} lacks /'.format(self.path)

    def requires(self):
        return ()

    @classmethod
    def get_phase_builder(
        cls, items: Iterable['ParentLayerItem'], layer_opts: LayerOpts,
    ):
        parent, = items
        assert isinstance(parent, ParentLayerItem), parent

        def builder(subvol: Subvol):
            parent_subvol = Subvol(parent.path, already_exists=True)
            subvol.snapshot(parent_subvol)
            # This assumes that the parent has everything mounted already.
            mount_item.clone_mounts(parent_subvol, subvol)
            _ensure_meta_dir_exists(subvol)

        return builder


class FilesystemRootItem(metaclass=ImageItem):
    'A simple item to endow parent-less layers with a standard-permissions /'
    fields = []

    def phase_order(self):
        return PhaseOrder.PARENT_LAYER

    def provides(self):
        yield ProvidesDirectory(path='/')
        for p in _protected_path_set(subvol=None):
            yield ProvidesDoNotAccess(path=p)

    def requires(self):
        return ()

    @classmethod
    def get_phase_builder(
        cls, items: Iterable['FilesystemRootItem'], layer_opts: LayerOpts,
    ):
        parent, = items
        assert isinstance(parent, FilesystemRootItem), parent

        def builder(subvol: Subvol):
            subvol.create()
            # Guarantee standard / permissions.  This could be a setting,
            # but in practice, probably any other choice would be wrong.
            subvol.run_as_root(['chmod', '0755', subvol.path()])
            subvol.run_as_root(['chown', 'root:root', subvol.path()])
            _ensure_meta_dir_exists(subvol)

        return builder


def gen_parent_layer_items(target, parent_layer_json, subvolumes_dir):
    if not parent_layer_json:
        yield FilesystemRootItem(from_target=target)  # just provides /
    else:
        with open(parent_layer_json) as infile:
            yield ParentLayerItem(
                from_target=target,
                path=SubvolumeOnDisk.from_json_file(infile, subvolumes_dir)
                    .subvolume_path(),
            )


class RemovePathAction(enum.Enum):
    assert_exists = 'assert_exists'
    if_exists = 'if_exists'


class RemovePathItem(metaclass=ImageItem):
    fields = ['path', 'action']

    def customize_fields(kwargs):  # noqa: B902
        _coerce_path_field_normal_relative(kwargs, 'path')
        kwargs['action'] = RemovePathAction(kwargs['action'])

    def phase_order(self):
        return PhaseOrder.REMOVE_PATHS

    def __sort_key(self):
        return (self.path, {action: idx for idx, action in enumerate([
            # We sort in reverse order, so by putting "if" first we allow
            # conflicts between "if_exists" and "assert_exists" items to be
            # resolved naturally.
            RemovePathAction.if_exists,
            RemovePathAction.assert_exists,
        ])}[self.action])

    @classmethod
    def get_phase_builder(
        cls, items: Iterable['RemovePathItem'], layer_opts: LayerOpts,
    ):
        # NB: We want `remove_paths` not to be able to remove additions by
        # regular (non-phase) items in the same layer -- that indicates
        # poorly designed `image.feature`s, which should be refactored.  At
        # present, this is only enforced implicitly, because all removes are
        # done before regular items are even validated or sorted.  Enforcing
        # it explicitly is possible by peeking at `DependencyGraph.items`,
        # but the extra complexity doesn't seem worth the faster failure.

        # NB: We could detect collisions between two `assert_exists` removes
        # early, but again, it doesn't seem worth the complexity.

        def builder(subvol: Subvol):
            protected_paths = _protected_path_set(subvol)
            # Reverse-lexicographic order deletes inner paths before
            # deleting the outer paths, thus minimizing conflicts between
            # `remove_paths` items.
            for item in sorted(
                items, reverse=True, key=lambda i: i.__sort_key(),
            ):
                if _is_path_protected(item.path, protected_paths):
                    # For META_DIR, this is never reached because of
                    # _make_path_normal_relative's check, but for other
                    # protected paths, this is required.
                    raise AssertionError(
                        f'Cannot remove protected {item}: {protected_paths}'
                    )
                # This ensures that there are no symlinks in item.path that
                # might take us outside of the subvolume.  Since recursive
                # `rm` does not follow symlinks, it is OK if the inode at
                # `item.path` is a symlink (or one of its sub-paths).
                path = subvol.path(item.path, no_dereference_leaf=True)
                if not os.path.lexists(path):
                    if item.action == RemovePathAction.assert_exists:
                        raise AssertionError(f'Path does not exist: {item}')
                    elif item.action == RemovePathAction.if_exists:
                        continue
                    else:  # pragma: no cover
                        raise AssertionError(f'Unknown {item.action}')
                subvol.run_as_root([
                    'rm', '-r',
                    # This prevents us from making removes outside of the
                    # per-repo loopback, which is an important safeguard.
                    # It does not stop us from reaching into other subvols,
                    # but since those have random IDs in the path, this is
                    # nearly impossible to do by accident.
                    '--one-file-system',
                    path,
                ])
            pass

        return builder


class RpmAction(enum.Enum):
    install = 'install'
    # It would be sensible to have a 'remove' that fails if the package is
    # not already installed, but `yum` doesn't seem to support that, and
    # implementing it manually is a hassle.
    remove_if_exists = 'remove_if_exists'


RPM_ACTION_TYPE_TO_YUM_CMD = {
    # We do NOT want people specifying package versions, releases, or
    # architectures via `image_feature`s.  That would be a sure-fire way to
    # get version conflicts.  For the cases where we need version pinning,
    # we'll add a per-layer "version picker" concept.
    RpmAction.install: 'install-n',
    # The way `yum` works, this is a no-op if the package is missing.
    RpmAction.remove_if_exists: 'remove-n',
}


# These items are part of a phase, so they don't get dependency-sorted, so
# there is no `requires()` or `provides()` or `build()` method.
class RpmActionItem(metaclass=ImageItem):
    fields = ['name', 'action']

    def customize_fields(kwargs):  # noqa: B902
        kwargs['action'] = RpmAction(kwargs['action'])

    def phase_order(self):
        return {
            RpmAction.install: PhaseOrder.RPM_INSTALL,
            RpmAction.remove_if_exists: PhaseOrder.RPM_REMOVE,
        }[self.action]

    @classmethod
    def get_phase_builder(
        cls, items: Iterable['RpmActionItem'], layer_opts: LayerOpts,
    ):
        # Do as much validation as possible outside of the builder to give
        # fast feedback to the user.
        assert (layer_opts.yum_from_snapshot is not None or
                layer_opts.build_appliance is not None), (
            f'`image_layer` {layer_opts.layer_target} must set '
            '`yum_from_repo_snapshot or build_appliance`'
        )
        assert (layer_opts.yum_from_snapshot is None or
                layer_opts.build_appliance is None), (
            f'`image_layer` {layer_opts.layer_target} must not set '
            '`both yum_from_repo_snapshot and build_appliance`'
        )

        action_to_rpms = {action: set() for action in RpmAction}
        rpm_to_actions = {}
        for item in items:
            assert isinstance(item, RpmActionItem), item
            action_to_rpms[item.action].add(item.name)
            actions = rpm_to_actions.setdefault(item.name, [])
            actions.append((item.action, item.from_target))
            # Raise when a layer has multiple actions for one RPM -- even
            # when all actions are the same.  This can be relaxed if needed.
            if len(actions) != 1:
                raise RuntimeError(
                    f'RPM action conflict for {item.name}: {actions}'
                )

        def builder(subvol: Subvol):
            for action, rpms in action_to_rpms.items():
                if not rpms:
                    continue
                # Future: `yum-from-snapshot` is actually designed to run
                # unprivileged (but we have no nice abstraction for this).
                if layer_opts.build_appliance is None:
                    subvol.run_as_root([
                        # Since `yum-from-snapshot` variants are generally
                        # Python binaries built from this very repo, in
                        # @mode/dev, we would run a symlink-PAR from the
                        # buck-out tree as `root`.  This would leave behind
                        # root-owned `__pycache__` directories, which would
                        # break Buck's fragile cleanup, and cause us to leak old
                        # build artifacts.  This eventually runs the host out of
                        # disk space.  Un-deletable *.pyc files can also
                        # interfere with e.g.  `test-image-layer`, since that
                        # test relies on there being just one `create_ops`
                        # subvolume in `buck-image-out` with the "received UUID"
                        # that was committed to VCS as part of the test
                        # sendstream.
                        'env', 'PYTHONDONTWRITEBYTECODE=1',
                        layer_opts.yum_from_snapshot,
                        *sum((
                            ['--protected-path', d]
                                for d in _protected_path_set(subvol)
                        ), []),
                        '--install-root', subvol.path(), '--',
                        RPM_ACTION_TYPE_TO_YUM_CMD[action],
                        # Sort ensures determinism even if `yum` is
                        # order-dependent
                        '--assumeyes', '--', *sorted(rpms),
                    ])
                else:
                    '''
                    ## Future
                    - implement image feature "manifold_support" with all
                      those bind-mounts below in mounts = [...]
                    - add features = ["manifold_support"] to fb_build_appliance
                    - call nspawn_in_subvol() instead of run_as_root() below
                    '''
                    svol = Subvol(
                        layer_opts.build_appliance,
                        already_exists=True,
                    )
                    mountpoints = mount_item.mountpoints_from_subvol_meta(svol)
                    bind_mount_args = sum((
                        [b'--bind-ro=' + svol.path(mp).replace(b':', b'\\:') +
                         b':' + b'/' + mp.encode()]
                            for mp in mountpoints
                        ), [])
                    protected_path_args = ' '.join(sum((
                        ['--protected-path', d]
                            for d in _protected_path_set(subvol)
                    ), []))
                    # Without this, nspawn would look for the host systemd's
                    # cgroup setup, which breaks us in continuous integration
                    # containers, which may not have a `systemd` in the host
                    # container.
                    subvol.run_as_root([
                        'env', 'UNIFIED_CGROUP_HIERARCHY=yes',
                        'systemd-nspawn',
                        '--quiet',
                        f'--directory={layer_opts.build_appliance}',
                        '--register=no',
                        '--keep-unit',
                        '--ephemeral',
                        b'--bind=' + subvol.path().replace(b':', b'\\:') +
                        b':/mnt',
                        '--bind-ro=/dev/fuse',
                        '--bind-ro=/etc/fbwhoami',
                        '--bind-ro=/etc/smc.tiers',
                        '--bind-ro=/var/facebook/rootcanal',
                        *bind_mount_args,
                        '--capability=CAP_NET_ADMIN',
                        'sh',
                        '-c',
                        (
                            'mkdir -p /mnt/var/cache/yum; '
                            'mount --bind /var/cache/yum /mnt/var/cache/yum; '
                            '/usr/bin/yum-from-fb-snapshot '
                            f'{protected_path_args}'
                            ' --install-root /mnt -- '
                            f'{RPM_ACTION_TYPE_TO_YUM_CMD[action]} '
                            '--assumeyes -- '
                            f'{" ".join(sorted(rpms))}'
                        )
                    ])

        return builder
