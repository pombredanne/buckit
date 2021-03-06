#!/usr/bin/env python3
import os
import platform
import subprocess
import time

from contextlib import contextmanager
from typing import AnyStr, BinaryIO, Iterator

from btrfs_loopback import LoopbackVolume, run_stdout_to_err
from common import byteme, check_popen_returncode, get_file_logger, pipe
from unshare import Namespace, nsenter_as_root, nsenter_as_user, Unshare

from compiler.subvolume_on_disk import SubvolumeOnDisk

log = get_file_logger(__file__)
MiB = 2 ** 20


# A simple helper returning path to subvolume referred by
# "subvolume_rel_path" key in layer_json json file
def get_subvolume_path(layer_json, subvolumes_dir):
    with open(layer_json) as infile:
        return SubvolumeOnDisk.from_json_file(
            infile, subvolumes_dir).subvolume_path()


# Exposed as a helper so that test_compiler.py can mock it.
def _path_is_btrfs_subvol(path):
    'Ensure that there is a btrfs subvolume at this path. As per @kdave at '
    'https://stackoverflow.com/a/32865333'
    # You'd think I could just `os.statvfs`, but no, not until Py3.7
    # https://bugs.python.org/issue32143
    fs_type = subprocess.run(
        ['stat', '-f', '--format=%T', path],
        stdout=subprocess.PIPE,
    ).stdout.decode().strip()
    ino = os.stat(path).st_ino
    return fs_type == 'btrfs' and ino == 256


