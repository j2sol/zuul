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

import logging
import voluptuous as v
from zuul.model import EventFilter
from zuul.trigger import BaseTrigger


class GithubTrigger(BaseTrigger):
    name = 'github'
    log = logging.getLogger("zuul.trigger.GithubTrigger")

    def _toList(self, item):
        if not item:
            return []
        if isinstance(item, list):
            return item
        return [item]

    def getEventFilters(self, trigger_config):
        efilters = []
        for trigger in self._toList(trigger_config):
            types = trigger.get('event', None)
            actions = trigger.get('actions')
            f = EventFilter(trigger=self,
                            types=self._toList(types),
                            actions=self._toList(actions))
            efilters.append(f)

        return efilters

    def onPullRequest(self, payload):
        pass


def validate_conf(trigger_conf):
    """Validates the layout's trigger data."""
    events_with_actions = ('pull_request',)
    for event in trigger_conf:
        if event['event'] not in events_with_actions \
                and event.get('action', False):
            raise v.Invalid(
                "The event %s does not include action information, Zuul "
                "cannot use action filter 'action: %s'" %
                (event['event'], event['action']))


def getSchema():
    def toList(x):
        return v.Any([x], x)

    github_trigger = {
        v.Required('event'):
            toList(v.Any('pull_request')),
        'action': toList(str),
    }

    return github_trigger
