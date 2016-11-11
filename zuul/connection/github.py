# Copyright 2015 Hewlett-Packard Development Company, L.P.
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

import collections
import logging
import hmac
import hashlib

import webob
import webob.dec
import voluptuous as v
import github3
from github3.exceptions import MethodNotAllowed

from zuul.connection import BaseConnection
from zuul.exceptions import MergeFailure
from zuul.model import GithubTriggerEvent


class GithubWebhookListener():

    log = logging.getLogger("zuul.GithubWebhookListener")

    def __init__(self, connection):
        self.connection = connection

    def handle_request(self, request):
        if request.method != 'POST':
            self.log.debug("Only POST method is allowed.")
            raise webob.exc.HTTPMethodNotAllowed(
                'Only POST method is allowed.')

        self.log.debug("Github Webhook Received.")

        self._validate_signature(request)

        self.__dispatch_event(request)

    def __dispatch_event(self, request):
        try:
            event = request.headers['X-Github-Event']
            self.log.debug("X-Github-Event: " + event)
        except KeyError:
            self.log.debug("Request headers missing the X-Github-Event.")
            raise webob.exc.HTTPBadRequest('Please specify a X-Github-Event '
                                           'header.')

        try:
            method = getattr(self, '_event_' + event)
        except AttributeError:
            message = "Unhandled X-Github-Event: {0}".format(event)
            self.log.debug(message)
            raise webob.exc.HTTPBadRequest(message)

        try:
            event = method(request)
        except:
            self.log.exception('Exception when handling event:')

        if event:
            self.log.debug('Scheduling github event: {0}'.format(event.type))
            self.connection.sched.addEvent(event)

    def _event_push(self, request):
        body = request.json_body
        base_repo = body.get('repository')

        event = GithubTriggerEvent()
        event.connection_name = self.connection.connection_name
        event.trigger_name = 'github'
        event.project_name = base_repo.get('full_name')

        event.ref = body.get('ref')
        event.oldrev = body.get('before')
        event.newrev = body.get('after')

        ref_parts = event.ref.split('/')  # ie, ['refs', 'heads', 'master']

        if ref_parts[1] == "heads":
            event.type = 'push'
        elif ref_parts[1] == "tags":
            event.type = 'tag'
        else:
            return None

        # necessary for the scheduler to match against particular branches
        event.branch = ref_parts[2]

        return event

    def _event_pull_request(self, request):
        body = request.json_body
        action = body.get('action')
        pr_body = body.get('pull_request')

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(body)

        if action == 'opened':
            event.type = 'pr-open'
        elif action == 'synchronize':
            event.type = 'pr-change'
        elif action == 'closed':
            event.type = 'pr-close'
        elif action == 'reopened':
            event.type = 'pr-reopen'
        elif action == 'labeled':
            event.type = 'pr-label'
            event.label = body['label']['name']
        elif action == 'unlabeled':
            event.type = 'pr-label'
            event.label = '-' + body['label']['name']
        else:
            return None

        return event

    def _event_issue_comment(self, request):
        """Handles pull request comments"""
        body = request.json_body
        action = body.get('action')
        if action != 'created':
            return
        pr_body = self._issue_to_pull_request(body)
        if pr_body is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(body)
        event.comment = body.get('comment').get('body')
        event.type = 'pr-comment'
        return event

    def _issue_to_pull_request(self, body):
        number = body.get('issue').get('number')
        project_name = body.get('repository').get('full_name')
        owner, project = project_name.split('/')
        pr_body = self.connection.getPull(owner, project, number)
        if pr_body is None:
            self.log.debug('Pull request #%s not found in project %s' %
                           (number, project_name))
        return pr_body

    def _validate_signature(self, request):
        secret = self.connection.connection_config.get('webhook_token', None)
        if secret is None:
            return True

        body = request.body
        try:
            request_signature = request.headers['X-Hub-Signature']
        except KeyError:
            raise webob.exc.HTTPUnauthorized(
                'Please specify a X-Hub-Signature header with secret.')

        payload_signature = 'sha1=' + hmac.new(secret,
                                               body,
                                               hashlib.sha1).hexdigest()

        self.log.debug("Payload Signature: {0}".format(str(payload_signature)))
        self.log.debug("Request Signature: {0}".format(str(request_signature)))
        if str(payload_signature) != str(request_signature):
            raise webob.exc.HTTPUnauthorized(
                'Request signature does not match calculated payload '
                'signature. Check that secret is correct.')

        return True

    def _pull_request_to_event(self, pr_body):
        event = GithubTriggerEvent()
        event.connection_name = self.connection.connection_name
        event.trigger_name = 'github'

        base = pr_body.get('base')
        base_repo = base.get('repo')
        head = pr_body.get('head')

        event.project_name = base_repo.get('full_name')
        event.change_number = pr_body.get('number')
        event.change_url = self.connection.getPullUrl(event.project_name,
                                                      event.change_number)
        event.updated_at = pr_body.get('updated_at')
        event.branch = base.get('ref')
        event.refspec = "refs/pull/" + str(pr_body.get('number')) + "/head"
        event.patch_number = head.get('sha')

        event.title = pr_body.get('title')

        return event

    def _get_sender(self, body):
        login = body.get('sender').get('login')
        if login:
            return self.connection.getUser(login)


