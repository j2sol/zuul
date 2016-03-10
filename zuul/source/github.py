# Copyright 2014 Puppet Labs Inc
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

from zuul.model import NullChange, PullRequest, Ref
from zuul.source import BaseSource


class GithubSource(BaseSource):
    name = 'github'
    log = logging.getLogger("zuul.GithubSource")

    def getRefSha(self, project, ref):
        """Return a sha for a given project ref."""
        raise NotImplementedError()

    def waitForRefSha(self, project, ref, old_sha=''):
        """Block until a ref shows up in a given project."""
        raise NotImplementedError()

    def isMerged(self, change, head=None):
        """Determine if change is merged."""
        if not change.number:
            # Not a pull request, considering merged.
            return True
        return change.is_merged

    def canMerge(self, change, allow_needs):
        """Determine if change can merge.

        Github does not have any voting system, all pull requests can merge."""
        return True

    def postConfig(self):
        """Called after configuration has been processed."""
        pass

    def getChange(self, event, project):
        """Get the change representing an event."""
        if event.change_number:
            change = PullRequest(project)
            change.number = event.change_number
            change.refspec = event.refspec
            change.branch = event.branch
            change.url = event.change_url
            change.updated_at = self._ghTimestampToDate(event.updated_at)
            change.patchset = event.patch_number
            change.files = self.getPullFiles(project, change.number)
            change.title = event.title
            change.source_event = event
        elif event.ref:
            change = Ref(project)
            change.ref = event.ref
            change.oldrev = event.oldrev
            change.newrev = event.newrev
            change.url = self.getGitwebUrl(project, sha=event.newrev)
            change.source_event = event
        else:
            change = NullChange(project)
        return change

    def getProjectOpenChanges(self, project):
        """Get the open changes for a project."""
        raise NotImplementedError()

    def updateChange(self, change, history=None):
        """Update information for a change."""
        raise NotImplementedError()

    def getGitUrl(self, project):
        """Get the git url for a project."""
        return self.connection.getGitUrl(project)

    def getGitwebUrl(self, project, sha=None):
        """Get the git-web url for a project."""
        return self.connection.getGitwebUrl(project, sha)

    def getPullFiles(self, project, number):
        """Get filenames of the pull request"""
        owner, project = project.name.split('/')
        return self.connection.getPullFileNames(owner, project, number)

    def _ghTimestampToDate(self, timestamp):
        return time.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')
