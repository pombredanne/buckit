# Copyright 2016-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree. An additional grant
# of patent rights can be found in the PATENTS file in the same directory.

from __future__ import absolute_import, division, print_function, unicode_literals

import tests.utils
from tests.utils import dedent


class CgoLibraryTest(tests.utils.TestCase):
    @tests.utils.with_project()
    def test_cgo_library_parses(self, root):
        buckfile = "testing/BUCK"
        root.addFile(
            buckfile,
            dedent(
                """
        load("@fbcode_macros//build_defs:cgo_library.bzl", "cgo_library")
        cgo_library(
            name = "foo",
            package_name = "bar",
            deps = [":otherArtifact"],
            exported_deps = [":exportedArtifact"],
            licenses = ["LICENSE"],
            visibility = None,
        )
        """
            ),
        )

        expected = {
            buckfile: dedent(
                r"""
                cgo_library(
                  name = "foo",
                  exported_deps = [
                    "//testing:exportedArtifact",
                  ],
                  licenses = [
                    "LICENSE",
                  ],
                  link_style = "static",
                  package_name = "bar",
                  deps = [
                    "//testing:otherArtifact",
                  ],
                  visibility = [
                    "PUBLIC",
                  ],
                )
                """
            )
        }

        self.validateAudit(expected, root.runAudit([buckfile]))
