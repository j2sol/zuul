# Copyright 2014 Rackspace Australia
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
import testtools
try:
    from unittest import mock
except ImportError:
    import mock

import zuul.reporter


class TestSMTPReporter(testtools.TestCase):
    log = logging.getLogger("zuul.test_reporter")

    def setUp(self):
        super(TestSMTPReporter, self).setUp()

    def test_reporter_abc(self):
        # We only need to instantiate a class for this
        reporter = zuul.reporter.smtp.SMTPReporter({})  # noqa

    def test_reporter_name(self):
        self.assertEqual('smtp', zuul.reporter.smtp.SMTPReporter.name)


class TestGerritReporter(testtools.TestCase):
    log = logging.getLogger("zuul.test_reporter")

    def setUp(self):
        super(TestGerritReporter, self).setUp()

    def test_reporter_abc(self):
        # We only need to instantiate a class for this
        reporter = zuul.reporter.gerrit.GerritReporter(None)  # noqa

    def test_reporter_name(self):
        self.assertEqual('gerrit', zuul.reporter.gerrit.GerritReporter.name)


class TestGithubReporter(testtools.TestCase):
    log = logging.getLogger("zuul.test_reporter")

    def setUp(self):
        super(TestGithubReporter, self).setUp()
        self.connection = mock.MagicMock()
        self.connection.getUserUri = mock.MagicMock(
            return_value='https://github.com/githubuser')
        self.change = mock.MagicMock()
        self.change.title = None
        self.change.source_event.account = {
            'username': 'githubuser',
            'name': '',
            'email': None
        }
        self.reporter = zuul.reporter.github.GithubReporter(
            connection=self.connection)

    def test_reporter_name(self):
        self.assertEqual('github', self.reporter.name)

    def test_format_merge_message_name_email_username(self):
        self.change.source_event.account['name'] = 'Github User'
        self.change.source_event.account['email'] = 'github.user@example.com'

        message = self.reporter._formatMergeMessage(self.change)
        self.assertEqual('\n\n'
                         'Reviewed-by: Github User <github.user@example.com>\n'
                         '             https://github.com/githubuser',
                         message)

    def test_format_merge_message_name_username(self):
        self.change.source_event.account['name'] = 'Github User'

        message = self.reporter._formatMergeMessage(self.change)
        self.assertEqual('\n\n'
                         'Reviewed-by: Github User\n'
                         '             https://github.com/githubuser',
                         message)

    def test_format_merge_message_email_username(self):
        self.change.source_event.account['email'] = 'github.user@example.com'

        message = self.reporter._formatMergeMessage(self.change)
        self.assertEqual('\n\n'
                         'Reviewed-by: <github.user@example.com>\n'
                         '             https://github.com/githubuser',
                         message)

    def test_format_merge_message_username(self):
        message = self.reporter._formatMergeMessage(self.change)
        self.assertEqual('\n\n'
                         'Reviewed-by: https://github.com/githubuser', message)
