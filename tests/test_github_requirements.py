#!/usr/bin/env python

# Copyright 2012-2014 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
import time

from tests.base import ZuulTestCase

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)-32s '
                    '%(levelname)-8s %(message)s')


class TestRequirements(ZuulTestCase):
    """Test pipeline and trigger requirements"""

    def test_pipeline_require_status(self):
        "Test pipeline requirement: status"
        return self._test_require_status('org/project1',
                                         'project1-pipeline')

#    def test_trigger_require_status(self):
#        "Test trigger requirement: status"
#        return self._test_require_status('org/project2',
#                                         'project2-trigger')

    def _test_require_status(self, project, job):
        self.config.set('zuul', 'layout_config',
                        'tests/fixtures/layout-github-requirement-status.yaml')
        self.sched.reconfigure(self.config)
        self.registerJobs()

        A = self.fake_github.openFakePullRequest(project, 'master', 'A')
        # A comment event that we will keep submitting to trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        # No status from zuul so should not be enqueued
        self.assertEqual(len(self.history), 0)

        # An error status should not cause it to be enqueued
        A.setStatus('zuul/check', 'error')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A success status goes in
        A.setStatus('zuul/check', 'success')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, job)

    def test_pipeline_require_reject_status(self):
        "Test negative pipeline requirement: status with state"
        return self._test_require_reject_status('org/project1',
                                                'project1-pipeline')

#    def test_trigger_require_reject_status_state(self):
#        "Test negative trigger requirement: status with state"
#        return self._test_require_reject_status('org/project2',
#                                                'project2-trigger')

    def _test_require_reject_status(self, project, job):
        "Test negative status match"
        # Should only trigger if zuul hasn't set error status
        self.config.set(
            'zuul', 'layout_config',
            'tests/fixtures/layout-github-requirement-reject-username.yaml')
        self.sched.reconfigure(self.config)
        self.registerJobs()

        # add in a change with no comments
        A = self.fake_github.openFakePullRequest(project, 'master', 'A')
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # add in a comment that will trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, job)

        # add in a status from which shouldn't trigger
        A.setStatus('zuul/check', 'error')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)

        # clearing status should queue
        A._clearStatues()
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 2)

        # setting success status should queue
        A.setStatus('zuul/check', 'success')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 3)