class GithubUser(collections.Mapping):
    log = logging.getLogger('zuul.GithubUser')

    def __init__(self, github, username):
        self._github = github
        self._username = username
        self._data = None

    def __getitem__(self, key):
        if self._data is None:
            self._data = self._init_data()
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def _init_data(self):
        user = self._github.user(self._username)
        log_rate_limit(self.log, self._github)
        data = {
            'username': user.login,
            'name': user.name,
            'email': user.email
        }
        return data


class GithubConnection(BaseConnection):
    driver_name = 'github'
    log = logging.getLogger("zuul.GithubConnection")
    payload_path = 'payload'
    git_user = 'git'
    git_host = 'github.com'

    def __init__(self, connection_name, connection_config):
        super(GithubConnection, self).__init__(
            connection_name, connection_config)
        self.github = None
        self._change_cache = {}
        self._git_ssh = bool(self.connection_config.get('sshkey', None))

    def onLoad(self):

        webhook_listener = GithubWebhookListener(self)
        self.registerHttpHandler(self.payload_path,
                                 webhook_listener.handle_request)
        self._authenticateGithubAPI()

    def onStop(self):
        self.unregisterHttpHandler(self.payload_path)

    def _authenticateGithubAPI(self):
        token = self.connection_config.get('api_token', None)
        if token is not None:
            self.github = github3.login(token=token)
            self.log.info("Github API Authentication successful.")
        else:
            self.github = None
            self.log.info(
                "No Github credentials found in zuul configuration, cannot "
                "authenticate.")

    def maintainCache(self, relevant):
        for key, change in self._change_cache.items():
            if change not in relevant:
                del self._change_cache[key]

    def getGitUrl(self, project):
        if self._git_ssh:
            url = 'ssh://%s@%s/%s.git' % \
                (self.git_user, self.git_host, project)
        else:
            url = 'https://%s/%s' % (self.git_host, project)
        return url

    def getGitwebUrl(self, project, sha=None):
        url = 'https://%s/%s' % (self.git_host, project)
        if sha is not None:
            url += '/commit/%s' % sha
        return url

    def getPullUrl(self, project, number):
        return '%s/pull/%s' % (self.getGitwebUrl(project), number)

    def getPull(self, owner, project, number):
        pr = self.github.pull_request(owner, project, number).as_dict()
        log_rate_limit(self.log, self.github)
        return pr

    def getPullFileNames(self, owner, project, number):
        filenames = [f.filename for f in
                     self.github.pull_request(owner, project, number).files()]
        log_rate_limit(self.log, self.github)
        return filenames

    def getUser(self, login):
        return GithubUser(self.github, login)

    def getUserUri(self, login):
        return 'https://%s/%s' % (self.git_host, login)

    def commentPull(self, owner, project, pr_number, message):
        pull_request = self.github.issue(owner, project, pr_number)
        pull_request.create_comment(message)
        log_rate_limit(self.log, self.github)

    def mergePull(self, owner, project, pr_number, commit_message='',
                  sha=None):
        pull_request = self.github.pull_request(owner, project, pr_number)
        try:
            result = pull_request.merge(commit_message=commit_message, sha=sha)
        except MethodNotAllowed as e:
            raise MergeFailure('Merge was not successful due to mergeability'
                               ' conflict, original error is %s' % e)
        log_rate_limit(self.log, self.github)
        if not result:
            raise Exception('Pull request was not merged')

    def setCommitStatus(self, owner, project, sha, state,
                        url='', description='', context=''):
        repository = self.github.repository(owner, project)
        repository.create_status(sha, state, url, description, context)
        log_rate_limit(self.log, self.github)

    def labelPull(self, owner, project, pr_number, label):
        pull_request = self.github.issue(owner, project, pr_number)
        pull_request.add_label(label)
        log_rate_limit(self.log, self.github)

    def unlabelPull(self, owner, project, pr_number, label):
        pull_request = self.github.issue(owner, project, pr_number)
        pull_request.remove_label(label)
        log_rate_limit(self.log, self.github)


def log_rate_limit(log, github):
    try:
        rate_limit = github.rate_limit()
        remaining = rate_limit['resources']['core']['remaining']
        reset = rate_limit['resources']['core']['reset']
    except:
        return
    log.debug('GitHub API rate limit remaining: %s reset: %s' %
              (remaining, reset))


def getSchema():
    github_connection = v.Any(str, v.Schema({}, extra=True))
    return github_connection
