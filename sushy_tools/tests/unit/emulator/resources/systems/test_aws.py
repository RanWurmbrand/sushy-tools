# Copyright 2026 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from unittest import mock
import uuid as uuid_lib

import boto3
from botocore import stub
from oslotest import base

from sushy_tools.emulator.resources.systems import awsdriver
from sushy_tools.emulator.resources.systems.awsdriver import AwsDriver
from sushy_tools import error


class AwsDriverTestCase(base.BaseTestCase):
    """Tests against a stubbed EC2 client

    ``Stubber`` validates every request against the real EC2 service model,
    so a call with a parameter EC2 does not accept fails the test rather than
    silently passing as it would against a bare `mock.Mock`.
    """

    instance_id = 'i-0123456789abcdef0'
    region = 'us-east-1'

    def setUp(self):
        super().setUp()

        self.ec2 = boto3.client(
            'ec2', region_name=self.region,
            aws_access_key_id='testing',
            aws_secret_access_key='testing',
            aws_session_token='testing')

        self.stubber = stub.Stubber(self.ec2)
        self.stubber.activate()
        self.addCleanup(self.stubber.deactivate)

        # `_ec2` and `_executor` live on the class (not on any single object)
        # so they need clearing between tests
        for attr in ('_ec2', '_executor'):
            if hasattr(AwsDriver, attr):
                delattr(AwsDriver, attr)

        awsdriver.FUTURES.clear()
        self.addCleanup(awsdriver.FUTURES.clear)

        AwsDriver._ec2 = self.ec2

        self.logger = mock.MagicMock()
        self.test_driver = AwsDriver.initialize(
            {}, self.logger, self.region)()

    def _instance(self, state='running', instance_id=None, **extra):
        instance = {'InstanceId': instance_id or self.instance_id,
                    'State': {'Name': state, 'Code': 16}}
        instance.update(extra)
        return instance

    def _describe_response(self, *instances):
        return {'Reservations': [{'Instances': list(instances)}]}

    def _expect_describe(self, *instances, **expected_params):
        params = expected_params or {'InstanceIds': [self.instance_id]}
        self.stubber.add_response(
            'describe_instances', self._describe_response(*instances), params)

    def _expect_not_found(self):
        self.stubber.add_client_error(
            'describe_instances', service_error_code='InvalidInstanceID'
                                                     '.NotFound',
            http_status_code=400)

    # -- identity ---------------------------------------------------------

    def test_driver_description(self):
        self.assertEqual('<AWS EC2>', self.test_driver.driver)

    def test_uuid_is_a_well_formed_uuid(self):
        """The rest of the emulator parses this value with uuid.UUID()"""
        self._expect_describe(self._instance())

        self.assertEqual(
            str(uuid_lib.uuid5(awsdriver.UUID_NAMESPACE, self.instance_id)),
            self.test_driver.uuid(self.instance_id))
        self.stubber.assert_no_pending_responses()

    def test_uuid_is_stable(self):
        for _ in range(2):
            self._expect_describe(self._instance())

        self.assertEqual(self.test_driver.uuid(self.instance_id),
                         self.test_driver.uuid(self.instance_id))

    def test_name_from_tag(self):
        self._expect_describe(
            self._instance(Tags=[{'Key': 'role', 'Value': 'worker'},
                                 {'Key': 'Name', 'Value': 'worker-0'}]))

        self.assertEqual('worker-0', self.test_driver.name(self.instance_id))

    def test_name_falls_back_to_id(self):
        self._expect_describe(self._instance())

        self.assertEqual(self.instance_id,
                         self.test_driver.name(self.instance_id))

    def test_not_found_raises(self):
        self._expect_not_found()

        self.assertRaises(error.NotFound,
                          self.test_driver.uuid, self.instance_id)

    # -- systems listing --------------------------------------------------

    def test_systems(self):
        self.stubber.add_response(
            'describe_instances',
            self._describe_response(self._instance(instance_id='i-aaa'),
                                    self._instance(instance_id='i-bbb')),
            {'Filters': [{'Name': 'instance-state-name',
                          'Values': ['pending', 'running', 'shutting-down',
                                     'stopped', 'stopping']}]})

        self.assertEqual(['i-aaa', 'i-bbb'], self.test_driver.systems)
        self.stubber.assert_no_pending_responses()

    def test_systems_excludes_terminated(self):
        """`terminated` must not be among the states we ask EC2 for"""
        self.assertNotIn('terminated', AwsDriver.EC2_STATES_ALIVE)

    def test_systems_filters_by_tag(self):
        driver = AwsDriver.initialize(
            {'SUSHY_EMULATOR_AWS_TAG': 'rhwa-lab=true'},
            self.logger, self.region)()

        self.stubber.add_response(
            'describe_instances',
            self._describe_response(self._instance(instance_id='i-aaa')),
            {'Filters': [{'Name': 'tag:rhwa-lab', 'Values': ['true']},
                         {'Name': 'instance-state-name',
                          'Values': ['pending', 'running', 'shutting-down',
                                     'stopped', 'stopping']}]})

        self.assertEqual(['i-aaa'], driver.systems)
        self.stubber.assert_no_pending_responses()

    def test_parse_tags_dict(self):
        self.assertEqual({'a': 'b', 'c': 'd'},
                         AwsDriver._parse_tags({'a': 'b', 'c': 'd'}))

    def test_parse_tags_multiple(self):
        self.assertEqual({'a': 'b', 'c': 'd'},
                         AwsDriver._parse_tags('a=b, c=d'))

    def test_parse_tags_empty(self):
        self.assertEqual({}, AwsDriver._parse_tags(None))

    def test_parse_tags_malformed(self):
        self.assertRaises(error.ConfigInvalid,
                          AwsDriver._parse_tags, 'no-equals-sign')

    # -- power state reading ----------------------------------------------

    def _assert_power_state(self, ec2_state, expected):
        self._expect_describe(self._instance(state=ec2_state))

        self.assertEqual(
            expected, self.test_driver.get_power_state(self.instance_id),
            'EC2 state %r should report as %r' % (ec2_state, expected))
        self.stubber.assert_no_pending_responses()

    def test_get_power_state_pending(self):
        self._assert_power_state('pending', 'On')

    def test_get_power_state_running(self):
        self._assert_power_state('running', 'On')

    def test_get_power_state_stopping_is_still_on(self):
        """An instance on its way down must never be reported as Off

        A fence agent treats Off as proof the machine is dead.
        """
        self._assert_power_state('stopping', 'On')

    def test_get_power_state_shutting_down_is_still_on(self):
        self._assert_power_state('shutting-down', 'On')

    def test_get_power_state_stopped(self):
        self._assert_power_state('stopped', 'Off')

    def test_get_power_state_terminated_is_unknown(self):
        self._assert_power_state('terminated', None)

    def test_get_power_state_missing_is_unknown(self):
        self._expect_not_found()

        self.assertIsNone(
            self.test_driver.get_power_state(self.instance_id))

    # -- power state setting ----------------------------------------------

    def test_set_power_state_on(self):
        self._expect_describe(self._instance(state='stopped'))
        self.stubber.add_response(
            'start_instances',
            {'StartingInstances': []},
            {'InstanceIds': [self.instance_id]})

        self.test_driver.set_power_state(self.instance_id, 'On')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_force_on(self):
        self._expect_describe(self._instance(state='stopped'))
        self.stubber.add_response(
            'start_instances',
            {'StartingInstances': []},
            {'InstanceIds': [self.instance_id]})

        self.test_driver.set_power_state(self.instance_id, 'ForceOn')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_on_already_running_is_noop(self):
        self._expect_describe(self._instance(state='running'))

        self.test_driver.set_power_state(self.instance_id, 'On')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_force_off_skips_os_shutdown(self):
        """Force alone still tries a graceful OS shutdown first

        Fencing must not wait for the OS, so SkipOsShutdown goes with it.
        """
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id],
             'Force': True,
             'SkipOsShutdown': True})

        self.test_driver.set_power_state(self.instance_id, 'ForceOff')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_force_off_while_stopping(self):
        """Still issue the stop: it is cheap and the state may have moved on"""
        self._expect_describe(self._instance(state='stopping'))
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id],
             'Force': True,
             'SkipOsShutdown': True})

        self.test_driver.set_power_state(self.instance_id, 'ForceOff')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_force_off_already_stopped_is_noop(self):
        self._expect_describe(self._instance(state='stopped'))

        self.test_driver.set_power_state(self.instance_id, 'ForceOff')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_force_off_without_skip_support(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id], 'Force': True})

        with mock.patch.object(AwsDriver, '_supports_skip_os_shutdown',
                               return_value=False):
            self.test_driver.set_power_state(self.instance_id, 'ForceOff')

        self.stubber.assert_no_pending_responses()
        self.assertTrue(self.logger.warning.called)

    def test_supports_skip_os_shutdown(self):
        self.assertTrue(self.test_driver._supports_skip_os_shutdown())

    def test_set_power_state_graceful_shutdown(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id]})

        self.test_driver.set_power_state(self.instance_id,
                                         'GracefulShutdown')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_graceful_shutdown_already_stopped(self):
        self._expect_describe(self._instance(state='stopped'))

        self.test_driver.set_power_state(self.instance_id,
                                         'GracefulShutdown')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_graceful_restart(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_response(
            'reboot_instances', {}, {'InstanceIds': [self.instance_id]})

        self.test_driver.set_power_state(self.instance_id, 'GracefulRestart')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_graceful_restart_not_running(self):
        self._expect_describe(self._instance(state='stopped'))

        self.assertRaises(error.Conflict,
                          self.test_driver.set_power_state,
                          self.instance_id, 'GracefulRestart')

    def test_set_power_state_nmi(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_response(
            'send_diagnostic_interrupt', {},
            {'InstanceId': self.instance_id})

        self.test_driver.set_power_state(self.instance_id, 'Nmi')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_unknown(self):
        self._expect_describe(self._instance(state='running'))

        self.assertRaises(error.BadRequest,
                          self.test_driver.set_power_state,
                          self.instance_id, 'Reset')

    def test_set_power_state_terminated(self):
        self._expect_describe(self._instance(state='terminated'))

        self.assertRaises(error.Conflict,
                          self.test_driver.set_power_state,
                          self.instance_id, 'ForceOff')

    def test_set_power_state_incorrect_state_is_noop(self):
        """EC2 rejecting a stop on state grounds is not a failure"""
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_client_error(
            'stop_instances', service_error_code='IncorrectInstanceState',
            http_status_code=400)

        self.test_driver.set_power_state(self.instance_id, 'ForceOff')
        self.stubber.assert_no_pending_responses()

    def test_set_power_state_aws_failure_raises(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_client_error(
            'stop_instances', service_error_code='UnauthorizedOperation',
            http_status_code=403)

        exc = self.assertRaises(error.FishyError,
                                self.test_driver.set_power_state,
                                self.instance_id, 'ForceOff')
        self.assertEqual(500, exc.code)

    def test_set_power_state_gone_during_call(self):
        self._expect_describe(self._instance(state='running'))
        self.stubber.add_client_error(
            'stop_instances',
            service_error_code='InvalidInstanceID.NotFound',
            http_status_code=400)

        self.assertRaises(error.NotFound,
                          self.test_driver.set_power_state,
                          self.instance_id, 'ForceOff')

    # -- force restart ----------------------------------------------------

    def test_force_restart_submits_in_background(self):
        self._expect_describe(self._instance(state='running'))

        with mock.patch.object(AwsDriver, '_force_restart') as restart_mock:
            self.test_driver.set_power_state(self.instance_id, 'ForceRestart')
            awsdriver.FUTURES[self.instance_id].result()

        restart_mock.assert_called_once_with(self.instance_id)
        self.stubber.assert_no_pending_responses()

    def test_force_restart_rejects_concurrent_restart(self):
        self._expect_describe(self._instance(state='running'))

        running_future = mock.Mock()
        running_future.running.return_value = True
        awsdriver.FUTURES[self.instance_id] = running_future

        self.assertRaises(error.Conflict,
                          self.test_driver.set_power_state,
                          self.instance_id, 'ForceRestart')

    def test_force_restart_stops_then_starts(self):
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id],
             'Force': True,
             'SkipOsShutdown': True})
        # still on its way down, so no start yet
        self._expect_describe(self._instance(state='stopping'))
        self._expect_describe(self._instance(state='stopped'))
        self.stubber.add_response(
            'start_instances',
            {'StartingInstances': []},
            {'InstanceIds': [self.instance_id]})

        with mock.patch.object(AwsDriver, 'RESTART_POLL_INTERVAL', 0):
            self.test_driver._force_restart(self.instance_id)

        self.stubber.assert_no_pending_responses()

    def test_force_restart_gives_up_if_terminated(self):
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id],
             'Force': True,
             'SkipOsShutdown': True})
        self._expect_describe(self._instance(state='terminated'))

        with mock.patch.object(AwsDriver, 'RESTART_POLL_INTERVAL', 0):
            self.test_driver._force_restart(self.instance_id)

        self.stubber.assert_no_pending_responses()
        self.assertTrue(self.logger.error.called)

    def test_force_restart_times_out_without_starting(self):
        self.stubber.add_response(
            'stop_instances',
            {'StoppingInstances': []},
            {'InstanceIds': [self.instance_id],
             'Force': True,
             'SkipOsShutdown': True})
        self._expect_describe(self._instance(state='stopping'))

        with mock.patch.object(AwsDriver, 'RESTART_POLL_INTERVAL', 0), \
                mock.patch.object(AwsDriver, 'RESTART_TIMEOUT', 0.0001):
            self.test_driver._force_restart(self.instance_id)

        self.assertTrue(self.logger.error.called)

    # -- boot device and inventory ----------------------------------------

    def test_get_boot_device(self):
        self._expect_describe(self._instance())

        self.assertEqual('Hdd',
                         self.test_driver.get_boot_device(self.instance_id))

    def test_set_boot_device_hdd(self):
        self._expect_describe(self._instance())

        self.test_driver.set_boot_device(self.instance_id, 'Hdd')
        self.stubber.assert_no_pending_responses()

    def test_set_boot_device_pxe_rejected(self):
        self._expect_describe(self._instance())

        self.assertRaises(error.NotSupportedError,
                          self.test_driver.set_boot_device,
                          self.instance_id, 'Pxe')

    def test_get_total_cpus(self):
        self._expect_describe(self._instance(
            CpuOptions={'CoreCount': 2, 'ThreadsPerCore': 2}))

        self.assertEqual(4,
                         self.test_driver.get_total_cpus(self.instance_id))

    def test_get_total_cpus_unknown(self):
        self._expect_describe(self._instance())

        self.assertIsNone(self.test_driver.get_total_cpus(self.instance_id))

    def test_get_nics(self):
        self._expect_describe(self._instance(NetworkInterfaces=[
            {'MacAddress': '02:00:00:00:00:02'},
            {'MacAddress': '02:00:00:00:00:01'}]))

        self.assertEqual(
            [{'id': '02:00:00:00:00:01', 'mac': '02:00:00:00:00:01'},
             {'id': '02:00:00:00:00:02', 'mac': '02:00:00:00:00:02'}],
            self.test_driver.get_nics(self.instance_id))

    def test_get_nics_none(self):
        self._expect_describe(self._instance())

        self.assertEqual([], self.test_driver.get_nics(self.instance_id))
