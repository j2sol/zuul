#!/usr/bin/env python
# Copyright (c) 2017 IBM Corp.
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

from zuul.driver.github.githubconnection import (
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
)

from tests.base import ZuulTestCase, simple_layout

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(name)-32s '
                    '%(levelname)-8s %(message)s')


class TestGithubRequirements(ZuulTestCase):
    """Test pipeline and trigger requirements"""
    config_file = 'zuul-github-driver.conf'

    @simple_layout('layouts/requirements-github.yaml', driver='github')
    def test_pipeline_require_status(self):
        "Test pipeline requirement: status"
        A = self.fake_github.openFakePullRequest('org/project1', 'master', 'A')
        # A comment event that we will keep submitting to trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        # No status from zuul so should not be enqueued
        self.assertEqual(len(self.history), 0)

        # An error status should not cause it to be enqueued
        A.setStatus(A.head_sha, 'error', 'null', 'null', 'check')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A success status goes in
        A.setStatus(A.head_sha, 'success', 'null', 'null', 'check')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, 'project1-pipeline')

    @simple_layout('layouts/requirements-github.yaml', driver='github')
    def test_trigger_require_status(self):
        "Test trigger requirement: status"
        A = self.fake_github.openFakePullRequest('org/project2', 'master', 'A')

        # An error status should not cause it to be enqueued
        A.setStatus(A.head_sha, 'error', 'null', 'null', 'check')
        self.fake_github.emitEvent(A.getCommitStatusEvent('error'))
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A success status from unknown user should not cause it to be
        # enqueued
        A.setStatus(A.head_sha, 'success', 'null', 'null', 'check', user='foo')
        self.fake_github.emitEvent(A.getCommitStatusEvent('error'))
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A success status from zuul goes in
        A.setStatus(A.head_sha, 'success', 'null', 'null', 'check')
        self.fake_github.emitEvent(A.getCommitStatusEvent('success'))
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, 'project2-trigger')

    @simple_layout('layouts/requirements-github.yaml', driver='github')
    def test_pipeline_require_approval_username(self):
        "Test pipeline requirement: approval username"

        A = self.fake_github.openFakePullRequest('org/project3', 'master', 'A')
        # A comment event that we will keep submitting to trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        # No approval from derp so should not be enqueued
        self.assertEqual(len(self.history), 0)

        # Add an approval (review) from derp
        A.addReview('derp', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, 'project3-reviewusername')

    @simple_layout('layouts/requirements-github.yaml', driver='github')
    def test_pipeline_require_approval_state(self):
        "Test pipeline requirement: approval state"

        A = self.fake_github.openFakePullRequest('org/project4', 'master', 'A')
        # Add derp to writers
        A.writers.append('derp')
        # A comment event that we will keep submitting to trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        # No positive approval from derp so should not be enqueued
        self.assertEqual(len(self.history), 0)

        # A -2 from derp should not cause it to be enqueued
        A.addReview('derp', REVIEW_CHANGES_REQUESTED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A +1 from nobody should not cause it to be enqueued
        A.addReview('nobody', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A +2 from derp should cause it to be enqueued
        A.addReview('derp', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, 'project4-reviewreq')

    @simple_layout('layouts/requirements-github.yaml', driver='github')
    def test_pipeline_require_approval_user_state(self):
        "Test pipeline requirement: approval state from user"

        A = self.fake_github.openFakePullRequest('org/project5', 'master', 'A')
        # Add derp and herp to writers
        A.writers.extend(('derp', 'herp'))
        # A comment event that we will keep submitting to trigger
        comment = A.getCommentAddedEvent('test me')
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        # No positive approval from derp so should not be enqueued
        self.assertEqual(len(self.history), 0)

        # A -2 from derp should not cause it to be enqueued
        A.addReview('derp', REVIEW_CHANGES_REQUESTED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A +1 from nobody should not cause it to be enqueued
        A.addReview('nobody', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A +2 from herp should not cause it to be enqueued
        A.addReview('herp', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 0)

        # A +2 from derp should cause it to be enqueued
        A.addReview('derp', REVIEW_APPROVED)
        self.fake_github.emitEvent(comment)
        self.waitUntilSettled()
        self.assertEqual(len(self.history), 1)
        self.assertEqual(self.history[0].name, 'project5-reviewuserstate')
