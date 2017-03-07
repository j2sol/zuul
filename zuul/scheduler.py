# Copyright 2012-2015 Hewlett-Packard Development Company, L.P.
# Copyright 2013 OpenStack Foundation
# Copyright 2013 Antoine "hashar" Musso
# Copyright 2013 Wikimedia Foundation Inc.
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

import extras
import json
import logging
import os
import pickle
import six
from six.moves import queue as Queue
import socket
import sys
import threading
import time

from zuul import configloader
from zuul import model
from zuul import exceptions
from zuul import version as zuul_version


class MutexHandler(object):
    log = logging.getLogger("zuul.MutexHandler")

    def __init__(self):
        self.mutexes = {}

    def acquire(self, item, job):
        if not job.mutex:
            return True
        mutex_name = job.mutex
        m = self.mutexes.get(mutex_name)
        if not m:
            # The mutex is not held, acquire it
            self._acquire(mutex_name, item, job.name)
            return True
        held_item, held_job_name = m
        if held_item is item and held_job_name == job.name:
            # This item already holds the mutex
            return True
        held_build = held_item.current_build_set.getBuild(held_job_name)
        if held_build and held_build.result:
            # The build that held the mutex is complete, release it
            # and let the new item have it.
            self.log.error("Held mutex %s being released because "
                           "the build that holds it is complete" %
                           (mutex_name,))
            self._release(mutex_name, item, job.name)
            self._acquire(mutex_name, item, job.name)
            return True
        return False

    def release(self, item, job):
        if not job.mutex:
            return
        mutex_name = job.mutex
        m = self.mutexes.get(mutex_name)
        if not m:
            # The mutex is not held, nothing to do
            self.log.error("Mutex can not be released for %s "
                           "because the mutex is not held" %
                           (item,))
            return
        held_item, held_job_name = m
        if held_item is item and held_job_name == job.name:
            # This item holds the mutex
            self._release(mutex_name, item, job.name)
            return
        self.log.error("Mutex can not be released for %s "
                       "which does not hold it" %
                       (item,))

    def _acquire(self, mutex_name, item, job_name):
        self.log.debug("Job %s of item %s acquiring mutex %s" %
                       (job_name, item, mutex_name))
        self.mutexes[mutex_name] = (item, job_name)

    def _release(self, mutex_name, item, job_name):
        self.log.debug("Job %s of item %s releasing mutex %s" %
                       (job_name, item, mutex_name))
        del self.mutexes[mutex_name]


class ManagementEvent(object):
    """An event that should be processed within the main queue run loop"""
    def __init__(self):
        self._wait_event = threading.Event()
        self._exc_info = None

    def exception(self, exc_info):
        self._exc_info = exc_info
        self._wait_event.set()

    def done(self):
        self._wait_event.set()

    def wait(self, timeout=None):
        self._wait_event.wait(timeout)
        if self._exc_info:
            six.reraise(*self._exc_info)
        return self._wait_event.is_set()


class ReconfigureEvent(ManagementEvent):
    """Reconfigure the scheduler.  The layout will be (re-)loaded from
    the path specified in the configuration.

    :arg ConfigParser config: the new configuration
    """
    def __init__(self, config):
        super(ReconfigureEvent, self).__init__()
        self.config = config


class TenantReconfigureEvent(ManagementEvent):
    """Reconfigure the given tenant.  The layout will be (re-)loaded from
    the path specified in the configuration.

    :arg Tenant tenant: the tenant to reconfigure
    """
    def __init__(self, tenant):
        super(TenantReconfigureEvent, self).__init__()
        self.tenant = tenant


class PromoteEvent(ManagementEvent):
    """Promote one or more changes to the head of the queue.

    :arg str tenant_name: the name of the tenant
    :arg str pipeline_name: the name of the pipeline
    :arg list change_ids: a list of strings of change ids in the form
        1234,1
    """

    def __init__(self, tenant_name, pipeline_name, change_ids):
        super(PromoteEvent, self).__init__()
        self.tenant_name = tenant_name
        self.pipeline_name = pipeline_name
        self.change_ids = change_ids


