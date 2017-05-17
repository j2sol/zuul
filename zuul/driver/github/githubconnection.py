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
import datetime
import logging
import hmac
import hashlib
import time

import cachecontrol
from cachecontrol.cache import DictCache
import iso8601
import jwt
import requests
import webob
import webob.dec
import voluptuous as v
import github3
from github3.exceptions import MethodNotAllowed

from zuul.connection import BaseConnection
from zuul.model import PullRequest, Ref, GithubTriggerEvent
from zuul.exceptions import MergeFailure

ACCESS_TOKEN_URL = 'https://api.github.com/installations/%s/access_tokens'
PREVIEW_JSON_ACCEPT = 'application/vnd.github.machine-man-preview+json'

# The reviews API is a developer preview.  These are the review states
# we currently react to. See: https://developer.github.com/v3/pulls/reviews/
REVIEW_APPROVED = 'APPROVED'
REVIEW_CHANGES_REQUESTED = 'CHANGES_REQUESTED'
REVIEW_COMMENTED = 'COMMENTED'
REVIEW_STATES = [
    REVIEW_APPROVED,
    REVIEW_CHANGES_REQUESTED,
    REVIEW_COMMENTED,
]


class UTC(datetime.tzinfo):
    """UTC"""

    def utcoffset(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return datetime.timedelta(0)


utc = UTC()


class GithubWebhookListener():

    log = logging.getLogger("zuul.GithubWebhookListener")

    def __init__(self, connection):
        self.connection = connection

    def handle_request(self, path, tenant_name, request):
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
            json_body = request.json_body
        except:
            message = 'Exception deserializing JSON body'
            self.log.exception(message)
            raise webob.exc.HTTPBadRequest(message)

        try:
            event = method(json_body)
        except:
            self.log.exception('Exception when handling event:')
            event = None

        if event:
            event.project_hostname = self.connection.canonical_hostname
            self.log.debug('Scheduling github event: {0}'.format(event.type))
            self.connection.sched.addEvent(event)

    def _event_push(self, body):
        base_repo = body.get('repository')

        event = GithubTriggerEvent()
        event.trigger_name = 'github'
        event.project_name = base_repo.get('full_name')
        event.type = 'push'

        event.ref = body.get('ref')
        event.oldrev = body.get('before')
        event.newrev = body.get('after')

        ref_parts = event.ref.split('/')  # ie, ['refs', 'heads', 'master']

        if ref_parts[1] == "heads":
            # necessary for the scheduler to match against particular branches
            event.branch = ref_parts[2]

        return event

    def _event_pull_request(self, body):
        action = body.get('action')
        pr_body = body.get('pull_request')

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(body)

        event.type = 'pull_request'
        if action == 'opened':
            event.action = 'opened'
        elif action == 'synchronize':
            event.action = 'changed'
        elif action == 'closed':
            event.action = 'closed'
        elif action == 'reopened':
            event.action = 'reopened'
        elif action == 'labeled':
            event.action = 'labeled'
            event.label = body['label']['name']
        elif action == 'unlabeled':
            event.action = 'unlabeled'
            event.label = body['label']['name']
        else:
            return None

        return event

    def _event_issue_comment(self, body):
        """Handles pull request comments"""
        action = body.get('action')
        if action != 'created':
            return
        pr_body = self._issue_to_pull_request(body)
        number = body.get('issue').get('number')
        project_name = body.get('repository').get('full_name')
        pr_body = self.connection.getPull(project_name, number)
        if pr_body is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(body)
        event.comment = body.get('comment').get('body')
        event.type = 'pull_request'
        event.action = 'comment'
        return event

    def _event_pull_request_review(self, body):
        """Handles pull request reviews"""
        pr_body = body.get('pull_request')
        if pr_body is None:
            return

        review = body.get('review')
        if review is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.state = review.get('state')
        event.account = self._get_sender(body)
        event.type = 'pull_request_review'
        event.action = body.get('action')
        return event

    def _event_status(self, body):
        action = body.get('action')
        if action == 'pending':
            return
        pr_body = self.connection.getPullBySha(body['sha'])
        if pr_body is None:
            return

        event = self._pull_request_to_event(pr_body)
        event.account = self._get_sender(body)
        event.type = 'pull_request'
        event.action = 'status'
        # Github API is silly. Webhook blob sets author data in
        # 'sender', but API call to get status puts it in 'creator'.
        # Duplicate the data so our code can look in one place
        body['creator'] = body['sender']
        event.event_status = "%s:%s:%s" % self._status_as_tuple(body)
        return event

    def _issue_to_pull_request(self, body):
        number = body.get('issue').get('number')
        project_name = body.get('repository').get('full_name')
        pr_body = self.connection.getPull(project_name, number)
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

        # get the statuses
        event.statuses = self._get_statuses(event.project_name,
                                            event.patch_number)

        return event

    def _get_statuses(self, project, sha):
        # A ref can have more than one status from each context,
        # however the API returns them in order, newest first.
        # So we can keep track of which contexts we've already seen
        # and throw out the rest. Our unique key is based on
        # the user and the context, since context is free form and anybody
        # can put whatever they want there. We want to ensure we track it
        # by user, so that we can require/trigger by user too.
        seen = []
        statuses = []
        for status in self.connection.getCommitStatuses(project, sha):
            stuple = self._status_as_tuple(status)
            if "%s:%s" % (stuple[0], stuple[1]) not in seen:
                statuses.append("%s:%s:%s" % stuple)
                seen.append("%s:%s" % (stuple[0], stuple[1]))

        return statuses

    def _status_as_tuple(self, status):
        """Translate a status into a tuple of user, context, state"""

        creator = status.get('creator')
        if not creator:
            user = "Unknown"
        else:
            user = creator.get('login')
        context = status.get('context')
        state = status.get('state')
        return (user, context, state)

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
    log = logging.getLogger("connection.github")
    payload_path = 'payload'

    def __init__(self, driver, connection_name, connection_config):
        super(GithubConnection, self).__init__(
            driver, connection_name, connection_config)
        self._change_cache = {}
        self.projects = {}
        self.git_ssh_key = self.connection_config.get('sshkey')
        self.git_host = self.connection_config.get('git_host', 'github.com')
        self.canonical_hostname = self.connection_config.get(
            'canonical_hostname', self.git_host)
        self.source = driver.getSource(self)

        self._github = None
        self.integration_id = None
        self.integration_key = None
        self.installation_id = None
        self.installation_token = None
        self.installation_expiry = None

        # NOTE(jamielennox): Better here would be to cache to memcache or file
        # or something external - but zuul already sucks at restarting so in
        # memory probably doesn't make this much worse.
        self.cache_adapter = cachecontrol.CacheControlAdapter(
            DictCache(),
            cache_etags=True)

    def onLoad(self):
        webhook_listener = GithubWebhookListener(self)
        self.registerHttpHandler(self.payload_path,
                                 webhook_listener.handle_request)
        self._authenticateGithubAPI()

    def onStop(self):
        self.unregisterHttpHandler(self.payload_path)

    def _authenticateGithubAPI(self):
        config = self.connection_config

        if self.git_host != 'github.com':
            url = 'https://%s/' % self.git_host
            github = github3.GitHubEnterprise(url)
        else:
            github = github3.GitHub()

        # anything going through requests to http/s goes through cache
        github.session.mount('http://', self.cache_adapter)
        github.session.mount('https://', self.cache_adapter)

        api_token = config.get('api_token')

        if api_token:
            github.login(token=api_token)
        else:
            integration_id = config.get('integration_id')
            installation_id = config.get('installation_id')
            integration_key_file = config.get('integration_key')

            if integration_key_file:
                with open(integration_key_file, 'r') as f:
                    integration_key = f.read()

            if not (integration_id and integration_key and installation_id):
                self.log.warning("You must provide an integration_id, "
                                 "integration_key and installation_id to use "
                                 "installation based authentication")

                return

            self.integration_id = int(integration_id)
            self.installation_id = int(installation_id)
            self.integration_key = integration_key

        self._github = github

    def _get_installation_key(self, user_id=None):
        if not (self.installation_id and self.integration_id):
            return None

        now = datetime.datetime.now(utc)

        if ((not self.installation_expiry) or
                (not self.installation_token) or
                (now < self.installation_expiry)):
            expiry = now + datetime.timedelta(minutes=5)

            data = {'iat': now, 'exp': expiry, 'iss': self.integration_id}
            integration_token = jwt.encode(data,
                                           self.integration_key,
                                           algorithm='RS256')

            url = ACCESS_TOKEN_URL % self.installation_id
            headers = {'Accept': PREVIEW_JSON_ACCEPT,
                       'Authorization': 'Bearer %s' % integration_token}
            json_data = {'user_id': user_id} if user_id else None

            response = requests.post(url, headers=headers, json=json_data)
            response.raise_for_status()

            data = response.json()

            self.installation_expiry = iso8601.parse_date(data['expires_at'])
            self.installation_expiry -= datetime.timedelta(minutes=5)
            self.installation_token = data['token']

        return self.installation_token

    def getGithubClient(self):
        # if we're using api_key authentication then we don't need to fetch
        # new installation tokens so return the existing one.
        installation_key = self._get_installation_key()

        if installation_key:
            self._github.login(token=installation_key)

        return self._github

    def maintainCache(self, relevant):
        for key, change in self._change_cache.items():
            if change not in relevant:
                del self._change_cache[key]

    def getChange(self, event):
        """Get the change representing an event."""

        project = self.source.getProject(event.project_name)
        if event.change_number:
            change = PullRequest(event.project_name)
            change.project = project
            change.number = event.change_number
            change.refspec = event.refspec
            change.branch = event.branch
            change.url = event.change_url
            change.updated_at = self._ghTimestampToDate(event.updated_at)
            change.patchset = event.patch_number
            change.files = self.getPullFileNames(project, change.number)
            change.title = event.title
            change.status = event.statuses
            change.approvals = self.getPullReviews(project, change.number)
            change.source_event = event
        elif event.ref:
            change = Ref(project)
            change.ref = event.ref
            change.oldrev = event.oldrev
            change.newrev = event.newrev
            change.url = self.getGitwebUrl(project, sha=event.newrev)
            change.source_event = event
        else:
            change = Ref(project)
        return change

    def getGitUrl(self, project):
        if self.git_ssh_key:
            return 'ssh://git@%s/%s.git' % (self.git_host, project)

        installation_key = self._get_installation_key()
        if installation_key:
            return 'https://x-access-token:%s@%s/%s' % (installation_key,
                                                        self.git_host,
                                                        project)

        return 'https://%s/%s' % (self.git_host, project)

    def getGitwebUrl(self, project, sha=None):
        url = 'https://%s/%s' % (self.git_host, project)
        if sha is not None:
            url += '/commit/%s' % sha
        return url

    def getProject(self, name):
        return self.projects.get(name)

    def addProject(self, project):
        self.projects[project.name] = project

    def getProjectBranches(self, project):
        github = self.getGithubClient()
        owner, proj = project.name.split('/')
        repository = github.repository(owner, proj)
        branches = [branch.name for branch in repository.branches()]
        log_rate_limit(self.log, github)
        return branches

    def getPullUrl(self, project, number):
        return '%s/pull/%s' % (self.getGitwebUrl(project), number)

    def getPull(self, project_name, number):
        github = self.getGithubClient()
        owner, proj = project_name.split('/')
        pr = github.pull_request(owner, proj, number).as_dict()
        log_rate_limit(self.log, github)
        return pr

    def canMerge(self, change, allow_needs):
        # This API call may get a false (null) while GitHub is calculating
        # if it can merge.  The github3.py library will just return that as
        # false. This could lead to false negatives.
        # Additionally, this only checks if the PR code could merge
        # cleanly to the target branch. It does not evaluate any branch
        # protection merge requirements (such as reviews and status states)
        # At some point in the future this may be available through the API
        # or we can fetch the branch protection settings and evaluate within
        # Zuul whether or not those protections have been met
        # For now, just send back a True value.
        return True

    def getPullBySha(self, sha):
        query = '%s type:pr is:open' % sha
        pulls = []
        github = self.getGithubClient()
        for issue in github.search_issues(query=query):
            pr_url = issue.pull_request.get('url')
            if not pr_url:
                continue
            # the issue provides no good description of the project :\
            owner, project, _, number = pr_url.split('/')[4:]
            github = self.getGithubClient("%s/%s" % (owner, project))
            pr = github.pull_request(owner, project, number)
            if pr.head.sha != sha:
                continue
            if pr.as_dict() in pulls:
                continue
            pulls.append(pr.as_dict())

        log_rate_limit(self.log, github)
        if len(pulls) > 1:
            raise Exception('Multiple pulls found with head sha %s' % sha)

        if len(pulls) == 0:
            return None
        return pulls.pop()

    def getPullFileNames(self, project, number):
        github = self.getGithubClient()
        owner, proj = project.name.split('/')
        filenames = [f.filename for f in
                     github.pull_request(owner, proj, number).files()]
        log_rate_limit(self.log, github)
        return filenames

    def getPullReviews(self, project, number):
        owner, proj = project.name.split('/')

        reviews = self._getPullReviews(owner, proj, number)
        # We are mapping reviews to something that looks gerrit approvals
        # 'APPROVE' and 'REQUEST_CHANGES' are a review type of
        # 'review', where as `COMMENT` is a type of 'comment'.
        # Users with write access get a value of 2/-2 whereas users without
        # write access get a value of 1/-1.

        approvals = {}
        for review in reviews:
            if review.get('state') not in REVIEW_STATES:
                continue

            user = review.get('user').get('login')
            approval = {
                'by': {
                    'username': user,
                    'email': review.get('user').get('email'),
                },
                'grantedOn': int(time.mktime(self._ghTimestampToDate(
                                             review.get('submitted_at')))),
            }

            approval['submitted'] = review.get('submitted_at')
            # Determine type
            if review.get('state') == REVIEW_COMMENTED:
                approval['type'] = 'comment'
                approval['description'] = 'comment'
                approval['value'] = '0'
            else:
                approval['type'] = 'review'
                approval['description'] = 'review'

            # Get user's rights
            user_can_write = False
            permission = self.getRepoPermission(
                project.name, user)
            if permission in ['admin', 'write']:
                user_can_write = True

            # Determine value
            if review.get('state') == REVIEW_APPROVED:
                if user_can_write:
                    approval['value'] = '2'
                else:
                    approval['value'] = '1'
            elif review.get('state') == REVIEW_CHANGES_REQUESTED:
                if user_can_write:
                    approval['value'] = '-2'
                else:
                    approval['value'] = '-1'

            if user not in approvals:
                approvals[user] = approval
            else:
                # if there are multiple reviews per user, keep the newest
                # note that this breaks the ability to set the 'older-than'
                # option on a review requirement.
                if approval['grantedOn'] > approvals[user]['grantedOn']:
                    approvals[user] = approval

        return approvals.values()

    def _getPullReviews(self, owner, project, number):
        # make a list out of the reviews so that we complete our
        # API transaction
        github = self.getGithubClient()
        reviews = [review.as_dict() for review in
                   github.pull_request(owner, project, number).reviews()]

        log_rate_limit(self.log, github)
        return reviews

    def getUser(self, login):
        return GithubUser(self.getGithubClient(), login)

    def getUserUri(self, login):
        return 'https://%s/%s' % (self.git_host, login)

    def getRepoPermission(self, project, login):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        # This gets around a missing API call
        # need preview header
        headers = {'Accept': 'application/vnd.github.korra-preview'}

        # Create a repo object
        repository = github.repository(owner, project)
        # Build up a URL
        url = repository._build_url('collaborators', login, 'permission',
                                    base_url=repository._api)
        # Get the data
        perms = repository._get(url, headers=headers)

        log_rate_limit(self.log, github)

        # no known user, maybe deleted since review?
        if perms.status_code == 404:
            return 'none'

        # get permissions from the data
        return perms.json()['permission']

    def commentPull(self, project, pr_number, message):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        repository = github.repository(owner, proj)
        pull_request = repository.issue(pr_number)
        pull_request.create_comment(message)
        log_rate_limit(self.log, github)

    def mergePull(self, project, pr_number, commit_message='', sha=None):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        pull_request = github.pull_request(owner, proj, pr_number)
        try:
            result = pull_request.merge(commit_message=commit_message, sha=sha)
        except MethodNotAllowed as e:
            raise MergeFailure('Merge was not successful due to mergeability'
                               ' conflict, original error is %s' % e)
        log_rate_limit(self.log, github)
        if not result:
            raise Exception('Pull request was not merged')

    def getCommitStatuses(self, project, sha):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        repository = github.repository(owner, proj)
        commit = repository.commit(sha)
        # make a list out of the statuses so that we complete our
        # API transaction
        statuses = [status.as_dict() for status in commit.statuses()]

        log_rate_limit(self.log, github)
        return statuses

    def setCommitStatus(self, project, sha, state, url='', description='',
                        context=''):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        repository = github.repository(owner, proj)
        repository.create_status(sha, state, url, description, context)
        log_rate_limit(self.log, github)

    def labelPull(self, project, pr_number, label):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        pull_request = github.issue(owner, proj, pr_number)
        pull_request.add_labels(label)
        log_rate_limit(self.log, github)

    def unlabelPull(self, project, pr_number, label):
        github = self.getGithubClient()
        owner, proj = project.split('/')
        pull_request = github.issue(owner, proj, pr_number)
        pull_request.remove_label(label)
        log_rate_limit(self.log, github)

    def _ghTimestampToDate(self, timestamp):
        return time.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ')


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
