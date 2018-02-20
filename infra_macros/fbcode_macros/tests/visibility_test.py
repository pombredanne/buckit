# Copyright 2016-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import tests.utils


class VisibilityTest(tests.utils.TestCase):
    maxDiff = None
    includes = [
        (
            "@fbcode_macros//build_defs:visibility.bzl",
            "get_visibility"
        )
    ]

    @tests.utils.with_project()
    def test_returns_default_visibility_or_original_visibility(self, root):
        statements = [
            'get_visibility(None)',
            'get_visibility(["//..."])',
        ]
        result = root.run_unittests(self.includes, statements)
        self.assertSuccess(result, ["PUBLIC"], ["//..."])
