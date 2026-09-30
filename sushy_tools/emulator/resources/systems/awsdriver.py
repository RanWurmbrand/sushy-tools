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

from concurrent import futures
import time
import uuid as uuid_lib

from sushy_tools.emulator.resources.systems.base import AbstractSystemsDriver
from sushy_tools import error

try:
    import boto3
    import botocore.exceptions as boto_exc

except ImportError:
    boto3 = None
    boto_exc = None


is_loaded = bool(boto3)

# Background ForceRestart tasks, keyed by instance ID. Kept at module scope so
# that it survives the short-lived driver instances created per request.
FUTURES = {}

# Namespace for deriving a stable system UUID from an EC2 instance ID. EC2
# does not expose the instance's SMBIOS UUID through the API, but a Redfish
# system UUID has to be a well-formed UUID: the rest of the emulator parses it
# as one, and the Redfish schema types it as one.
UUID_NAMESPACE = uuid_lib.UUID('1f34febd-107c-414a-bc5a-13bd1c0fd9f8')


class AwsDriver(AbstractSystemsDriver):
    """AWS EC2 driver

    Presents EC2 instances as Redfish computer systems so that Redfish fence
    agents can power-manage them. Only power control is implemented: that is
    all a fence agent ever asks for.
    """

    # EC2 instance states that mean "this machine is still powered".
    #
    # NOTE: `stopping` and `shutting-down` are deliberately reported as `On`.
    # A fence agent treats `Off` as proof the machine is dead and lets its
    # workloads move elsewhere. An instance that is merely on its way down is
    # still running and still writing to storage, so reporting `Off` early
    # would be unsafe. `stopped` is the only state that is safely off.
    EC2_STATES_ON = frozenset(
        ['pending', 'running', 'stopping', 'shutting-down'])

    EC2_STATE_OFF = 'stopped'
    EC2_STATE_GONE = 'terminated'

    # Instance states to list under `systems` - everything but `terminated`.
    EC2_STATES_ALIVE = sorted(EC2_STATES_ON | {EC2_STATE_OFF})

    # Errors that mean "no such instance" rather than "something broke".
    NOT_FOUND_ERRORS = frozenset(
        ['InvalidInstanceID.NotFound', 'InvalidInstanceID.Malformed'])

    # How often, and for how long, a background ForceRestart waits for the
    # instance to reach `stopped` before giving up and leaving it powered off.
    RESTART_POLL_INTERVAL = 2
    RESTART_TIMEOUT = 300

    @classmethod
    def initialize(cls, config, logger, aws_region=None, *args, **kwargs):
        cls._config = config
        cls._logger = logger
        cls._region = aws_region or config.get('SUSHY_EMULATOR_AWS_REGION')
        cls._tags = cls._parse_tags(config.get('SUSHY_EMULATOR_AWS_TAG'))

        if not hasattr(cls, '_ec2'):
            # Credentials are resolved by boto3 itself, which prefers the
            # instance's IAM role. Never accept keys through our own config.
            cls._ec2 = boto3.client('ec2', region_name=cls._region)

        if not hasattr(cls, '_executor'):
            cls._executor = futures.ThreadPoolExecutor(max_workers=4)

        return cls

    @staticmethod
    def _parse_tags(tags):
        """Normalize the configured tag filter into a `dict`

        Accepts either a ``"Key=Value"`` string or a mapping of tag keys to
        tag values. An instance must carry *all* the given tags to be listed.

        :param tags: tag filter as configured, or `None`
        :returns: mapping of tag key to tag value, possibly empty
        """
        if not tags:
            return {}

        if isinstance(tags, dict):
            return {str(k): str(v) for k, v in tags.items()}

        parsed = {}

        for item in str(tags).split(','):
            key, sep, value = item.partition('=')
            if not sep:
                raise error.ConfigInvalid(
                    'SUSHY_EMULATOR_AWS_TAG entry "%(item)s" is not in '
                    'Key=Value form' % {'item': item})

            parsed[key.strip()] = value.strip()

        return parsed

    def _tag_filters(self):
        return [{'Name': 'tag:%s' % key, 'Values': [value]}
                for key, value in self._tags.items()]

    def _describe(self, **kwargs):
        """Call DescribeInstances, mapping AWS failures to Fishy errors

        :returns: list of instance `dict`s
        :raises: `error.NotFound` if an instance ID is unknown to EC2
        :raises: `error.FishyError` on any other AWS failure
        """
        instances = []

        try:
            paginator = self._ec2.get_paginator('describe_instances')

            for page in paginator.paginate(**kwargs):
                for reservation in page.get('Reservations', []):
                    instances.extend(reservation.get('Instances', []))

        except boto_exc.ClientError as ex:
            code = ex.response.get('Error', {}).get('Code')

            if code in self.NOT_FOUND_ERRORS:
                raise error.NotFound(
                    'Instance not found in EC2 region %(region)s: '
                    '%(err)s' % {'region': self._region, 'err': ex})

            raise self._aws_error('describe instances', ex)

        except boto_exc.BotoCoreError as ex:
            raise self._aws_error('describe instances', ex)

        return instances

    def _aws_error(self, action, ex):
        """Log an AWS failure and turn it into a `FishyError` to raise"""
        msg = ('Failed to %(action)s in EC2 region %(region)s: '
               '%(err)s' % {'action': action, 'region': self._region,
                            'err': ex})

        self._logger.error(msg)

        return error.FishyError(msg)

    def _get_instance(self, identity):
        """Get an EC2 instance by ID, always freshly from the EC2 API

        The result is deliberately never cached: a fence agent polls this
        driver to decide whether a machine has really stopped, so a stale
        answer is worse than a slow one.

        :param identity: EC2 instance ID
        :returns: instance description as `dict`
        :raises: `error.NotFound` if no such instance exists
        """
        instances = self._describe(InstanceIds=[identity])

        if not instances:
            msg = ('Error finding instance by ID "%(identity)s" in EC2 '
                   'region %(region)s' % {'identity': identity,
                                          'region': self._region})

            self._logger.debug(msg)

            raise error.NotFound(msg)

        return instances[0]

    @staticmethod
    def _state_of(instance):
        return instance.get('State', {}).get('Name')

    def _power_call(self, action, identity, fn, **kwargs):
        """Issue a power API call, tolerating EC2 state races

        EC2 rejects a stop or start that no longer makes sense for the
        instance's current state. Between reading the state and acting on it
        the instance may have moved on, so such a rejection means "already
        being handled", not "failed". The fence agent's polling decides
        whether the machine actually reached the requested state.

        :returns: `True` if EC2 accepted the call
        """
        try:
            fn(**kwargs)

        except boto_exc.ClientError as ex:
            code = ex.response.get('Error', {}).get('Code')

            if code == 'IncorrectInstanceState':
                self._logger.info(
                    'EC2 refused to %(action)s %(identity)s in its current '
                    'state, treating as a no-op: %(err)s' %
                    {'action': action, 'identity': identity, 'err': ex})
                return False

            if code in self.NOT_FOUND_ERRORS:
                raise error.NotFound(
                    'Instance %(identity)s is gone: %(err)s' %
                    {'identity': identity, 'err': ex})

            raise self._aws_error('%s %s' % (action, identity), ex)

        except boto_exc.BotoCoreError as ex:
            raise self._aws_error('%s %s' % (action, identity), ex)

        return True

    def _supports_skip_os_shutdown(self):
        """Whether the installed botocore knows the SkipOsShutdown parameter"""
        try:
            operation = self._ec2.meta.service_model.operation_model(
                'StopInstances')
            return 'SkipOsShutdown' in operation.input_shape.members

        except Exception:
            return False

    def _stop_hard(self, identity):
        """Stop an instance the way fencing needs: without asking the OS

        `Force=True` alone is not enough. Per the StopInstances API, it still
        attempts a graceful OS shutdown first and only forces the stop if that
        times out. `SkipOsShutdown=True` bypasses the OS outright, which is
        what fencing means. This is what `fence_aws` does by default.
        """
        kwargs = {'InstanceIds': [identity], 'Force': True}

        if self._supports_skip_os_shutdown():
            kwargs['SkipOsShutdown'] = True

        else:
            self._logger.warning(
                'The installed botocore does not support SkipOsShutdown; '
                'stopping %(identity)s with Force only. EC2 will attempt a '
                'graceful OS shutdown first, so powering off may take '
                'noticeably longer.' % {'identity': identity})

        return self._power_call('force stop', identity,
                                self._ec2.stop_instances, **kwargs)

    def _force_restart(self, identity):
        """Force off, wait for `stopped`, then start again

        Runs in the background: the Redfish request must return at once
        rather than block for the length of an EC2 stop.
        """
        self._stop_hard(identity)

        deadline = time.time() + self.RESTART_TIMEOUT

        while time.time() < deadline:
            time.sleep(self.RESTART_POLL_INTERVAL)

            try:
                state = self._state_of(self._get_instance(identity))

            except error.FishyError as ex:
                self._logger.error(
                    'Giving up on restarting %(identity)s: %(err)s' %
                    {'identity': identity, 'err': ex})
                return

            if state == self.EC2_STATE_OFF:
                self._logger.debug(
                    'Instance %(identity)s stopped, starting it back up' %
                    {'identity': identity})
                self._power_call('start', identity,
                                 self._ec2.start_instances,
                                 InstanceIds=[identity])
                return

            if state == self.EC2_STATE_GONE:
                self._logger.error(
                    'Instance %(identity)s was terminated while restarting '
                    'it; not starting it back up' % {'identity': identity})
                return

        self._logger.error(
            'Instance %(identity)s did not reach the "stopped" state within '
            '%(timeout)s seconds; leaving it powered off' %
            {'identity': identity, 'timeout': self.RESTART_TIMEOUT})

    def _submit_restart(self, identity):
        future = FUTURES.get(identity)

        if future is not None:
            if future.running():
                raise error.Conflict(
                    'A restart is already in progress for '
                    '%(identity)s' % {'identity': identity})

            del FUTURES[identity]

        FUTURES[identity] = self._executor.submit(
            self._force_restart, identity)

    @property
    def driver(self):
        """Return human-friendly driver description

        :returns: driver description as `str`
        """
        return '<AWS EC2>'

    @property
    def systems(self):
        """Return available computer systems

        Terminated instances are left out: they are gone, not merely off.

        :returns: list of EC2 instance IDs
        """
        filters = self._tag_filters()
        filters.append({'Name': 'instance-state-name',
                        'Values': list(self.EC2_STATES_ALIVE)})

        return [instance['InstanceId']
                for instance in self._describe(Filters=filters)]

    def uuid(self, identity):
        """Get computer system UUID by ID

        An EC2 instance ID is not a UUID, so derive one from it. The mapping
        is deterministic, so the UUID is stable across emulator restarts. The
        Redfish `Id` stays the instance ID, which is what callers address the
        system by.

        :param identity: EC2 instance ID

        :returns: computer system UUID
        """
        instance_id = self._get_instance(identity)['InstanceId']

        return str(uuid_lib.uuid5(UUID_NAMESPACE, instance_id))

    def name(self, identity):
        """Get computer system name by ID

        :param identity: EC2 instance ID

        :returns: the instance's `Name` tag, or its ID if it has none
        """
        instance = self._get_instance(identity)

        for tag in instance.get('Tags', []):
            if tag.get('Key') == 'Name':
                return tag.get('Value')

        return instance['InstanceId']

    def get_power_state(self, identity):
        """Get computer system power state

        :param identity: EC2 instance ID

        :returns: *On* or *Off* as `str`, or `None` if the power state can't
            be determined - which includes a terminated or missing instance,
            since neither is meaningfully "off"
        """
        try:
            instance = self._get_instance(identity)

        except error.FishyError:
            return

        state = self._state_of(instance)

        if state in self.EC2_STATES_ON:
            return 'On'

        if state == self.EC2_STATE_OFF:
            return 'Off'

        return

    def set_power_state(self, identity, state):
        """Set computer system power state

        Returns as soon as EC2 has accepted the request. The caller polls
        `get_power_state` to learn when the instance got there.

        :param identity: EC2 instance ID
        :param state: string literal requesting power state transition.
            Valid values  are: *On*, *ForceOn*, *ForceOff*, *GracefulShutdown*,
            *GracefulRestart*, *ForceRestart*, *Nmi*.

        :raises: `error.FishyError` if power state can't be set
        """
        instance = self._get_instance(identity)
        current = self._state_of(instance)

        self._logger.debug(
            'set_power_state called: identity=%(identity)s, '
            'requested_state=%(state)s, ec2_state=%(current)s' %
            {'identity': identity, 'state': state, 'current': current})

        if current == self.EC2_STATE_GONE:
            raise error.Conflict(
                'Instance %(identity)s is terminated and can no longer be '
                'power-managed' % {'identity': identity})

        if state in ('On', 'ForceOn'):
            if current in ('pending', 'running'):
                self._logger.debug(
                    'Instance %(identity)s is already %(current)s, not '
                    'starting it' % {'identity': identity,
                                     'current': current})
                return

            self._power_call('start', identity, self._ec2.start_instances,
                             InstanceIds=[identity])

        elif state == 'ForceOff':
            if current == self.EC2_STATE_OFF:
                self._logger.debug(
                    'Instance %(identity)s is already stopped, not stopping '
                    'it' % {'identity': identity})
                return

            self._stop_hard(identity)

        elif state == 'GracefulShutdown':
            if current == self.EC2_STATE_OFF:
                return

            self._power_call('stop', identity, self._ec2.stop_instances,
                             InstanceIds=[identity])

        elif state == 'GracefulRestart':
            if current != 'running':
                raise error.Conflict(
                    'Instance %(identity)s is %(current)s, it can only be '
                    'rebooted while running' % {'identity': identity,
                                                'current': current})

            self._power_call('reboot', identity, self._ec2.reboot_instances,
                             InstanceIds=[identity])

        elif state == 'ForceRestart':
            self._submit_restart(identity)

        elif state == 'Nmi':
            self._power_call('send diagnostic interrupt to', identity,
                             self._ec2.send_diagnostic_interrupt,
                             InstanceId=identity)

        else:
            raise error.BadRequest(
                'Unknown ResetType "%(state)s"' % {'state': state})

    def get_boot_device(self, identity):
        """Get computer system boot device name

        EC2 instances always boot from their root volume.

        :param identity: EC2 instance ID

        :returns: *Hdd*
        """
        self._get_instance(identity)

        return 'Hdd'

    def set_boot_device(self, identity, boot_source):
        """Set computer system boot device name

        :param identity: EC2 instance ID
        :param boot_source: string literal requesting boot device
            change on the system. Valid values are: *Pxe*, *Hdd*, *Cd*.

        :raises: `error.FishyError` if boot device can't be set
        """
        self._get_instance(identity)

        if boot_source == 'Hdd':
            return

        raise error.NotSupportedError(
            'The cloud driver %(driver)s can only boot from Hdd, '
            'not %(boot_source)s' % {'driver': self.driver,
                                     'boot_source': boot_source})

    def get_total_cpus(self, identity):
        """Get computer system total count of available CPUs

        :param identity: EC2 instance ID

        :returns: available vCPU count as `int`, or `None` if EC2 does not
            report the instance's CPU layout
        """
        try:
            instance = self._get_instance(identity)

        except error.FishyError:
            return

        cpu_options = instance.get('CpuOptions') or {}
        cores = cpu_options.get('CoreCount')
        threads = cpu_options.get('ThreadsPerCore')

        if cores is None or threads is None:
            return

        return int(cores) * int(threads)

    def get_nics(self, identity):
        """Get the instance's network interfaces

        Use the MAC address as the network interface's id.

        :param identity: EC2 instance ID

        :returns: list of dictionaries with NIC attributes (id and mac)
        """
        instance = self._get_instance(identity)

        macs = {nic['MacAddress']
                for nic in instance.get('NetworkInterfaces', [])
                if nic.get('MacAddress')}

        return [{'id': mac, 'mac': mac} for mac in sorted(macs)]