class EnqueueEvent(ManagementEvent):
    """Enqueue a change into a pipeline

    :arg TriggerEvent trigger_event: a TriggerEvent describing the
        trigger, pipeline, and change to enqueue
    """

    def __init__(self, trigger_event):
        super(EnqueueEvent, self).__init__()
        self.trigger_event = trigger_event


class ResultEvent(object):
    """An event that needs to modify the pipeline state due to a
    result from an external system."""

    pass


class BuildStartedEvent(ResultEvent):
    """A build has started.

    :arg Build build: The build which has started.
    """

    def __init__(self, build):
        self.build = build


class BuildCompletedEvent(ResultEvent):
    """A build has completed

    :arg Build build: The build which has completed.
    """

    def __init__(self, build):
        self.build = build


class MergeCompletedEvent(ResultEvent):
    """A remote merge operation has completed

    :arg BuildSet build_set: The build_set which is ready.
    :arg str zuul_url: The URL of the Zuul Merger.
    :arg bool merged: Whether the merge succeeded (changes with refs).
    :arg bool updated: Whether the repo was updated (changes without refs).
    :arg str commit: The SHA of the merged commit (changes with refs).
    """

    def __init__(self, build_set, zuul_url, merged, updated, commit,
                 files):
        self.build_set = build_set
        self.zuul_url = zuul_url
        self.merged = merged
        self.updated = updated
        self.commit = commit
        self.files = files


class NodesProvisionedEvent(ResultEvent):
    """Nodes have been provisioned for a build_set

    :arg BuildSet build_set: The build_set which has nodes.
    :arg list of Node objects nodes: The provisioned nodes
    """

    def __init__(self, request):
        self.request = request


def toList(item):
    if not item:
        return []
    if isinstance(item, list):
        return item
    return [item]


