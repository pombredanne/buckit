#!/usr/bin/env python3
'Utilities to make Python systems programming more palatable.'
import hashlib
import os
import subprocess
import stat

from typing import AnyStr, NamedTuple


# Bite me, Python3.
def byteme(s: AnyStr) -> bytes:
    'Byte literals are tiring, just promote strings as needed.'
    return s.encode() if isinstance(s, str) else s


# `pathlib` refuses to operate on `bytes`, which is the only sane way on Linux.
class Path(bytes):
    'A byte path that supports joining via the / operator.'

    def __new__(cls, arg, *args, **kwargs):
        return super().__new__(cls, byteme(arg), *args, **kwargs)

    def __truediv__(self, right: AnyStr) -> bytes:
        return Path(os.path.join(self, byteme(right)))

    def __rtruediv__(self, left: AnyStr) -> bytes:
        return Path(os.path.join(byteme(left), self))


def open_ro(path, mode):
    '`open` that creates (and never overwrites) a file with mode `a+r`.'
    def ro_opener(path, flags):
        return os.open(
            path,
            (flags & ~os.O_TRUNC) | os.O_CREAT | os.O_CLOEXEC,
            mode=stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH,
        )
    return open(path, mode, opener=ro_opener)


def check_popen_returncode(proc: subprocess.Popen):
    if proc.returncode != 0:  # pragma: no cover
        # Providing a meaningful coverage test for this is annoying, so I just
        # tested manually:
        #   >>> import subprocess
        #   >>> raise subprocess.CalledProcessError(returncode=5, cmd=['a'])
        #   Traceback (most recent call last):
        #     File "<stdin>", line 1, in <module>
        #   subprocess.CalledProcessError: Command '['a']' returned non-zero
        #   exit status 5.
        raise subprocess.CalledProcessError(
            returncode=proc.returncode, cmd=proc.args,
        )


class Checksum(NamedTuple):
    algorithm: str
    hexdigest: str

    @classmethod
    def from_string(cls, s: str) -> 'Checksum':
        algorithm, hexdigest = s.split(':')
        return cls(algorithm=algorithm, hexdigest=hexdigest)

    def __str__(self):
        return f'{self.algorithm}:{self.hexdigest}'

    def hasher(self):
        # Certain repos use "sha" to refer to "SHA-1", whereas in `hashlib`,
        # "sha" goes through OpenSSL and refers to a different digest.
        if self.algorithm == 'sha':
            return hashlib.sha1()
        return hashlib.new(self.algorithm)