class Subvol:
    '''
    ## What is this for?

    This class is to be a privilege / abstraction boundary that allows
    regular, unprivileged Python code to construct images.  Many btrfs
    ioctls require CAP_SYS_ADMIN (or some kind of analog -- e.g. a
    `libguestfs` VM or a privileged server for performing image operations).
    Furthermore, writes to the image-under-construction may require similar
    sorts of privileges to manipulate the image-under-construction as uid 0.

    One approach would be to eschew privilege boundaries, and to run the
    entire build process as `root`.  However, that would forever confine our
    build tool to be run in VMs and other tightly isolated contexts.  Since
    unprivileged image construction is technically possible, we will instead
    take the approach that -- as much as possible -- the build code runs
    unprivileged, as the repo-owning user, and only manipulates the
    filesystem-under-construction via this one class.

    For now, this means shelling out via `sudo`, but in the future,
    `libguestfs` or a privileged filesystem construction proxy could be
    swapped in with minimal changes to the overall structure.

    ## Usage

    - Think of `Subvol` as a ticket to operate on a btrfs subvolume that
      exists, or is about to be created, at a known path on disk. This
      convention lets us cleanly describe paths on a subvolume that does not
      yet physically exist.

    - Call the functions from the btrfs section to manage the subvolumes.

    - Call `subvol.run_as_root()` to use shell commands to manipulate the
      image under construction.

    - Call `subvol.path('image/relative/path')` to refer to paths inside the
      subvolume e.g. in arguments to the `subvol.run_*` functions.
    '''

    def __init__(self, path: AnyStr, already_exists=False):
        '''
        `Subvol` can represent not-yet-created subvolumes.  Unless
        already_exists=True, you must call create() or snapshot() to
        actually make the subvolume.
        '''
        self._path = os.path.abspath(byteme(path))
        self._exists = already_exists
        if self._exists and not _path_is_btrfs_subvol(self._path):
            raise AssertionError(f'No btrfs subvol at {self._path}')

    def path(
        self, path_in_subvol: AnyStr=b'.', *, no_dereference_leaf=False,
    ) -> bytes:
        '''
        The only safe way to access paths inside the subvolume.  Do NOT
        `os.path.join(subvol.path('a/path'), 'more/path')`, since that skips
        crucial safety checks.  Instead: `subvol.path(os.path.join(...))`.

        This code has checks to mitigate two risks:
          - `path_in_subvol` is relative, and exits the subvolume via '..'
          - Some component of the path is a symlink, and this symlink, when
            interpreted by a non-chrooted tool, will attempt to access
            something outside of the subvolume.

        At present, the above check fail on attempting to traverse an
        in-subvolume symlink that is an absolute path to another directory
        within the subvolume, but support could easily be added.  It is not
        supported now because at present, I believe that the right idiom is
        to encourage image authors to manipulate the "real" locations of
        files, and not to manipulate paths through symlinks.

        In the rare case that you need to manipulate a symlink itself (e.g.
        remove or rename), you will want to pass `no_dereference_leaf`.

        Future: consider using a file descriptor to refer to the subvolume
        root directory to better mitigate races due to renames in its path.
        '''
        # The `btrfs` CLI is not very flexible, so it will try to name a
        # subvol '.' if we do not normalize `/subvol/.`.
        result_path = os.path.normpath(os.path.join(
            self._path,
            # Without the lstrip, we would lose the subvolume prefix if the
            # supplied path is absolute.
            byteme(path_in_subvol).lstrip(b'/'),
        ))
        # Paranoia: Make sure that, despite any symlinks in the path, the
        # resulting path is not outside of the subvolume root.
        #
        # NB: This will prevent us from even accessing symlinks created
        # inside the subvolume.  To fix this, we should add an OPTION not to
        # follow the LAST component of the path.
        root_relative = os.path.relpath((
            os.path.join(
                os.path.realpath(os.path.dirname(result_path)),
                os.path.basename(result_path),
            ) if no_dereference_leaf else os.path.realpath(result_path)
        ), os.path.realpath(self._path))
        if root_relative.startswith(b'../') or root_relative == b'..':
            raise AssertionError(f'{path_in_subvol} is outside the subvol')
        return result_path

    # This differs from the regular `subprocess.Popen` interface in these ways:
    #   - stdout maps to stderr by default (to protect the caller's stdout),
    #   - `check` is supported, and default to `True`,
    #   - `cwd` is prohibited.
    #
    # `_subvol_exists` is a private kwarg letting us `run_as_root` to create
    # new subvolumes, and not just touch existing ones.
    @contextmanager
    def popen_as_root(
        self, args, *, _subvol_exists=True, stdout=None, check=True, **kwargs,
    ):
        if 'cwd' in kwargs:
            raise AssertionError(
                'cwd= is not permitted as an argument to run_as_root, '
                'because that makes it too easy to accidentally traverse '
                'a symlink from inside the container and touch the host '
                'filesystem. Best practice: wrap your path with '
                'Subvol.path() as close as possible to its site of use.'
            )
        if _subvol_exists != self._exists:
            raise AssertionError(
                f'{self.path()} exists is {self._exists}, not {_subvol_exists}'
            )
        # Ban our subcommands from writing to stdout, since many of our
        # tools (e.g. make-demo-sendstream, compiler) write structured
        # data to stdout to be usable in pipelines.
        if stdout is None:
            stdout = 2
        # The '--' is to avoid `args` from accidentally being parsed as
        # environment variables or `sudo` options.
        with subprocess.Popen(
            ['sudo', '--', *args], stdout=stdout, **kwargs,
        ) as pr:
            yield pr
        if check:
            check_popen_returncode(pr)

    def run_as_root(
        self, args, timeout=None, input=None, _subvol_exists=True,
        check=True, **kwargs,
    ):
        '''
        Run a command against an image.  IMPORTANT: You MUST wrap all image
        paths with `Subvol.path`, see that function's docblock.

        Mostly API-compatible with subprocess.run, except that:
            - `check` defaults to True instead of False,
            - `stdout` is redirected to stderr by default,
            - `cwd` is prohibited.
        '''
        # IMPORTANT: Any logic that CAN go in popen_as_root, MUST go there.
        if input:
            assert 'stdin' not in kwargs
            kwargs['stdin'] = subprocess.PIPE
        with self.popen_as_root(
            args, _subvol_exists=_subvol_exists, check=check, **kwargs,
        ) as proc:
            stdout, stderr = proc.communicate(timeout=timeout, input=input)
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    # Future: run_in_image()

    # From here on out, every public method directly maps to the btrfs API.
    # For now, we shell out, but in the future, we may talk to a privileged
    # `btrfsutil` helper, or use `guestfs`.

    def create(self):
        self.run_as_root([
            'btrfs', 'subvolume', 'create', self.path(),
        ], _subvol_exists=False)
        self._exists = True

    def snapshot(self, source: 'Subvol'):
        # Since `snapshot` has awkward semantics around the `dest`,
        # `_subvol_exists` won't be enough and we ought to ensure that the
        # path physically does not exist.  This needs to run as root, since
        # `os.path.exists` may not have the right permissions.
        self.run_as_root(
            ['test', '!', '-e', self.path()], _subvol_exists=False
        )
        self.run_as_root([
            'btrfs', 'subvolume', 'snapshot', source.path(), self.path()
        ], _subvol_exists=False)
        self._exists = True

    def delete(self):
        self.run_as_root(['btrfs', 'subvolume', 'delete', self.path()])
        self._exists = False

    def set_readonly(self, readonly: bool):
        self.run_as_root([
            'btrfs', 'property', 'set', '-ts', self.path(), 'ro',
            'true' if readonly else 'false',
        ])

    def sync(self):
        self.run_as_root(['btrfs', 'filesystem', 'sync', self.path()])

    @contextmanager
    def _mark_readonly_and_send(
        self, *, stdout, no_data: bool=False, parent: 'Subvol'=None,
    ) -> Iterator[subprocess.Popen]:
        self.set_readonly(True)

        # Btrfs bug #25329702: in some cases, a `send` without a sync will
        # violate read-after-write consistency and send a "past" view of the
        # filesystem.  Do this on the read-only filesystem to improve
        # consistency.
        self.sync()

        # Btrfs bug #25379871: our 4.6 kernels have an experimental xattr
        # caching patch, which is broken, and results in xattrs not showing
        # up in the `send` stream unless that metadata is `fsync`ed.  For
        # some dogscience reason, `getfattr` on a file actually triggers
        # such an `fsync`.  We do this on a read-only filesystem to improve
        # consistency. Coverage: manually tested this on a 4.11 machine:
        #   platform.uname().release.startswith('4.11.')
        if platform.uname().release.startswith('4.6.'):  # pragma: no cover
            self.run_as_root([
                # Symlinks can point outside of the subvol, don't follow them
                'getfattr', '--no-dereference', '--recursive', self.path()
            ])

        with self.popen_as_root([
            'btrfs', 'send',
            *(['--no-data'] if no_data else []),
            *(['-p', parent.path()] if parent else []),
            self.path(),
        ], stdout=stdout) as proc:
            yield proc

    def mark_readonly_and_get_sendstream(self, **kwargs) -> bytes:
        with self._mark_readonly_and_send(
            stdout=subprocess.PIPE, **kwargs,
        ) as proc:
            return proc.stdout.read()

    @contextmanager
    def mark_readonly_and_write_sendstream_to_file(
        self, outfile: BinaryIO, **kwargs,
    ) -> Iterator[None]:
        with self._mark_readonly_and_send(stdout=outfile, **kwargs):
            yield

    def mark_readonly_and_send_to_new_loopback(
        self, output_path, waste_factor=1.15,
    ) -> int:
        '''
        Overwrites `ouput_path` with a new btrfs image, and send this
        subvolume to this new volume.  The image is populated as a loopback
        mount, which will be unmounted before this function returns.

        Since btrfs sizing facilities are unreliable, we size the new
        filesystem by guesstimating the content size of the filesystem, and
        multiplying it by `waste_factor` to ensure that `receive` does not
        run out of space.  If out-of-space does occur, this function repeats
        multiply-send-receive until we succeed, so a low `waste_factor` can
        make image builds much slower.

        ## Notes on setting `waste_factor`

          - This is exposed for unit tests, you should probably not surface
            it to users.  We should improve the auto-sizing instead.

          - Even though sparse files make it fairly cheap to allocate a
            much larger loopback than what is required to contain the
            subvolume, we want to try to keep the loopback filesystem as
            full as possible. The primary rationale is that parts of
            our image distribution filesystem do not support sparse files
            (to be fixed). Secondarily, btrfs seems to increase the
            amount of overhead it permits itself as the base filesystem
            becomes larger. I have not carefully measured the loopback
            size after accounting for sparseness, but this needs to
            be tested before considering much larger waste factors.

          - While we resize down to `min-dev-size` after populating the
            volume, setting a higher waste factor is still not free.  The
            reason is that btrfs auto-allocates more metadata blocks for
            larger filesystems, but `resize` does not release them.  So if
            you start with a larger waste factor, your post-shrink
            filesystem will be larger, too.  This is one of the reasons why
            we should just `findmnt -o SIZE` to determine a safe size of the
            loopback (the other reason is below).

          - The default of 15% is very conservative, with the goal of
            never triggering an expensive send+receive combo. This seeks to
            optimize developer happiness.  In my tests, I have not seen a
            filesystem that needed more than 5%.  Later, we can add
            monitoring and gradually dial this down.

          - If your subvolume's `_estimate_content_bytes` is X, and it
            fits in a loopback of size Y, it is not guaranteed that you
            could have used `waste_factor = Y / X`, because lazy writes make
            it possible to resize a populated filesystem to have a size
            **below** what you would have needed to populate its content.

          - There is an alternative strategy to "multiply by waste_factor &
            re-send", which is to implement a `pv`-style function that
            sits on a pipe between `send` and `receive`, and does the
            following to make sure `receive` never runs out of space:
              - `btrfs filesystem sync`, `usage`, and if "min" free space
                drops too low, `resize`
              - `splice` (via `ctypes`, or write this interposition program
                in C) a chunk from `send` to `receive`. Using `splice`
                instead of copying through userspace is not **necessarily**
                essential, but in order to minimize latency, it's important
                that we starve the `receive` end as rarely as possible,
                which may require some degree of concurrency between reading
                from `send` and writing to `receive`.  To clarify: a naive
                Python prototype that read & wrote 2MB at a time -- a buffer
                that's large enough that we'd frequently starve `receive` or
                stall `send` -- experienced a 30% increase in wall time
                compared to `send | receive`.
              - Monitor usage much more frequently than the free space to
                chunk size ratio would indicate, since something may buffer.
                Don't use a growth increment that is TOO small.
              - Since there are no absolute guarantees that btrfs won't
                run out of space on `receive`, there should still be an
                outer retry layer, but it ought to never fire.
              - Be aware that the minimum `mkfs.brfs` size is 108MiB, the
                minimum size to which we can grow via `resize` is 175MiB,
                while the minimum size to which we can shrink via `resize`
                is 256MiB, so the early growth tactics should reflect this.

            The advantage of this strategy of interposing on a pipe, if
            implemented well, is that we should be able to achieve a smaller
            waste factor without paying occasionally doubling our wall clock
            and IOP costs due to retries.  The disadvantage is that if we do
            a lot of grow cycles prior to our shrink, the resulting
            filesystem may end up being more out-of-tune than if we had
            started with a large enough size from the beginning.
        '''
        # In my experiments, btrfs needs at least 81 MB of overhead in all
        # circumstances, and this initial overhead is not multiplicative.
        # To be specific, I tried single-file subvolumes with files of size
        # 27, 69, 94, 129, 175, 220MiB.
        fs_bytes = self._estimate_content_bytes() + 81 * MiB
        attempts = 0
        while True:
            attempts += 1
            fs_bytes *= waste_factor
            if self._send_to_loopback_if_fits(output_path, int(fs_bytes)):
                break
            log.warning(f'{self._path} did not fit in {fs_bytes} bytes')
        # Future: It would not be unreasonable to run some sanity checks on
        # the resulting filesystem here. Ideas:
        #  - See if we have an unexpectedly large amount of unused metadata
        #    space, or other "waste" via `btrfs filesystem usage -b` --
        #    could ask @clm if this is a frequent issue.
        #  - Can we check for fragmentation / balance issues?
        #  - We could (very occasionally & maybe in the background, since
        #    this is expensive) check that the received subvolume is
        #    identical to the source subvolume.
        return attempts

    def _estimate_content_bytes(self):
        '''
        Returns a (usually) tight lower-bound guess of the filesystem size
        necessary to contain this subvolume.  The caller is responsible for
        appropriately padding this size when creating the destination FS.

        ## Future: Query the subvolume qgroup to estimate its size

          - If quotas are enabled, this should be an `O(1)` operation
            instead of the more costly filesystem tree traversal.  NB:
            qgroup size estimates tend to run a bit (~1%) lower than `du`,
            so growth factors may need a tweak.  `_estimate_content_bytes()`
            should `log.warning` and fall back to `du` if quotas are
            disabled in an older `buck-image-out`.  It's also an option to
            enable quotas and to trigger a `rescan -w`, but requires more
            code/testing.

          - Using qgroups for builds is a good stress test of the qgroup
            subsystem. It would help us gain confidence in that (a) they
            don't trigger overt issues (filesystem corruption, dramatic perf
            degradation, or crashes), and that (b) they report reasonably
            accurate numbers on I/O-stressed build hosts.

          - Should run an A/B test to see if the build perf wins of querying
            qgroups exceed the perf hit of having quotas enabled.

          - Eventually, we'd enable quotas by default for `buck-image-out`
            volumes.

          - Need to delete the qgroup whenever we delete a subvolume.  Two
            main cases: `Subvol.delete` and `subvolume_garbage_collector.py`.
            Can check if we are leaking cgroups by building & running &
            image tests, and looking to see if that leaves behind 0-sized
            cgroups unaffiliated with subvolumes.

          - The basic logic for qgroups looks like this:

            $ sudo btrfs subvol show buck-image-out/volume/example |
                grep 'Subvolume ID'
                    Subvolume ID:           1432

            $ sudo btrfs qgroup show --raw --sync buck-image-out/volume/ |
                grep ^0/1432
            0/1432     1381523456        16384
            # We want the second column, bytes in referenced extents.

            # For the `qgroup show` operation, check for **at least** these
            # error signals on stderr -- with exit code 1:
            ERROR: can't list qgroups: quotas not enabled
            # ... and with exit code 0:
            WARNING: qgroup data inconsistent, rescan recommended
            WARNING: rescan is running, qgroup data may be incorrect
            # Moreover, I would have the build fail on any unknown output.
        '''
        # Not adding `-x` since buck-built subvolumes should not have other
        # filesystems mounted inside them.
        start_time = time.time()
        du_out = subprocess.check_output([
            'sudo', 'du', '--block-size=1', '--summarize', self._path,
        ]).split(b'\t', 1)
        assert du_out[1] == self._path + b'\n'
        size = int(du_out[0])
        log.info(
            f'`du` estimated size of {self._path} as {size} in '
            f'{time.time() - start_time} seconds'
        )
        return size

    # Mocking this allows tests to exercise the fallback "out of space" path.
    _OUT_OF_SPACE_SUFFIX = b': No space left on device\n'

    def _send_to_loopback_if_fits(self, output_path, fs_size_bytes) -> bool:
        '''
        Creates a loopback of the specified size, and sends the current
        subvolume to it.  Returns True if the subvolume fits in that space.
        '''
        open(output_path, 'wb').close()
        with pipe() as (r_send, w_send), \
                Unshare([Namespace.MOUNT, Namespace.PID]) as ns, \
                LoopbackVolume(ns, output_path, fs_size_bytes) as loop_vol, \
                self.mark_readonly_and_write_sendstream_to_file(w_send):
            w_send.close()  # This end is now fully owned by `btrfs send`.
            with r_send:
                recv_ret = run_stdout_to_err(nsenter_as_root(
                    ns, 'btrfs', 'receive', loop_vol.dir(),
                ), stdin=r_send, stderr=subprocess.PIPE)
                if recv_ret.returncode != 0:
                    if recv_ret.stderr.endswith(self._OUT_OF_SPACE_SUFFIX):
                        return False
                    # It's pretty lame to rely on `btrfs receive` continuing
                    # to be unlocalized, and emitting that particular error
                    # message, so we fall back to checking available bytes.
                    size_ret = subprocess.run(nsenter_as_user(
                        ns, 'findmnt', '--noheadings', '--bytes',
                        '--output', 'AVAIL', loop_vol.dir(),
                    ), stdout=subprocess.PIPE)
                    # If the `findmnt` fails, don't mask the original error.
                    if size_ret.returncode == 0 and int(size_ret.stdout) == 0:
                        return False
                    # Covering this is hard, so the test plan is "inspection".
                    log.error(  # pragma: no cover
                        'Unhandled receive stderr:\n\n' +
                        recv_ret.stderr.decode(errors='surrogateescape'),
                    )
                recv_ret.check_returncode()
            loop_vol.minimize_size()
            return True