class Scheduler(threading.Thread):
    """The engine of Zuul.

    The Scheduler is reponsible for recieving events and dispatching
    them to appropriate components (including pipeline managers,
    mergers and launchers).

    It runs a single threaded main loop which processes events
    received one at a time and takes action as appropriate.  Other
    parts of Zuul may run in their own thread, but synchronization is
    performed within the scheduler to reduce or eliminate the need for
    locking in most circumstances.

    The main daemon will have one instance of the Scheduler class
    running which will persist for the life of the process.  The
    Scheduler instance is supplied to other Zuul components so that
    they can submit events or otherwise communicate with other
    components.

    """

    log = logging.getLogger("zuul.Scheduler")

    def __init__(self, config, testonly=False):
        threading.Thread.__init__(self)
        self.daemon = True
        self.hostname = socket.gethostname()
        self.wake_event = threading.Event()
        self.layout_lock = threading.Lock()
        self.run_handler_lock = threading.Lock()
        self._pause = False
        self._exit = False
        self._stopped = False
        self.launcher = None
        self.merger = None
        self.connections = None
        self.statsd = extras.try_import('statsd.statsd')
        # TODO(jeblair): fix this
        self.mutex = MutexHandler()
        # Despite triggers being part of the pipeline, there is one trigger set
        # per scheduler. The pipeline handles the trigger filters but since
        # the events are handled by the scheduler itself it needs to handle
        # the loading of the triggers.
        # self.triggers['connection_name'] = triggerObject
        self.triggers = dict()
        self.config = config

        self.trigger_event_queue = Queue.Queue()
        self.result_event_queue = Queue.Queue()
        self.management_event_queue = Queue.Queue()
        self.abide = model.Abide()

        if not testonly:
            time_dir = self._get_time_database_dir()
            self.time_database = model.TimeDataBase(time_dir)

        self.zuul_version = zuul_version.version_info.release_string()
        self.last_reconfigured = None

    def stop(self):
        self._stopped = True
        self.stopConnections()
        self.wake_event.set()

    def registerConnections(self, connections, webapp, load=True):
        # load: whether or not to trigger the onLoad for the connection. This
        # is useful for not doing a full load during layout validation.
        self.connections = connections
        self.connections.registerWebapp(webapp)
        self.connections.registerScheduler(self, load)

    def stopConnections(self):
        self.connections.stop()

    def setLauncher(self, launcher):
        self.launcher = launcher

    def setMerger(self, merger):
        self.merger = merger

    def setNodepool(self, nodepool):
        self.nodepool = nodepool

    def setZooKeeper(self, zk):
        self.zk = zk

    def addEvent(self, event):
        self.log.debug("Adding trigger event: %s" % event)
        try:
            if self.statsd:
                self.statsd.incr('gerrit.event.%s' % event.type)
        except:
            self.log.exception("Exception reporting event stats")
        self.trigger_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Done adding trigger event: %s" % event)

    def onBuildStarted(self, build):
        self.log.debug("Adding start event for build: %s" % build)
        build.start_time = time.time()
        event = BuildStartedEvent(build)
        self.result_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Done adding start event for build: %s" % build)

    def onBuildCompleted(self, build, result):
        self.log.debug("Adding complete event for build: %s result: %s" % (
            build, result))
        build.end_time = time.time()
        # Note, as soon as the result is set, other threads may act
        # upon this, even though the event hasn't been fully
        # processed.  Ensure that any other data from the event (eg,
        # timing) is recorded before setting the result.
        build.result = result
        try:
            if self.statsd and build.pipeline:
                jobname = build.job.name.replace('.', '_')
                key = 'zuul.pipeline.%s.all_jobs' % build.pipeline.name
                self.statsd.incr(key)
                for label in build.node_labels:
                    # Jenkins includes the node name in its list of labels, so
                    # we filter it out here, since that is not statistically
                    # interesting.
                    if label == build.node_name:
                        continue
                    dt = int((build.start_time - build.launch_time) * 1000)
                    key = 'zuul.pipeline.%s.label.%s.wait_time' % (
                        build.pipeline.name, label)
                    self.statsd.timing(key, dt)
                key = 'zuul.pipeline.%s.job.%s.%s' % (build.pipeline.name,
                                                      jobname, build.result)
                if build.result in ['SUCCESS', 'FAILURE'] and build.start_time:
                    dt = int((build.end_time - build.start_time) * 1000)
                    self.statsd.timing(key, dt)
                self.statsd.incr(key)

                key = 'zuul.pipeline.%s.job.%s.wait_time' % (
                    build.pipeline.name, jobname)
                dt = int((build.start_time - build.launch_time) * 1000)
                self.statsd.timing(key, dt)
        except:
            self.log.exception("Exception reporting runtime stats")
        event = BuildCompletedEvent(build)
        self.result_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Done adding complete event for build: %s" % build)

    def onMergeCompleted(self, build_set, zuul_url, merged, updated,
                         commit, files):
        self.log.debug("Adding merge complete event for build set: %s" %
                       build_set)
        event = MergeCompletedEvent(build_set, zuul_url, merged,
                                    updated, commit, files)
        self.result_event_queue.put(event)
        self.wake_event.set()

    def onNodesProvisioned(self, req):
        self.log.debug("Adding nodes provisioned event for build set: %s" %
                       req.build_set)
        event = NodesProvisionedEvent(req)
        self.result_event_queue.put(event)
        self.wake_event.set()

    def reconfigureTenant(self, tenant):
        self.log.debug("Prepare to reconfigure")
        event = TenantReconfigureEvent(tenant)
        self.management_event_queue.put(event)
        self.wake_event.set()

    def reconfigure(self, config):
        self.log.debug("Prepare to reconfigure")
        event = ReconfigureEvent(config)
        self.management_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Waiting for reconfiguration")
        event.wait()
        self.log.debug("Reconfiguration complete")
        self.last_reconfigured = int(time.time())
        # TODOv3(jeblair): reconfigure time should be per-tenant

    def promote(self, tenant_name, pipeline_name, change_ids):
        event = PromoteEvent(tenant_name, pipeline_name, change_ids)
        self.management_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Waiting for promotion")
        event.wait()
        self.log.debug("Promotion complete")

    def enqueue(self, trigger_event):
        event = EnqueueEvent(trigger_event)
        self.management_event_queue.put(event)
        self.wake_event.set()
        self.log.debug("Waiting for enqueue")
        event.wait()
        self.log.debug("Enqueue complete")

    def exit(self):
        self.log.debug("Prepare to exit")
        self._pause = True
        self._exit = True
        self.wake_event.set()
        self.log.debug("Waiting for exit")

    def _get_queue_pickle_file(self):
        if self.config.has_option('zuul', 'state_dir'):
            state_dir = os.path.expanduser(self.config.get('zuul',
                                                           'state_dir'))
        else:
            state_dir = '/var/lib/zuul'
        return os.path.join(state_dir, 'queue.pickle')

    def _get_time_database_dir(self):
        if self.config.has_option('zuul', 'state_dir'):
            state_dir = os.path.expanduser(self.config.get('zuul',
                                                           'state_dir'))
        else:
            state_dir = '/var/lib/zuul'
        d = os.path.join(state_dir, 'times')
        if not os.path.exists(d):
            os.mkdir(d)
        return d

    def _save_queue(self):
        pickle_file = self._get_queue_pickle_file()
        events = []
        while not self.trigger_event_queue.empty():
            events.append(self.trigger_event_queue.get())
        self.log.debug("Queue length is %s" % len(events))
        if events:
            self.log.debug("Saving queue")
            pickle.dump(events, open(pickle_file, 'wb'))

    def _load_queue(self):
        pickle_file = self._get_queue_pickle_file()
        if os.path.exists(pickle_file):
            self.log.debug("Loading queue")
            events = pickle.load(open(pickle_file, 'rb'))
            self.log.debug("Queue length is %s" % len(events))
            for event in events:
                self.trigger_event_queue.put(event)
        else:
            self.log.debug("No queue file found")

    def _delete_queue(self):
        pickle_file = self._get_queue_pickle_file()
        if os.path.exists(pickle_file):
            self.log.debug("Deleting saved queue")
            os.unlink(pickle_file)

    def resume(self):
        try:
            self._load_queue()
        except:
            self.log.exception("Unable to load queue")
        try:
            self._delete_queue()
        except:
            self.log.exception("Unable to delete saved queue")
        self.log.debug("Resuming queue processing")
        self.wake_event.set()

    def _doPauseEvent(self):
        if self._exit:
            self.log.debug("Exiting")
            self._save_queue()
            os._exit(0)

    def _doReconfigureEvent(self, event):
        # This is called in the scheduler loop after another thread submits
        # a request
        self.layout_lock.acquire()
        self.config = event.config
        try:
            self.log.debug("Performing reconfiguration")
            loader = configloader.ConfigLoader()
            abide = loader.loadConfig(
                self.config.get('zuul', 'tenant_config'),
                self, self.merger, self.connections)
            for tenant in abide.tenants.values():
                self._reconfigureTenant(tenant)
            self.abide = abide
        finally:
            self.layout_lock.release()

    def _doTenantReconfigureEvent(self, event):
        # This is called in the scheduler loop after another thread submits
        # a request
        self.layout_lock.acquire()
        try:
            self.log.debug("Performing tenant reconfiguration")
            loader = configloader.ConfigLoader()
            abide = loader.reloadTenant(
                self.config.get('zuul', 'tenant_config'),
                self, self.merger, self.connections,
                self.abide, event.tenant)
            tenant = abide.tenants[event.tenant.name]
            self._reconfigureTenant(tenant)
            self.abide = abide
        finally:
            self.layout_lock.release()

    def _reenqueueTenant(self, old_tenant, tenant):
        for name, new_pipeline in tenant.layout.pipelines.items():
            old_pipeline = old_tenant.layout.pipelines.get(name)
            if not old_pipeline:
                self.log.warning("No old pipeline matching %s found "
                                 "when reconfiguring" % name)
                continue
            self.log.debug("Re-enqueueing changes for pipeline %s" % name)
            items_to_remove = []
            builds_to_cancel = []
            last_head = None
            for shared_queue in old_pipeline.queues:
                for item in shared_queue.queue:
                    if not item.item_ahead:
                        last_head = item
                    item.item_ahead = None
                    item.items_behind = []
                    item.pipeline = None
                    item.queue = None
                    project_name = item.change.project.name
                    item.change.project = new_pipeline.source.getProject(
                        project_name)
                    if new_pipeline.manager.reEnqueueItem(item,
                                                          last_head):
                        new_jobs = item.getJobs()
                        for build in item.current_build_set.getBuilds():
                            jobtree = item.job_tree.getJobTreeForJob(build.job)
                            if jobtree and jobtree.job in new_jobs:
                                build.job = jobtree.job
                            else:
                                item.removeBuild(build)
                                builds_to_cancel.append(build)
                    else:
                        items_to_remove.append(item)
            for item in items_to_remove:
                for build in item.current_build_set.getBuilds():
                    builds_to_cancel.append(build)
            for build in builds_to_cancel:
                self.log.warning(
                    "Canceling build %s during reconfiguration" % (build,))
                try:
                    self.launcher.cancel(build)
                except Exception:
                    self.log.exception(
                        "Exception while canceling build %s "
                        "for change %s" % (build, item.change))
                finally:
                    self.mutex.release(build.build_set.item, build.job)

    def _reconfigureTenant(self, tenant):
        # This is called from _doReconfigureEvent while holding the
        # layout lock
        old_tenant = self.abide.tenants.get(tenant.name)
        if old_tenant:
            self._reenqueueTenant(old_tenant, tenant)
        # TODOv3(jeblair): update for tenants
        # self.maintainConnectionCache()
        self.connections.reconfigureDrivers(tenant)
        # TODOv3(jeblair): remove postconfig calls?
        for pipeline in tenant.layout.pipelines.values():
            pipeline.source.postConfig()
            for trigger in pipeline.triggers:
                trigger.postConfig(pipeline)
            for reporter in pipeline.actions:
                reporter.postConfig()
        if self.statsd:
            try:
                for pipeline in tenant.layout.pipelines.values():
                    items = len(pipeline.getAllItems())
                    # stats.gauges.zuul.pipeline.NAME.current_changes
                    key = 'zuul.pipeline.%s' % pipeline.name
                    self.statsd.gauge(key + '.current_changes', items)
            except Exception:
                self.log.exception("Exception reporting initial "
                                   "pipeline stats:")

    def _doPromoteEvent(self, event):
        tenant = self.abide.tenants.get(event.tenant_name)
        pipeline = tenant.layout.pipelines[event.pipeline_name]
        change_ids = [c.split(',') for c in event.change_ids]
        items_to_enqueue = []
        change_queue = None
        for shared_queue in pipeline.queues:
            if change_queue:
                break
            for item in shared_queue.queue:
                if (item.change.number == change_ids[0][0] and
                        item.change.patchset == change_ids[0][1]):
                    change_queue = shared_queue
                    break
        if not change_queue:
            raise Exception("Unable to find shared change queue for %s" %
                            event.change_ids[0])
        for number, patchset in change_ids:
            found = False
            for item in change_queue.queue:
                if (item.change.number == number and
                        item.change.patchset == patchset):
                    found = True
                    items_to_enqueue.append(item)
                    break
            if not found:
                raise Exception("Unable to find %s,%s in queue %s" %
                                (number, patchset, change_queue))
        for item in change_queue.queue[:]:
            if item not in items_to_enqueue:
                items_to_enqueue.append(item)
            pipeline.manager.cancelJobs(item)
            pipeline.manager.dequeueItem(item)
        for item in items_to_enqueue:
            pipeline.manager.addChange(
                item.change,
                enqueue_time=item.enqueue_time,
                quiet=True,
                ignore_requirements=True)

    def _doEnqueueEvent(self, event):
        tenant = self.abide.tenants.get(event.tenant_name)
        project = tenant.layout.project_configs.get(event.project_name)
        pipeline = tenant.layout.pipelines[event.forced_pipeline]
        change = pipeline.source.getChange(event, project)
        self.log.debug("Event %s for change %s was directly assigned "
                       "to pipeline %s" % (event, change, self))
        pipeline.manager.addChange(change, ignore_requirements=True)

    def _areAllBuildsComplete(self):
        self.log.debug("Checking if all builds are complete")
        if self.merger.areMergesOutstanding():
            self.log.debug("Waiting on merger")
            return False
        waiting = False
        for tenant in self.abide.tenants.values():
            for pipeline in tenant.layout.pipelines.values():
                for item in pipeline.getAllItems():
                    for build in item.current_build_set.getBuilds():
                        if build.result is None:
                            self.log.debug("%s waiting on %s" %
                                           (pipeline.manager, build))
                            waiting = True
        if not waiting:
            self.log.debug("All builds are complete")
            return True
        return False

    def run(self):
        if self.statsd:
            self.log.debug("Statsd enabled")
        else:
            self.log.debug("Statsd disabled because python statsd "
                           "package not found")
        while True:
            self.log.debug("Run handler sleeping")
            self.wake_event.wait()
            self.wake_event.clear()
            if self._stopped:
                self.log.debug("Run handler stopping")
                return
            self.log.debug("Run handler awake")
            self.run_handler_lock.acquire()
            try:
                while not self.management_event_queue.empty():
                    self.process_management_queue()

                # Give result events priority -- they let us stop builds,
                # whereas trigger events cause us to launch builds.
                while not self.result_event_queue.empty():
                    self.process_result_queue()

                if not self._pause:
                    while not self.trigger_event_queue.empty():
                        self.process_event_queue()

                if self._pause and self._areAllBuildsComplete():
                    self._doPauseEvent()

                for tenant in self.abide.tenants.values():
                    for pipeline in tenant.layout.pipelines.values():
                        while pipeline.manager.processQueue():
                            pass

            except Exception:
                self.log.exception("Exception in run handler:")
                # There may still be more events to process
                self.wake_event.set()
            finally:
                self.run_handler_lock.release()

    def maintainConnectionCache(self):
        # TODOv3(jeblair): update for tenants
        relevant = set()
        for tenant in self.abide.tenants.values():
            for pipeline in tenant.layout.pipelines.values():
                self.log.debug("Gather relevant cache items for: %s" %
                               pipeline)

                for item in pipeline.getAllItems():
                    relevant.add(item.change)
                    relevant.update(item.change.getRelatedChanges())
        for connection in self.connections.values():
            connection.maintainCache(relevant)
            self.log.debug(
                "End maintain connection cache for: %s" % connection)
        self.log.debug("Connection cache size: %s" % len(relevant))

    def process_event_queue(self):
        self.log.debug("Fetching trigger event")
        event = self.trigger_event_queue.get()
        self.log.debug("Processing trigger event %s" % event)
        try:
            for tenant in self.abide.tenants.values():
                reconfigured_tenant = False
                for pipeline in tenant.layout.pipelines.values():
                    # Get the change even if the project is unknown to
                    # us for the use of updating the cache if there is
                    # another change depending on this foreign one.
                    try:
                        change = pipeline.source.getChange(event)
                    except exceptions.ChangeNotFound as e:
                        self.log.debug("Unable to get change %s from "
                                       "source %s (most likely looking "
                                       "for a change from another "
                                       "connection trigger)",
                                       e.change, pipeline.source)
                        continue
                    if (event.type == 'change-merged' and
                        hasattr(change, 'files') and
                        not reconfigured_tenant and
                        change.updatesConfig()):
                        # The change that just landed updates the config.
                        # Clear out cached data for this project and
                        # perform a reconfiguration.
                        change.project.unparsed_config = None
                        self.reconfigureTenant(tenant)
                        reconfigured_tenant = True
                    if event.type == 'patchset-created':
                        pipeline.manager.removeOldVersionsOfChange(change)
                    elif event.type == 'change-abandoned':
                        pipeline.manager.removeAbandonedChange(change)
                    if pipeline.manager.eventMatches(event, change):
                        pipeline.manager.addChange(change)
        finally:
            self.trigger_event_queue.task_done()

    def process_management_queue(self):
        self.log.debug("Fetching management event")
        event = self.management_event_queue.get()
        self.log.debug("Processing management event %s" % event)
        try:
            if isinstance(event, ReconfigureEvent):
                self._doReconfigureEvent(event)
            elif isinstance(event, TenantReconfigureEvent):
                self._doTenantReconfigureEvent(event)
            elif isinstance(event, PromoteEvent):
                self._doPromoteEvent(event)
            elif isinstance(event, EnqueueEvent):
                self._doEnqueueEvent(event.trigger_event)
            else:
                self.log.error("Unable to handle event %s" % event)
            event.done()
        except Exception:
            event.exception(sys.exc_info())
        self.management_event_queue.task_done()

    def process_result_queue(self):
        self.log.debug("Fetching result event")
        event = self.result_event_queue.get()
        self.log.debug("Processing result event %s" % event)
        try:
            if isinstance(event, BuildStartedEvent):
                self._doBuildStartedEvent(event)
            elif isinstance(event, BuildCompletedEvent):
                self._doBuildCompletedEvent(event)
            elif isinstance(event, MergeCompletedEvent):
                self._doMergeCompletedEvent(event)
            elif isinstance(event, NodesProvisionedEvent):
                self._doNodesProvisionedEvent(event)
            else:
                self.log.error("Unable to handle event %s" % event)
        finally:
            self.result_event_queue.task_done()

    def _doBuildStartedEvent(self, event):
        build = event.build
        if build.build_set is not build.build_set.item.current_build_set:
            self.log.warning("Build %s is not in the current build set" %
                             (build,))
            return
        pipeline = build.build_set.item.pipeline
        if not pipeline:
            self.log.warning("Build %s is not associated with a pipeline" %
                             (build,))
            return
        try:
            build.estimated_time = float(self.time_database.getEstimatedTime(
                build.job.name))
        except Exception:
            self.log.exception("Exception estimating build time:")
        pipeline.manager.onBuildStarted(event.build)

    def _doBuildCompletedEvent(self, event):
        build = event.build

        # Regardless of any other conditions which might cause us not
        # to pass this on to the pipeline manager, make sure we return
        # the nodes to nodepool.
        try:
            nodeset = build.build_set.getJobNodeSet(build.job.name)
            self.nodepool.returnNodeSet(nodeset)
        except Exception:
            self.log.exception("Unable to return nodeset %s" % (nodeset,))

        if build.build_set is not build.build_set.item.current_build_set:
            self.log.debug("Build %s is not in the current build set" %
                           (build,))
            return
        pipeline = build.build_set.item.pipeline
        if not pipeline:
            self.log.warning("Build %s is not associated with a pipeline" %
                             (build,))
            return
        if build.end_time and build.start_time and build.result:
            duration = build.end_time - build.start_time
            try:
                self.time_database.update(
                    build.job.name, duration, build.result)
            except Exception:
                self.log.exception("Exception recording build time:")
        pipeline.manager.onBuildCompleted(event.build)

    def _doMergeCompletedEvent(self, event):
        build_set = event.build_set
        if build_set is not build_set.item.current_build_set:
            self.log.warning("Build set %s is not current" % (build_set,))
            return
        pipeline = build_set.item.pipeline
        if not pipeline:
            self.log.warning("Build set %s is not associated with a pipeline" %
                             (build_set,))
            return
        pipeline.manager.onMergeCompleted(event)

    def _doNodesProvisionedEvent(self, event):
        request = event.request
        build_set = request.build_set

        self.nodepool.acceptNodes(request)

        if build_set is not build_set.item.current_build_set:
            self.log.warning("Build set %s is not current" % (build_set,))
            if request.fulfilled:
                self.nodepool.returnNodeSet(request.nodeset)
            return
        pipeline = build_set.item.pipeline
        if not pipeline:
            self.log.warning("Build set %s is not associated with a pipeline" %
                             (build_set,))
            if request.fulfilled:
                self.nodepool.returnNodeSet(request.nodeset)
            return
        pipeline.manager.onNodesProvisioned(event)

    def formatStatusJSON(self, tenant_name):
        # TODOv3(jeblair): use tenants
        if self.config.has_option('zuul', 'url_pattern'):
            url_pattern = self.config.get('zuul', 'url_pattern')
        else:
            url_pattern = None

        data = {}

        data['zuul_version'] = self.zuul_version

        if self._pause:
            ret = '<p><b>Queue only mode:</b> preparing to '
            if self._exit:
                ret += 'exit'
            ret += ', queue length: %s' % self.trigger_event_queue.qsize()
            ret += '</p>'
            data['message'] = ret

        data['trigger_event_queue'] = {}
        data['trigger_event_queue']['length'] = \
            self.trigger_event_queue.qsize()
        data['result_event_queue'] = {}
        data['result_event_queue']['length'] = \
            self.result_event_queue.qsize()

        if self.last_reconfigured:
            data['last_reconfigured'] = self.last_reconfigured * 1000

        pipelines = []
        data['pipelines'] = pipelines
        tenant = self.abide.tenants.get(tenant_name)
        for pipeline in tenant.layout.pipelines.values():
            pipelines.append(pipeline.formatStatusJSON(url_pattern))
        return json.dumps(data)
