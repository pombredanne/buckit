#!/usr/bin/env python3
'''
This test only validates the binary send-stream parser. We rely on the fact
that `test_parse_dump.py` already sanity-checks the gold data.
'''
import io
import struct
import unittest

from .demo_sendstreams import gold_demo_sendstreams
from .demo_sendstreams_expected import get_filtered_and_expected_items

from ..parse_send_stream import (
    AttributeKind, check_magic, check_version, CommandKind, file_unpack,
    parse_send_stream, read_attribute, read_command,
)

# `unittest`'s output shortening makes tests much harder to debug.
unittest.util._MAX_LENGTH = 12345


def _parse_stream_bytes(s: bytes) -> io.BytesIO:
    return parse_send_stream(io.BytesIO(s))


class ParseSendStreamTestCase(unittest.TestCase):

    def setUp(self):
        self.maxDiff = 12345

    def test_verify_gold_parse(self):
        stream_dict = gold_demo_sendstreams()
        filtered_items, expected_items = get_filtered_and_expected_items(
            items=[
                *_parse_stream_bytes(stream_dict['create_ops']['sendstream']),
                *_parse_stream_bytes(stream_dict['mutate_ops']['sendstream']),
            ],
            build_start_time=stream_dict['create_ops']['build_start_time'],
            build_end_time=stream_dict['mutate_ops']['build_end_time'],
            dump_mode=False,
        )
        self.assertEqual(filtered_items, expected_items)

    def test_errors(self):
        with self.assertRaisesRegex(RuntimeError, "Magic b'xxx', not "):
            check_magic(io.BytesIO(b'xxx'))
        with self.assertRaisesRegex(RuntimeError, 'we require version 1'):
            check_version(io.BytesIO(b'abcd'))
        with self.assertRaisesRegex(RuntimeError, 'Not enough bytes'):
            file_unpack('<Q', io.BytesIO(b''))

        cmd_header_2_attrs = struct.pack(
            '<IHI',
            2 * (2 + 2 + 3),  # length excluding this header
            CommandKind.MKFILE.value,
            0,  # crc32c
        )

        with self.assertRaisesRegex(RuntimeError, 'CommandHead.* got 0 bytes'):
            read_command(io.BytesIO(cmd_header_2_attrs))

        with self.assertRaisesRegex(RuntimeError, 'AttributeH.* got 0 bytes'):
            read_attribute(io.BytesIO(struct.pack(
                '<HH',
                AttributeKind.PATH.value,
                3,  # length excluding this header -- error: we write no data!
            )))

        with self.assertRaisesRegex(RuntimeError, '\\.PATH occurred twice'):
            read_command(io.BytesIO(cmd_header_2_attrs + struct.pack(
                '<' + 'HH3s' * 2,  # 2 attributes

                AttributeKind.PATH.value,
                3,  # length excluding this header
                b'cat',

                AttributeKind.PATH.value,
                3,  # length excluding this header
                b'dog',
            )))


if __name__ == '__main__':
    unittest.main()
