#!/usr/bin/env python

# Copyright 2012 Hewlett-Packard Development Company, L.P.
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

import os
import textwrap

import testtools

import zuul.configloader
from zuul.lib import encryption
from tests.base import AnsibleZuulTestCase, ZuulTestCase, FIXTURE_DIR


class TestMultipleTenants(AnsibleZuulTestCase):
    # A temporary class to hold new tests while others are disabled

    tenant_config_file = 'config/multi-tenant/main.yaml'

    def test_multiple_tenants(self):
        A = self.fake_gerrit.addFakeChange('org/project1', 'master', 'A')
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(self.getJobFromHistory('project1-test1').result,
                         'SUCCESS')
        self.assertEqual(self.getJobFromHistory('python27').result,
                         'SUCCESS')
        self.assertEqual(A.data['status'], 'MERGED')
        self.assertEqual(A.reported, 2,
                         "A should report start and success")
        self.assertIn('tenant-one-gate', A.messages[1],
                      "A should transit tenant-one gate")
        self.assertNotIn('tenant-two-gate', A.messages[1],
                         "A should *not* transit tenant-two gate")

        B = self.fake_gerrit.addFakeChange('org/project2', 'master', 'B')
        B.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(B.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(self.getJobFromHistory('python27',
                                                'org/project2').result,
                         'SUCCESS')
        self.assertEqual(self.getJobFromHistory('project2-test1').result,
                         'SUCCESS')
        self.assertEqual(B.data['status'], 'MERGED')
        self.assertEqual(B.reported, 2,
                         "B should report start and success")
        self.assertIn('tenant-two-gate', B.messages[1],
                      "B should transit tenant-two gate")
        self.assertNotIn('tenant-one-gate', B.messages[1],
                         "B should *not* transit tenant-one gate")

        self.assertEqual(A.reported, 2, "Activity in tenant two should"
                         "not affect tenant one")


class TestInRepoConfig(ZuulTestCase):
    # A temporary class to hold new tests while others are disabled

    tenant_config_file = 'config/in-repo/main.yaml'

    def test_in_repo_config(self):
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(self.getJobFromHistory('project-test1').result,
                         'SUCCESS')
        self.assertEqual(A.data['status'], 'MERGED')
        self.assertEqual(A.reported, 2,
                         "A should report start and success")
        self.assertIn('tenant-one-gate', A.messages[1],
                      "A should transit tenant-one gate")

    def test_dynamic_config(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                tenant-one-gate:
                  jobs:
                    - project-test2
            """)

        in_repo_playbook = textwrap.dedent(
            """
            - hosts: all
              tasks: []
            """)

        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(A.data['status'], 'MERGED')
        self.assertEqual(A.reported, 2,
                         "A should report start and success")
        self.assertIn('tenant-one-gate', A.messages[1],
                      "A should transit tenant-one gate")
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1')])

        self.fake_gerrit.addEvent(A.getChangeMergedEvent())
        self.waitUntilSettled()

        # Now that the config change is landed, it should be live for
        # subsequent changes.
        B = self.fake_gerrit.addFakeChange('org/project', 'master', 'B')
        B.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(B.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(self.getJobFromHistory('project-test2').result,
                         'SUCCESS')
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1'),
            dict(name='project-test2', result='SUCCESS', changes='2,1')])

    def test_dynamic_config_new_patchset(self):
        self.executor_server.hold_jobs_in_build = True

        tenant = self.sched.abide.tenants.get('tenant-one')
        check_pipeline = tenant.layout.pipelines['check']

        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                check:
                  jobs:
                    - project-test2
            """)

        in_repo_playbook = textwrap.dedent(
            """
            - hosts: all
              tasks: []
            """)

        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()

        items = check_pipeline.getAllItems()
        self.assertEqual(items[0].change.number, '1')
        self.assertEqual(items[0].change.patchset, '1')
        self.assertTrue(items[0].live)

        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                check:
                  jobs:
                    - project-test1
                    - project-test2
            """)
        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}

        A.addPatchset(files=file_dict)
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(2))

        self.waitUntilSettled()

        items = check_pipeline.getAllItems()
        self.assertEqual(items[0].change.number, '1')
        self.assertEqual(items[0].change.patchset, '2')
        self.assertTrue(items[0].live)

        self.executor_server.hold_jobs_in_build = False
        self.executor_server.release()
        self.waitUntilSettled()

    def test_dynamic_dependent_pipeline(self):
        # Test dynamically adding a project to a
        # dependent pipeline for the first time
        self.executor_server.hold_jobs_in_build = True

        tenant = self.sched.abide.tenants.get('tenant-one')
        gate_pipeline = tenant.layout.pipelines['gate']

        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                gate:
                  jobs:
                    - project-test2
            """)

        in_repo_playbook = textwrap.dedent(
            """
            - hosts: all
              tasks: []
            """)

        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('approved', 1)
        self.fake_gerrit.addEvent(A.addApproval('code-review', 2))
        self.waitUntilSettled()

        items = gate_pipeline.getAllItems()
        self.assertEqual(items[0].change.number, '1')
        self.assertEqual(items[0].change.patchset, '1')
        self.assertTrue(items[0].live)

        self.executor_server.hold_jobs_in_build = False
        self.executor_server.release()
        self.waitUntilSettled()

        # Make sure the dynamic queue got cleaned up
        self.assertEqual(gate_pipeline.queues, [])

    def test_in_repo_branch(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                tenant-one-gate:
                  jobs:
                    - project-test2
            """)

        in_repo_playbook = textwrap.dedent(
            """
            - hosts: all
              tasks: []
            """)

        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}
        self.create_branch('org/project', 'stable')
        A = self.fake_gerrit.addFakeChange('org/project', 'stable', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertEqual(A.data['status'], 'MERGED')
        self.assertEqual(A.reported, 2,
                         "A should report start and success")
        self.assertIn('tenant-one-gate', A.messages[1],
                      "A should transit tenant-one gate")
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1')])
        self.fake_gerrit.addEvent(A.getChangeMergedEvent())
        self.waitUntilSettled()

        # The config change should not affect master.
        B = self.fake_gerrit.addFakeChange('org/project', 'master', 'B')
        B.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(B.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1'),
            dict(name='project-test1', result='SUCCESS', changes='2,1')])

        # The config change should be live for further changes on
        # stable.
        C = self.fake_gerrit.addFakeChange('org/project', 'stable', 'C')
        C.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(C.addApproval('approved', 1))
        self.waitUntilSettled()
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1'),
            dict(name='project-test1', result='SUCCESS', changes='2,1'),
            dict(name='project-test2', result='SUCCESS', changes='3,1')])

    def test_crd_dynamic_config_branch(self):
        # Test that we can create a job in one repo and be able to use
        # it from a different branch on a different repo.

        self.create_branch('org/project1', 'stable')

        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2

            - project:
                name: org/project
                check:
                  jobs:
                    - project-test2
            """)

        in_repo_playbook = textwrap.dedent(
            """
            - hosts: all
              tasks: []
            """)

        file_dict = {'.zuul.yaml': in_repo_conf,
                     'playbooks/project-test2.yaml': in_repo_playbook}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)

        second_repo_conf = textwrap.dedent(
            """
            - project:
                name: org/project1
                check:
                  jobs:
                    - project-test2
            """)

        second_file_dict = {'.zuul.yaml': second_repo_conf}
        B = self.fake_gerrit.addFakeChange('org/project1', 'stable', 'B',
                                           files=second_file_dict)
        B.data['commitMessage'] = '%s\n\nDepends-On: %s\n' % (
            B.subject, A.data['id'])

        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        self.fake_gerrit.addEvent(B.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()

        self.assertEqual(A.reported, 1, "A should report")
        self.assertHistory([
            dict(name='project-test2', result='SUCCESS', changes='1,1'),
            dict(name='project-test2', result='SUCCESS', changes='1,1 2,1'),
        ])

    def test_untrusted_syntax_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2
                foo: error
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('syntax error', A.messages[0],
                      "A should have a syntax error reported")

    def test_trusted_syntax_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test2
                foo: error
            """)

        file_dict = {'zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('common-config', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('syntax error', A.messages[0],
                      "A should have a syntax error reported")

    def test_untrusted_yaml_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
            foo: error
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('syntax error', A.messages[0],
                      "A should have a syntax error reported")

    def test_untrusted_shadow_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - job:
                name: common-config-test
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('not permitted to shadow', A.messages[0],
                      "A should have a syntax error reported")

    def test_untrusted_pipeline_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - pipeline:
                name: test
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('Pipelines may not be defined', A.messages[0],
                      "A should have a syntax error reported")

    def test_untrusted_project_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - project:
                name: org/project1
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('the only project definition permitted', A.messages[0],
                      "A should have a syntax error reported")

    def test_duplicate_node_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - nodeset:
                name: duplicate
                nodes:
                  - name: compute
                    label: foo
                  - name: compute
                    label: foo
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('appears multiple times', A.messages[0],
                      "A should have a syntax error reported")

    def test_duplicate_group_error(self):
        in_repo_conf = textwrap.dedent(
            """
            - nodeset:
                name: duplicate
                nodes:
                  - name: compute
                    label: foo
                groups:
                  - name: group
                    nodes: compute
                  - name: group
                    nodes: compute
            """)

        file_dict = {'.zuul.yaml': in_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'NEW')
        self.assertEqual(A.reported, 1,
                         "A should report failure")
        self.assertIn('appears multiple times', A.messages[0],
                      "A should have a syntax error reported")

    def test_multi_repo(self):
        downstream_repo_conf = textwrap.dedent(
            """
            - project:
                name: org/project1
                tenant-one-gate:
                  jobs:
                    - project-test1

            - job:
                name: project1-test1
                parent: project-test1
            """)

        file_dict = {'.zuul.yaml': downstream_repo_conf}
        A = self.fake_gerrit.addFakeChange('org/project1', 'master', 'A',
                                           files=file_dict)
        A.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(A.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(A.data['status'], 'MERGED')
        self.fake_gerrit.addEvent(A.getChangeMergedEvent())
        self.waitUntilSettled()

        upstream_repo_conf = textwrap.dedent(
            """
            - job:
                name: project-test1

            - job:
                name: project-test2

            - project:
                name: org/project
                tenant-one-gate:
                  jobs:
                    - project-test1
            """)

        file_dict = {'.zuul.yaml': upstream_repo_conf}
        B = self.fake_gerrit.addFakeChange('org/project', 'master', 'B',
                                           files=file_dict)
        B.addApproval('code-review', 2)
        self.fake_gerrit.addEvent(B.addApproval('approved', 1))
        self.waitUntilSettled()

        self.assertEqual(B.data['status'], 'MERGED')
        self.fake_gerrit.addEvent(B.getChangeMergedEvent())
        self.waitUntilSettled()

        tenant = self.sched.abide.tenants.get('tenant-one')
        # Ensure the latest change is reflected in the config; if it
        # isn't this will raise an exception.
        tenant.layout.getJob('project-test2')


class TestAnsible(AnsibleZuulTestCase):
    # A temporary class to hold new tests while others are disabled

    tenant_config_file = 'config/ansible/main.yaml'

    def test_playbook(self):
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        build_timeout = self.getJobFromHistory('timeout')
        self.assertEqual(build_timeout.result, 'TIMED_OUT')
        build_faillocal = self.getJobFromHistory('faillocal')
        self.assertEqual(build_faillocal.result, 'FAILURE')
        build_failpost = self.getJobFromHistory('failpost')
        self.assertEqual(build_failpost.result, 'POST_FAILURE')
        build_check_vars = self.getJobFromHistory('check-vars')
        self.assertEqual(build_check_vars.result, 'SUCCESS')
        build_hello = self.getJobFromHistory('hello-world')
        self.assertEqual(build_hello.result, 'SUCCESS')
        build_python27 = self.getJobFromHistory('python27')
        self.assertEqual(build_python27.result, 'SUCCESS')
        flag_path = os.path.join(self.test_root, build_python27.uuid + '.flag')
        self.assertTrue(os.path.exists(flag_path))
        copied_path = os.path.join(self.test_root, build_python27.uuid +
                                   '.copied')
        self.assertTrue(os.path.exists(copied_path))
        failed_path = os.path.join(self.test_root, build_python27.uuid +
                                   '.failed')
        self.assertFalse(os.path.exists(failed_path))
        pre_flag_path = os.path.join(self.test_root, build_python27.uuid +
                                     '.pre.flag')
        self.assertTrue(os.path.exists(pre_flag_path))
        post_flag_path = os.path.join(self.test_root, build_python27.uuid +
                                      '.post.flag')
        self.assertTrue(os.path.exists(post_flag_path))
        bare_role_flag_path = os.path.join(self.test_root,
                                           build_python27.uuid +
                                           '.bare-role.flag')
        self.assertTrue(os.path.exists(bare_role_flag_path))

        secrets_path = os.path.join(self.test_root,
                                    build_python27.uuid + '.secrets')
        with open(secrets_path) as f:
            self.assertEqual(f.read(), "test-username test-password")

        msg = A.messages[0]
        success = "{} https://success.example.com/zuul-logs/{}"
        fail = "{} https://failure.example.com/zuul-logs/{}"
        self.assertIn(success.format("python27", build_python27.uuid), msg)
        self.assertIn(fail.format("faillocal", build_faillocal.uuid), msg)
        self.assertIn(success.format("check-vars", build_check_vars.uuid), msg)
        self.assertIn(success.format("hello-world", build_hello.uuid), msg)
        self.assertIn(fail.format("timeout", build_timeout.uuid), msg)
        self.assertIn(fail.format("failpost", build_failpost.uuid), msg)


class TestPrePlaybooks(AnsibleZuulTestCase):
    # A temporary class to hold new tests while others are disabled

    tenant_config_file = 'config/pre-playbook/main.yaml'

    def test_pre_playbook_fail(self):
        # Test that we run the post playbooks (but not the actual
        # playbook) when a pre-playbook fails.
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        build = self.getJobFromHistory('python27')
        self.assertIsNone(build.result)
        self.assertIn('RETRY_LIMIT', A.messages[0])
        flag_path = os.path.join(self.test_root, build.uuid +
                                 '.main.flag')
        self.assertFalse(os.path.exists(flag_path))
        pre_flag_path = os.path.join(self.test_root, build.uuid +
                                     '.pre.flag')
        self.assertFalse(os.path.exists(pre_flag_path))
        post_flag_path = os.path.join(self.test_root, build.uuid +
                                      '.post.flag')
        self.assertTrue(os.path.exists(post_flag_path))


class TestBrokenConfig(ZuulTestCase):
    # Test that we get an appropriate syntax error if we start with a
    # broken config.

    tenant_config_file = 'config/broken/main.yaml'

    def setUp(self):
        with testtools.ExpectedException(
                zuul.configloader.ConfigurationSyntaxError,
                "\nZuul encountered a syntax error"):
            super(TestBrokenConfig, self).setUp()

    def test_broken_config_on_startup(self):
        pass


class TestProjectKeys(ZuulTestCase):
    # Test that we can generate project keys

    # Normally the test infrastructure copies a static key in place
    # for each project before starting tests.  This saves time because
    # Zuul's automatic key-generation on startup can be slow.  To make
    # sure we exercise that code, in this test we allow Zuul to create
    # keys for the project on startup.
    create_project_keys = True
    tenant_config_file = 'config/in-repo/main.yaml'

    def test_key_generation(self):
        key_root = os.path.join(self.state_root, 'keys')
        private_key_file = os.path.join(key_root, 'gerrit/org/project.pem')
        # Make sure that a proper key was created on startup
        with open(private_key_file, "rb") as f:
            private_key, public_key = \
                encryption.deserialize_rsa_keypair(f.read())

        with open(os.path.join(FIXTURE_DIR, 'private.pem')) as i:
            fixture_private_key = i.read()

        # Make sure that we didn't just end up with the static fixture
        # key
        self.assertNotEqual(fixture_private_key, private_key)

        # Make sure it's the right length
        self.assertEqual(4096, private_key.key_size)


class TestRoles(ZuulTestCase):
    tenant_config_file = 'config/roles/main.yaml'

    def test_role(self):
        # This exercises a proposed change to a role being checked out
        # and used.
        A = self.fake_gerrit.addFakeChange('bare-role', 'master', 'A')
        B = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        B.data['commitMessage'] = '%s\n\nDepends-On: %s\n' % (
            B.subject, A.data['id'])
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.fake_gerrit.addEvent(B.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        self.assertHistory([
            dict(name='project-test', result='SUCCESS', changes='1,1 2,1'),
        ])


class TestShadow(ZuulTestCase):
    tenant_config_file = 'config/shadow/main.yaml'

    def test_shadow(self):
        # Test that a repo is allowed to shadow another's job definitions.
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        self.assertHistory([
            dict(name='test1', result='SUCCESS', changes='1,1'),
            dict(name='test2', result='SUCCESS', changes='1,1'),
        ], ordered=False)


class TestDataReturn(AnsibleZuulTestCase):
    tenant_config_file = 'config/data-return/main.yaml'

    def test_data_return(self):
        # This exercises a proposed change to a role being checked out
        # and used.
        A = self.fake_gerrit.addFakeChange('org/project', 'master', 'A')
        self.fake_gerrit.addEvent(A.getPatchsetCreatedEvent(1))
        self.waitUntilSettled()
        self.assertHistory([
            dict(name='data-return', result='SUCCESS', changes='1,1'),
        ])
        self.assertIn('- data-return test/log/url',
                      A.messages[-1])
