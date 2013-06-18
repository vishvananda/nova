# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Piston Cloud Computing, Inc.
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
"""Tests for compute service."""

import base64
import copy
import datetime
import sys
import testtools
import time
import traceback
import uuid

import mox
from oslo.config import cfg

import nova
from nova import block_device
from nova import compute
from nova.compute import api as compute_api
from nova.compute import flavors
from nova.compute import instance_actions
from nova.compute import manager as compute_manager
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova.conductor import manager as conductor_manager
from nova import context
from nova import db
from nova import exception
from nova.image import glance
from nova.network import api as network_api
from nova.network import model as network_model
from nova.network.security_group import openstack_driver
from nova.objects import instance as instance_obj
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common.notifier import api as notifier_api
from nova.openstack.common.notifier import test_notifier
from nova.openstack.common import rpc
from nova.openstack.common.rpc import common as rpc_common
from nova.openstack.common import timeutils
from nova.openstack.common import uuidutils
from nova import policy
from nova import quota
from nova import test
from nova.tests.api.openstack import fakes
from nova.tests.compute import fake_resource_tracker
from nova.tests.db import fakes as db_fakes
from nova.tests import fake_instance
from nova.tests import fake_instance_actions
from nova.tests import fake_network
from nova.tests import fake_network_cache_model
from nova.tests.image import fake as fake_image
from nova.tests import matchers
from nova import utils
from nova.virt import fake
from nova.volume import cinder

QUOTAS = quota.QUOTAS
LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.import_opt('compute_manager', 'nova.service')
CONF.import_opt('host', 'nova.netconf')
CONF.import_opt('live_migration_retry_count', 'nova.compute.manager')


FAKE_IMAGE_REF = 'fake-image-ref'

NODENAME = 'fakenode1'


def nop_report_driver_status(self):
    pass


def get_primitive_instance_by_uuid(context, instance_uuid):
    """
    Helper method to get an instance and then convert it to
    a primitive form using jsonutils.
    """
    instance = db.instance_get_by_uuid(context, instance_uuid)
    return jsonutils.to_primitive(instance)


class FakeSchedulerAPI(object):

    def run_instance(self, ctxt, request_spec, admin_password,
            injected_files, requested_networks, is_first_time,
            filter_properties):
        pass

    def live_migration(self, ctxt, block_migration, disk_over_commit,
            instance, dest):
        pass

    def prep_resize(self, ctxt, instance, instance_type, image, request_spec,
            filter_properties, reservations):
        pass


class BaseTestCase(test.TestCase):

    def setUp(self):
        super(BaseTestCase, self).setUp()
        notifier_api._reset_drivers()
        self.addCleanup(notifier_api._reset_drivers)
        self.flags(compute_driver='nova.virt.fake.FakeDriver',
                   notification_driver=[test_notifier.__name__],
                   network_manager='nova.network.manager.FlatManager')
        fake.set_nodes([NODENAME])
        self.flags(use_local=True, group='conductor')
        self.compute = importutils.import_object(CONF.compute_manager)

        # override tracker with a version that doesn't need the database:
        fake_rt = fake_resource_tracker.FakeResourceTracker(self.compute.host,
                    self.compute.driver, NODENAME)
        self.compute._resource_tracker_dict[NODENAME] = fake_rt

        def fake_get_compute_nodes_in_db(context):
            fake_compute_nodes = [{'local_gb': 259,
                                   'vcpus_used': 0,
                                   'deleted': 0,
                                   'hypervisor_type': 'powervm',
                                   'created_at': '2013-04-01T00:27:06.000000',
                                   'local_gb_used': 0,
                                   'updated_at': '2013-04-03T00:35:41.000000',
                                   'hypervisor_hostname': 'fake_phyp1',
                                   'memory_mb_used': 512,
                                   'memory_mb': 131072,
                                   'current_workload': 0,
                                   'vcpus': 16,
                                   'cpu_info': 'ppc64,powervm,3940',
                                   'running_vms': 0,
                                   'free_disk_gb': 259,
                                   'service_id': 7,
                                   'hypervisor_version': 7,
                                   'disk_available_least': 265856,
                                   'deleted_at': None,
                                   'free_ram_mb': 130560,
                                   'id': 2}]
            return fake_compute_nodes

        def fake_compute_node_delete(context, compute_node):
            self.assertEqual(compute_node.get('hypervisor_hostname'),
                             'fake_phyp1')

        self.stubs.Set(self.compute, '_get_compute_nodes_in_db',
                fake_get_compute_nodes_in_db)
        self.stubs.Set(self.compute.conductor_api, 'compute_node_delete',
                fake_compute_node_delete)

        self.compute.update_available_resource(
                context.get_admin_context())

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id,
                                              self.project_id)
        test_notifier.NOTIFICATIONS = []

        def fake_show(meh, context, id):
            if id:
                return {'id': id, 'min_disk': None, 'min_ram': None,
                        'name': 'fake_name',
                        'status': 'active',
                        'properties': {'kernel_id': 'fake_kernel_id',
                                       'ramdisk_id': 'fake_ramdisk_id',
                                       'something_else': 'meow'}}
            else:
                raise exception.ImageNotFound(image_id=id)

        fake_image.stub_out_image_service(self.stubs)
        self.stubs.Set(fake_image._FakeImageService, 'show', fake_show)

        fake_rpcapi = FakeSchedulerAPI()
        self.stubs.Set(self.compute, 'scheduler_rpcapi', fake_rpcapi)
        fake_network.set_stub_network_methods(self.stubs)
        fake_instance_actions.stub_out_action_events(self.stubs)

        def fake_get_nw_info(cls, ctxt, instance, *args, **kwargs):
            self.assertTrue(ctxt.is_admin)
            return fake_network.fake_get_instance_nw_info(self.stubs, 1, 1,
                                                          spectacular=True)

        self.stubs.Set(network_api.API, 'get_instance_nw_info',
                       fake_get_nw_info)
        self.stubs.Set(network_api.API, 'allocate_for_instance',
                       fake_get_nw_info)
        self.compute_api = compute.API()

        # Just to make long lines short
        self.rt = self.compute._get_resource_tracker(NODENAME)

    def tearDown(self):
        timeutils.clear_time_override()
        ctxt = context.get_admin_context()
        fake_image.FakeImageService_reset()
        instances = db.instance_get_all(ctxt)
        for instance in instances:
            db.instance_destroy(ctxt, instance['uuid'])
        fake.restore_nodes()
        super(BaseTestCase, self).tearDown()

    def stub_out_client_exceptions(self):
        def passthru(exceptions, func, *args, **kwargs):
            return func(*args, **kwargs)

        self.stubs.Set(rpc_common, 'catch_client_exception', passthru)

    def _create_fake_instance(self, params=None, type_name='m1.tiny'):
        """Create a test instance."""
        if not params:
            params = {}

        def make_fake_sys_meta():
            sys_meta = {}
            inst_type = flavors.get_flavor_by_name(type_name)
            for key in flavors.system_metadata_flavor_props:
                sys_meta['instance_type_%s' % key] = inst_type[key]
            return sys_meta

        inst = {}
        inst['vm_state'] = vm_states.ACTIVE
        inst['task_state'] = None
        inst['image_ref'] = FAKE_IMAGE_REF
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = self.user_id
        inst['project_id'] = self.project_id
        inst['host'] = 'fake_host'
        inst['node'] = NODENAME
        type_id = flavors.get_flavor_by_name(type_name)['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['memory_mb'] = 0
        inst['vcpus'] = 0
        inst['root_gb'] = 0
        inst['ephemeral_gb'] = 0
        inst['architecture'] = 'x86_64'
        inst['os_type'] = 'Linux'
        inst['system_metadata'] = make_fake_sys_meta()
        inst['locked'] = False
        inst.update(params)
        _create_service_entries(self.context.elevated(),
                {'fake_zone': [inst['host']]})
        return db.instance_create(self.context, inst)

    def _create_instance_obj(self, params=None, type_name='m1.tiny'):
        """Create a test instance object."""
        instance = instance_obj.Instance()
        instance.uuid = uuidutils.generate_uuid()
        instance.cell_name = 'api!child'

        def _fake_db_create(_ctxt, inst):
            for k, v in inst.items():
                setattr(instance, k, v)
            return instance

        self.stubs.Set(db, 'instance_create', _fake_db_create)
        return self._create_fake_instance(params=params,
                type_name=type_name)

    def _create_instance(self, params=None, type_name='m1.tiny'):
        """Create a test instance. Returns uuid."""
        return self._create_fake_instance(params, type_name=type_name)

    def _create_instance_type(self, params=None):
        """Create a test instance type."""
        if not params:
            params = {}

        context = self.context.elevated()
        inst = {}
        inst['name'] = 'm1.small'
        inst['memory_mb'] = 1024
        inst['vcpus'] = 1
        inst['root_gb'] = 20
        inst['ephemeral_gb'] = 10
        inst['flavorid'] = '1'
        inst['swap'] = 2048
        inst['rxtx_factor'] = 1
        inst.update(params)
        return db.instance_type_create(context, inst)['id']

    def _create_group(self):
        values = {'name': 'testgroup',
                  'description': 'testgroup',
                  'user_id': self.user_id,
                  'project_id': self.project_id}
        return db.security_group_create(self.context, values)


class ComputeVolumeTestCase(BaseTestCase):

    def setUp(self):
        super(ComputeVolumeTestCase, self).setUp()
        self.volume_id = 'fake'
        self.fetched_attempts = 0
        self.instance = {
            'id': 'fake',
            'uuid': 'fake',
            'name': 'fake',
            'root_device_name': '/dev/vda',
        }
        self.stubs.Set(self.compute.volume_api, 'get', lambda *a, **kw:
                       {'id': self.volume_id})
        self.stubs.Set(self.compute.driver, 'get_volume_connector',
                       lambda *a, **kw: None)
        self.stubs.Set(self.compute.volume_api, 'initialize_connection',
                       lambda *a, **kw: {})
        self.stubs.Set(self.compute.volume_api, 'terminate_connection',
                       lambda *a, **kw: None)
        self.stubs.Set(self.compute.volume_api, 'attach',
                       lambda *a, **kw: None)
        self.stubs.Set(self.compute.volume_api, 'detach',
                       lambda *a, **kw: None)
        self.stubs.Set(self.compute.volume_api, 'check_attach',
                       lambda *a, **kw: None)

        def store_cinfo(context, *args):
            self.cinfo = jsonutils.loads(args[-1].get('connection_info'))

        self.stubs.Set(self.compute.conductor_api,
                       'block_device_mapping_update',
                       store_cinfo)
        self.stubs.Set(self.compute.conductor_api,
                       'block_device_mapping_update_or_create',
                       store_cinfo)

    def test_attach_volume_serial(self):

        self.compute.attach_volume(self.context, self.volume_id,
                                   '/dev/vdb', self.instance)
        self.assertEqual(self.cinfo.get('serial'), self.volume_id)

    def test_await_block_device_created_to_slow(self):

        def never_get(context, vol_id):
            return {
                'status': 'creating',
                'id': 'blah',
            }

        self.stubs.Set(self.compute.volume_api, 'get', never_get)
        self.assertRaises(exception.VolumeNotCreated,
                          self.compute._await_block_device_map_created,
                          self.context, '1', max_tries=2, wait_between=0.1)

    def test_await_block_device_created_slow(self):
        c = self.compute

        def slow_get(context, vol_id):
            while self.fetched_attempts < 2:
                self.fetched_attempts += 1
                return {
                    'status': 'creating',
                    'id': 'blah',
                }
            return {
                'status': 'available',
                'id': 'blah',
            }

        self.stubs.Set(c.volume_api, 'get', slow_get)
        attempts = c._await_block_device_map_created(self.context, '1',
                                                     max_tries=4,
                                                     wait_between=0.1)
        self.assertEqual(attempts, 3)

    def test_boot_volume_serial(self):
        block_device_mapping = [{
            'id': 1,
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': self.volume_id,
            'device_name': '/dev/vdb',
            'delete_on_termination': False,
        }]
        self.compute._setup_block_device_mapping(self.context, self.instance,
                                                 block_device_mapping)
        self.assertEqual(self.cinfo.get('serial'), self.volume_id)

    def test_boot_volume_metadata(self):
        self.mox.StubOutWithMock(self.compute_api.volume_api, 'get')
        block_device_mapping = [{
            'id': 1,
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': self.volume_id,
            'status': 'active',
            'device_name': 'vda',
            'delete_on_termination': False,
            'volume_image_metadata':
                {'test_key': 'test_value'}
        }]
        sentinel = object()
        self.compute_api.volume_api.get(self.context,
                self.volume_id).AndReturn(sentinel)
        self.mox.ReplayAll()
        vol = self.compute_api._get_volume(self.context, block_device_mapping)
        self.assertIs(vol, sentinel)

    def test_poll_volume_usage_disabled(self):
        ctxt = 'MockContext'
        self.mox.StubOutWithMock(self.compute, '_get_host_volume_bdms')
        self.mox.StubOutWithMock(utils, 'last_completed_audit_period')
        # None of the mocks should be called.
        self.mox.ReplayAll()

        CONF.volume_usage_poll_interval = 0
        self.compute._poll_volume_usage(ctxt)
        self.mox.UnsetStubs()

    def test_poll_volume_usage_interval_not_elapsed(self):
        ctxt = 'MockContext'
        self.mox.StubOutWithMock(self.compute, '_get_host_volume_bdms')
        self.mox.StubOutWithMock(utils, 'last_completed_audit_period')
        self.mox.StubOutWithMock(self.compute.driver, 'get_all_volume_usage')
        self.mox.StubOutWithMock(time, 'time')
        # Following methods will be called.
        utils.last_completed_audit_period().AndReturn((0, 0))
        time.time().AndReturn(10)
        self.mox.ReplayAll()

        CONF.volume_usage_poll_interval = 2
        self.compute._last_vol_usage_poll = 9
        self.compute._poll_volume_usage(ctxt)
        self.mox.UnsetStubs()

    def test_poll_volume_usage_returns_no_vols(self):
        ctxt = 'MockContext'
        self.compute.host = 'MockHost'
        self.mox.StubOutWithMock(self.compute, '_get_host_volume_bdms')
        self.mox.StubOutWithMock(utils, 'last_completed_audit_period')
        self.mox.StubOutWithMock(self.compute.driver, 'get_all_volume_usage')
        # Following methods are called.
        utils.last_completed_audit_period().AndReturn((0, 0))
        self.compute._get_host_volume_bdms(ctxt, 'MockHost').AndReturn([])
        self.mox.ReplayAll()

        CONF.volume_usage_poll_interval = 10
        self.compute._last_vol_usage_poll = 0
        self.compute._poll_volume_usage(ctxt)
        self.mox.UnsetStubs()

    def test_poll_volume_usage_with_data(self):
        ctxt = 'MockContext'
        self.compute.host = 'MockHost'
        curr_time = time.time()
        self.mox.StubOutWithMock(utils, 'last_completed_audit_period')
        self.mox.StubOutWithMock(self.compute, '_get_host_volume_bdms')
        self.mox.StubOutWithMock(self.compute, '_update_volume_usage_cache')
        self.stubs.Set(self.compute.driver, 'get_all_volume_usage',
                       lambda x, y: [3, 4])
        # All the mocks are called
        utils.last_completed_audit_period().AndReturn((10, 20))
        self.compute._get_host_volume_bdms(ctxt, 'MockHost').AndReturn([1, 2])
        self.compute._update_volume_usage_cache(ctxt, [3, 4])
        self.mox.ReplayAll()
        CONF.volume_usage_poll_interval = 10
        self.compute._last_vol_usage_poll = 0
        self.compute._poll_volume_usage(ctxt)
        self.assertTrue((curr_time < self.compute._last_vol_usage_poll),
                        "_last_vol_usage_poll was not properly updated <%s>" %
                        self.compute._last_vol_usage_poll)
        self.mox.UnsetStubs()

    def test_detach_volume_usage(self):
        # Test that detach volume update the volume usage cache table correctly
        instance = self._create_fake_instance()
        vol = {'id': 1,
               'attach_status': 'in-use',
               'instance_uuid': instance['uuid']}
        bdm = {'id': 1,
               'device_name': '/dev/vdb',
               'connection_info': '{}',
               'instance_uuid': instance['uuid'],
               'volume_id': 1}

        self.mox.StubOutWithMock(self.compute, '_get_instance_volume_bdm')
        self.mox.StubOutWithMock(self.compute.driver, 'block_stats')
        self.mox.StubOutWithMock(self.compute, '_get_host_volume_bdms')
        self.mox.StubOutWithMock(self.compute.driver, 'get_all_volume_usage')

        # The following methods will be called
        self.compute._get_instance_volume_bdm(self.context, instance, 1).\
            AndReturn(bdm)
        self.compute.driver.block_stats(instance['name'], 'vdb').\
            AndReturn([1L, 30L, 1L, 20L, None])
        self.compute._get_host_volume_bdms(self.context, 'fake-mini').\
            AndReturn(bdm)
        self.compute.driver.get_all_volume_usage(self.context, bdm).\
            AndReturn([{'volume': 1,
                        'rd_req': 1,
                        'rd_bytes': 10,
                        'wr_req': 1,
                        'wr_bytes': 5,
                        'instance': instance}])

        self.mox.ReplayAll()

        self.compute.attach_volume(self.context, 1, '/dev/vdb', instance)

        # Poll volume usage & then detach the volume. This will update the
        # total fields in the volume usage cache.
        CONF.volume_usage_poll_interval = 10
        self.compute._poll_volume_usage(self.context)
        # Check that a volume.usage notification was sent
        self.assertEqual(1, len(test_notifier.NOTIFICATIONS))
        msg = test_notifier.NOTIFICATIONS[0]

        self.compute.detach_volume(self.context, 1, instance)

        # Check that a volume.usage notification was sent
        self.assertEquals(2, len(test_notifier.NOTIFICATIONS))
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals('volume.usage', msg['event_type'])
        payload = msg['payload']
        self.assertEquals(instance['uuid'], payload['instance_id'])
        self.assertEquals('fake', payload['user_id'])
        self.assertEquals('fake', payload['tenant_id'])
        self.assertEquals(1, payload['reads'])
        self.assertEquals(30, payload['read_bytes'])
        self.assertEquals(1, payload['writes'])
        self.assertEquals(20, payload['write_bytes'])
        self.assertEquals(None, payload['availability_zone'])

        # Check the database for the
        volume_usages = db.vol_get_usage_by_time(self.context, 0)
        self.assertEqual(1, len(volume_usages))
        volume_usage = volume_usages[0]
        self.assertEqual(0, volume_usage['curr_reads'])
        self.assertEqual(0, volume_usage['curr_read_bytes'])
        self.assertEqual(0, volume_usage['curr_writes'])
        self.assertEqual(0, volume_usage['curr_write_bytes'])
        self.assertEqual(1, volume_usage['tot_reads'])
        self.assertEqual(30, volume_usage['tot_read_bytes'])
        self.assertEqual(1, volume_usage['tot_writes'])
        self.assertEqual(20, volume_usage['tot_write_bytes'])

    def test_validate_bdm(self):
        # Test if volume is checked for availability before being attached
        # at boot time

        def fake_bdms(context, instance_uuid):
            block_device_mapping = [{
                'id': 1,
                'no_device': None,
                'source_type': 'volume',
                'destination_type': 'volume',
                'snapshot_id': None,
                'volume_id': self.volume_id,
                'device_name': 'vda',
                'delete_on_termination': False,
            }]
            return block_device_mapping
        self.stubs.Set(self.compute.db,
                       'block_device_mapping_get_all_by_instance',
                       fake_bdms)

        # Check that the volume status is 'available' and reject if not
        def fake_volume_get_1(self, context, volume_id):
            return {'id': volume_id,
                    'status': 'creating',
                    'attach_status': 'detached'}
        self.stubs.Set(cinder.API, 'get', fake_volume_get_1)

        self.assertRaises(exception.InvalidBDMVolume,
                          self.compute_api._validate_bdm,
                          self.context,
                          instance=self.instance)

        # Check that the volume attach_status is 'detached' and reject if not
        def fake_volume_get_2(self, context, volume_id):
            return {'id': volume_id,
                    'status': 'available',
                    'attach_status': 'attached'}
        self.stubs.Set(cinder.API, 'get', fake_volume_get_2)

        self.assertRaises(exception.InvalidBDMVolume,
                          self.compute_api._validate_bdm,
                          self.context,
                          instance=self.instance)

        # Check that the volume status is 'available' and attach_status is
        # 'detached' and accept the request if so
        def fake_volume_get_3(self, context, volume_id):
            return {'id': volume_id,
                    'status': 'available',
                    'attach_status': 'detached'}
        self.stubs.Set(cinder.API, 'get', fake_volume_get_3)

        self.compute_api._validate_bdm(self.context, instance=self.instance)


class ComputeTestCase(BaseTestCase):
    def test_wrap_instance_fault(self):
        inst = {"uuid": "fake_uuid"}

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stubs.Set(compute_utils, 'add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance):
            raise NotImplementedError()

        self.assertRaises(NotImplementedError, failer,
                          self.compute, self.context, instance=inst)

        self.assertTrue(called['fault_added'])

    def test_wrap_instance_fault_instance_in_args(self):
        inst = {"uuid": "fake_uuid"}

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stubs.Set(compute_utils, 'add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance):
            raise NotImplementedError()

        self.assertRaises(NotImplementedError, failer,
                          self.compute, self.context, inst)

        self.assertTrue(called['fault_added'])

    def test_wrap_instance_fault_no_instance(self):
        inst_uuid = "fake_uuid"

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stubs.Set(compute_utils, 'add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance_uuid):
            raise exception.InstanceNotFound(instance_id=instance_uuid)

        self.assertRaises(exception.InstanceNotFound, failer,
                          self.compute, self.context, inst_uuid)

        self.assertFalse(called['fault_added'])

    def test_wrap_instance_event(self):
        inst = {"uuid": "fake_uuid"}

        called = {'started': False,
                  'finished': False}

        def did_it_update_start(self2, context, values):
            called['started'] = True

        def did_it_update_finish(self2, context, values):
            called['finished'] = True

        self.stubs.Set(conductor_manager.ConductorManager,
                       'action_event_start', did_it_update_start)

        self.stubs.Set(conductor_manager.ConductorManager,
                       'action_event_finish', did_it_update_finish)

        @compute_manager.wrap_instance_event
        def fake_event(self, context, instance):
            pass

        fake_event(self.compute, self.context, instance=inst)

        self.assertTrue(called['started'])
        self.assertTrue(called['finished'])

    def test_wrap_instance_event_log_exception(self):
        inst = {"uuid": "fake_uuid"}

        called = {'started': False,
                  'finished': False,
                  'message': ''}

        def did_it_update_start(self2, context, values):
            called['started'] = True

        def did_it_update_finish(self2, context, values):
            called['finished'] = True
            called['message'] = values['message']

        self.stubs.Set(conductor_manager.ConductorManager,
                       'action_event_start', did_it_update_start)

        self.stubs.Set(conductor_manager.ConductorManager,
                       'action_event_finish', did_it_update_finish)

        @compute_manager.wrap_instance_event
        def fake_event(self2, context, instance):
            raise exception.NovaException()

        self.assertRaises(exception.NovaException, fake_event,
                          self.compute, self.context, instance=inst)

        self.assertTrue(called['started'])
        self.assertTrue(called['finished'])
        self.assertEqual('An unknown exception occurred.', called['message'])

    def test_object_compat(self):
        db_inst = fake_instance.fake_db_instance()

        @compute_manager.object_compat
        def test_fn(_self, context, instance):
            self.assertTrue(isinstance(instance, instance_obj.Instance))
            self.assertEqual(instance.uuid, db_inst['uuid'])

        test_fn(None, self.context, instance=db_inst)

    def test_create_instance_with_img_ref_associates_config_drive(self):
        # Make sure create associates a config drive.

        instance = jsonutils.to_primitive(self._create_fake_instance(
                        params={'config_drive': '1234', }))

        try:
            self.compute.run_instance(self.context, instance=instance)
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertTrue(instance['config_drive'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_create_instance_associates_config_drive(self):
        # Make sure create associates a config drive.

        instance = jsonutils.to_primitive(self._create_fake_instance(
                        params={'config_drive': '1234', }))

        try:
            self.compute.run_instance(self.context, instance=instance)
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertTrue(instance['config_drive'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_create_instance_unlimited_memory(self):
        # Default of memory limit=None is unlimited.
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        params = {"memory_mb": 999999999999}
        filter_properties = {'limits': {'memory_mb': None}}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)
        self.assertEqual(999999999999, self.rt.compute_node['memory_mb_used'])

    def test_create_instance_unlimited_disk(self):
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        params = {"root_gb": 999999999999,
                  "ephemeral_gb": 99999999999}
        filter_properties = {'limits': {'disk_gb': None}}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)

    def test_create_multiple_instances_then_starve(self):
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        filter_properties = {'limits': {'memory_mb': 4096, 'disk_gb': 1000}}
        params = {"memory_mb": 1024, "root_gb": 128, "ephemeral_gb": 128}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                                  filter_properties=filter_properties)
        self.assertEquals(1024, self.rt.compute_node['memory_mb_used'])
        self.assertEquals(256, self.rt.compute_node['local_gb_used'])

        params = {"memory_mb": 2048, "root_gb": 256, "ephemeral_gb": 256}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                                  filter_properties=filter_properties)
        self.assertEquals(3072, self.rt.compute_node['memory_mb_used'])
        self.assertEquals(768, self.rt.compute_node['local_gb_used'])

        params = {"memory_mb": 8192, "root_gb": 8192, "ephemeral_gb": 8192}
        instance = self._create_fake_instance(params)
        self.assertRaises(exception.ComputeResourcesUnavailable,
                self.compute.run_instance, self.context, instance=instance,
                filter_properties=filter_properties)

    def test_create_instance_with_oversubscribed_ram(self):
        # Test passing of oversubscribed ram policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_mem_mb = resources['memory_mb']

        oversub_limit_mb = total_mem_mb * 1.5
        instance_mb = int(total_mem_mb * 1.45)

        # build an instance, specifying an amount of memory that exceeds
        # total_mem_mb, but is less than the oversubscribed limit:
        params = {"memory_mb": instance_mb, "root_gb": 128,
                  "ephemeral_gb": 128}
        instance = self._create_fake_instance(params)

        limits = {'memory_mb': oversub_limit_mb}
        filter_properties = {'limits': limits}
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)

        self.assertEqual(instance_mb, self.rt.compute_node['memory_mb_used'])

    def test_create_instance_with_oversubscribed_ram_fail(self):
        """Test passing of oversubscribed ram policy from the scheduler, but
        with insufficient memory.
        """
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_mem_mb = resources['memory_mb']

        oversub_limit_mb = total_mem_mb * 1.5
        instance_mb = int(total_mem_mb * 1.55)

        # build an instance, specifying an amount of memory that exceeds
        # total_mem_mb, but is less than the oversubscribed limit:
        params = {"memory_mb": instance_mb, "root_gb": 128,
                  "ephemeral_gb": 128}
        instance = self._create_fake_instance(params)

        filter_properties = {'limits': {'memory_mb': oversub_limit_mb}}

        self.assertRaises(exception.ComputeResourcesUnavailable,
                self.compute.run_instance, self.context, instance=instance,
                filter_properties=filter_properties)

    def test_create_instance_with_oversubscribed_cpu(self):
        # Test passing of oversubscribed cpu policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        limits = {'vcpu': 3}
        filter_properties = {'limits': limits}

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        self.assertEqual(1, resources['vcpus'])

        # build an instance, specifying an amount of memory that exceeds
        # total_mem_mb, but is less than the oversubscribed limit:
        params = {"memory_mb": 10, "root_gb": 1,
                  "ephemeral_gb": 1, "vcpus": 2}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)

        self.assertEqual(2, self.rt.compute_node['vcpus_used'])

        # create one more instance:
        params = {"memory_mb": 10, "root_gb": 1,
                  "ephemeral_gb": 1, "vcpus": 1}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)

        self.assertEqual(3, self.rt.compute_node['vcpus_used'])

        # delete the instance:
        instance['vm_state'] = vm_states.DELETED
        self.rt.update_usage(self.context,
                instance=instance)

        self.assertEqual(2, self.rt.compute_node['vcpus_used'])

        # now oversubscribe vcpus and fail:
        params = {"memory_mb": 10, "root_gb": 1,
                  "ephemeral_gb": 1, "vcpus": 2}
        instance = self._create_fake_instance(params)

        limits = {'vcpu': 3}
        filter_properties = {'limits': limits}
        self.assertRaises(exception.ComputeResourcesUnavailable,
                self.compute.run_instance, self.context, instance=instance,
                filter_properties=filter_properties)

    def test_create_instance_with_oversubscribed_disk(self):
        # Test passing of oversubscribed disk policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_disk_gb = resources['local_gb']

        oversub_limit_gb = total_disk_gb * 1.5
        instance_gb = int(total_disk_gb * 1.45)

        # build an instance, specifying an amount of disk that exceeds
        # total_disk_gb, but is less than the oversubscribed limit:
        params = {"root_gb": instance_gb, "memory_mb": 10}
        instance = self._create_fake_instance(params)

        limits = {'disk_gb': oversub_limit_gb}
        filter_properties = {'limits': limits}
        self.compute.run_instance(self.context, instance=instance,
                filter_properties=filter_properties)

        self.assertEqual(instance_gb, self.rt.compute_node['local_gb_used'])

    def test_create_instance_with_oversubscribed_disk_fail(self):
        """Test passing of oversubscribed disk policy from the scheduler, but
        with insufficient disk.
        """
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_disk_gb = resources['local_gb']

        oversub_limit_gb = total_disk_gb * 1.5
        instance_gb = int(total_disk_gb * 1.55)

        # build an instance, specifying an amount of disk that exceeds
        # total_disk_gb, but is less than the oversubscribed limit:
        params = {"root_gb": instance_gb, "memory_mb": 10}
        instance = self._create_fake_instance(params)

        limits = {'disk_gb': oversub_limit_gb}
        filter_properties = {'limits': limits}
        self.assertRaises(exception.ComputeResourcesUnavailable,
                self.compute.run_instance, self.context, instance=instance,
                filter_properties=filter_properties)

    def test_create_instance_without_node_param(self):
        instance = self._create_fake_instance({'node': None})

        self.compute.run_instance(self.context, instance=instance)
        instances = db.instance_get_all(self.context)
        instance = instances[0]

        self.assertEqual(NODENAME, instance['node'])

    def test_create_instance_no_image(self):
        # Create instance with no image provided.
        params = {'image_ref': ''}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance)
        self._assert_state({'vm_state': vm_states.ACTIVE,
                            'task_state': None})

    def test_default_access_ip(self):
        self.flags(default_access_ip_network_name='test1')
        fake_network.unset_stub_network_methods(self.stubs)
        instance = jsonutils.to_primitive(self._create_fake_instance())

        orig_update = self.compute._instance_update

        # Make sure the access_ip_* updates happen in the same DB
        # update as the set to ACTIVE.
        def _instance_update(ctxt, instance_uuid, **kwargs):
            if kwargs.get('vm_state', None) == vm_states.ACTIVE:
                self.assertEqual(kwargs['access_ip_v4'], '192.168.1.100')
                self.assertEqual(kwargs['access_ip_v6'], '2001:db8:0:1::1')
            return orig_update(ctxt, instance_uuid, **kwargs)

        self.stubs.Set(self.compute, '_instance_update', _instance_update)

        try:
            self.compute.run_instance(self.context, instance=instance,
                    is_first_time=True)
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertEqual(instance['access_ip_v4'], '192.168.1.100')
            self.assertEqual(instance['access_ip_v6'], '2001:db8:0:1::1')
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_no_default_access_ip(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())

        try:
            self.compute.run_instance(self.context, instance=instance,
                    is_first_time=True)
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertFalse(instance['access_ip_v4'])
            self.assertFalse(instance['access_ip_v6'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_fail_to_schedule_persists(self):
        # check the persistence of the ERROR(scheduling) state.
        self._create_instance(params={'vm_state': vm_states.ERROR,
                                      'task_state': task_states.SCHEDULING})
        #check state is failed even after the periodic poll
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': task_states.SCHEDULING})

    def test_run_instance_setup_block_device_mapping_fail(self):
        """block device mapping failure test.

        Make sure that when there is a block device mapping problem,
        the instance goes to ERROR state, keeping the task state
        """
        def fake(*args, **kwargs):
            raise test.TestingException()
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       '_setup_block_device_mapping', fake)
        instance = self._create_instance()
        self.assertRaises(test.TestingException, self.compute.run_instance,
                          self.context, instance=instance)
        #check state is failed even after the periodic poll
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})

    def test_run_instance_spawn_fail(self):
        """spawn failure test.

        Make sure that when there is a spawning problem,
        the instance goes to ERROR state, keeping the task state.
        """
        def fake(*args, **kwargs):
            raise test.TestingException()
        self.stubs.Set(self.compute.driver, 'spawn', fake)
        instance = self._create_instance()
        self.assertRaises(test.TestingException, self.compute.run_instance,
                          self.context, instance=instance)
        #check state is failed even after the periodic poll
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})

    def test_run_instance_dealloc_network_instance_not_found(self):
        """spawn network deallocate test.

        Make sure that when an instance is not found during spawn
        that the network is deallocated
        """
        instance = self._create_instance()

        def fake(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id="fake")

        self.stubs.Set(self.compute.driver, 'spawn', fake)
        self.mox.StubOutWithMock(self.compute, '_deallocate_network')
        self.compute._deallocate_network(mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()

        self.assertRaises(exception.InstanceNotFound,
                          self.compute.run_instance,
                          self.context, instance=instance)

    def test_run_instance_bails_on_missing_instance(self):
        # Make sure that run_instance() will quickly ignore a deleted instance
        called = {}
        instance = self._create_instance()

        def fake_instance_update(self, *a, **args):
            called['instance_update'] = True
            raise exception.InstanceNotFound(instance_id='foo')
        self.stubs.Set(self.compute, '_instance_update', fake_instance_update)

        self.compute.run_instance(self.context, instance)
        self.assertIn('instance_update', called)

    def test_can_terminate_on_error_state(self):
        # Make sure that the instance can be terminated in ERROR state.
        #check failed to schedule --> terminate
        instance = self._create_instance(params={'vm_state': vm_states.ERROR})
        self.compute.terminate_instance(self.context, instance=instance)
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          self.context, instance['uuid'])
        # Double check it's not there for admins, either.
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          self.context.elevated(), instance['uuid'])

    def test_run_terminate(self):
        # Make sure it is possible to  run and terminate instance.
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        self.compute.terminate_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("After terminating instances: %s"), instances)
        self.assertEqual(len(instances), 0)

    def test_run_terminate_with_vol_attached(self):
        """Make sure it is possible to  run and terminate instance with volume
        attached
        """
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        def fake_check_attach(*args, **kwargs):
            pass

        def fake_reserve_volume(*args, **kwargs):
            pass

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}

        self.stubs.Set(cinder.API, 'get', fake_volume_get)
        self.stubs.Set(cinder.API, 'check_attach', fake_check_attach)
        self.stubs.Set(cinder.API, 'reserve_volume',
                       fake_reserve_volume)

        self.compute_api.attach_volume(self.context, instance, 1,
                                       '/dev/vdc')

        self.compute.terminate_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("After terminating instances: %s"), instances)
        self.assertEqual(len(instances), 0)
        bdms = db.block_device_mapping_get_all_by_instance(self.context,
                                                           instance['uuid'])
        self.assertEqual(len(bdms), 0)

    def test_run_terminate_no_image(self):
        """
        Make sure instance started without image (from volume)
        can be termintad without issues
        """
        params = {'image_ref': ''}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance)
        self._assert_state({'vm_state': vm_states.ACTIVE,
                            'task_state': None})

        self.compute.terminate_instance(self.context, instance=instance)
        instances = db.instance_get_all(self.context)
        self.assertEqual(len(instances), 0)

    def test_terminate_no_network(self):
        # This is as reported in LP bug 1008875
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        # Make it look like this is no instance
        self.mox.StubOutWithMock(self.compute, '_get_instance_nw_info')
        self.compute._get_instance_nw_info(
                mox.IgnoreArg(),
                mox.IgnoreArg()).AndRaise(
                    exception.NetworkNotFound(network_id='fake')
                )
        self.mox.ReplayAll()

        self.compute.terminate_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("After terminating instances: %s"), instances)
        self.assertEqual(len(instances), 0)

    def test_terminate_failure_leaves_task_state(self):
        """Ensure that a failure in terminate_instance does not result
        in the task state being reverted from DELETING (see LP 1046236).
        """
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        # Network teardown fails ungracefully
        self.mox.StubOutWithMock(self.compute, '_get_instance_nw_info')
        self.compute._get_instance_nw_info(
                mox.IgnoreArg(),
                mox.IgnoreArg()).AndRaise(TypeError())
        self.mox.ReplayAll()

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.DELETING})
        try:
            self.compute.terminate_instance(self.context, instance=instance)
        except TypeError:
            pass

        instances = db.instance_get_all(self.context)
        LOG.info(_("After terminating instances: %s"), instances)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['task_state'], 'deleting')

    def test_run_terminate_timestamps(self):
        # Make sure timestamps are set for launched and destroyed.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.assertEqual(instance['launched_at'], None)
        self.assertEqual(instance['deleted_at'], None)
        launch = timeutils.utcnow()
        self.compute.run_instance(self.context, instance=instance)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assert_(instance['launched_at'] > launch)
        self.assertEqual(instance['deleted_at'], None)
        terminate = timeutils.utcnow()
        self.compute.terminate_instance(self.context, instance=instance)
        with utils.temporary_mutation(self.context, read_deleted='only'):
            instance = db.instance_get_by_uuid(self.context,
                    instance['uuid'])
        self.assert_(instance['launched_at'] < terminate)
        self.assert_(instance['deleted_at'] > terminate)

    def test_run_terminate_deallocate_net_failure_sets_error_state(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        def _fake_deallocate_network(*args, **kwargs):
            raise Exception()

        self.stubs.Set(self.compute, '_deallocate_network',
                _fake_deallocate_network)

        try:
            self.compute.terminate_instance(self.context, instance=instance)
        except Exception:
            pass

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)

    def test_stop(self):
        # Ensure instance can be stopped.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        inst_uuid = instance['uuid']
        extra = ['system_metadata', 'metadata']
        inst_obj = instance_obj.Instance.get_by_uuid(self.context,
                                                     inst_uuid,
                                                     expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_start(self):
        # Ensure instance can be started.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        extra = ['system_metadata', 'metadata']
        inst_uuid = instance['uuid']
        inst_obj = instance_obj.Instance.get_by_uuid(self.context,
                                                     inst_uuid,
                                                     expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save(self.context)
        self.compute.start_instance(self.context, instance=inst_obj)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_stop_start_no_image(self):
        params = {'image_ref': ''}
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        extra = ['system_metadata', 'metadata']
        inst_uuid = instance['uuid']
        inst_obj = instance_obj.Instance.get_by_uuid(self.context,
                                                     inst_uuid,
                                                     expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save(self.context)
        self.compute.start_instance(self.context, instance=inst_obj)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_rescue(self):
        # Ensure instance can be rescued and unrescued.

        called = {'rescued': False,
                  'unrescued': False}

        def fake_rescue(self, context, instance_ref, network_info, image_meta,
                        rescue_password):
            called['rescued'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'rescue', fake_rescue)

        def fake_unrescue(self, instance_ref, network_info):
            called['unrescued'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'unrescue',
                       fake_unrescue)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.RESCUING})
        self.compute.rescue_instance(self.context, instance=instance)
        self.assertTrue(called['rescued'])
        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.UNRESCUING})
        self.compute.unrescue_instance(self.context, instance=instance)
        self.assertTrue(called['unrescued'])

        self.compute.terminate_instance(self.context, instance=instance)

    def test_rescue_handle_err(self):
        # If the driver fails to rescue, instance state should remain the same
        # and the exception should be converted to InstanceNotRescuable
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.mox.StubOutWithMock(self.compute, '_get_rescue_image_ref')
        self.mox.StubOutWithMock(nova.virt.fake.FakeDriver, 'rescue')

        self.compute._get_rescue_image_ref(
            mox.IgnoreArg(), instance).AndReturn('resc_image_ref')
        nova.virt.fake.FakeDriver.rescue(
            mox.IgnoreArg(), instance, [], mox.IgnoreArg(), 'password'
            ).AndRaise(RuntimeError("Try again later"))

        self.mox.ReplayAll()

        expected_message = ('Instance %s cannot be rescued: '
                            'Driver Error: Try again later' % instance['uuid'])
        instance['vm_state'] = 'some_random_state'

        with testtools.ExpectedException(
            exception.InstanceNotRescuable, expected_message):
                self.compute.rescue_instance(
                    self.context, instance=instance,
                    rescue_password='password')

        self.assertEqual('some_random_state', instance['vm_state'])

    def test_power_on(self):
        # Ensure instance can be powered on.

        called = {'power_on': False}

        def fake_driver_power_on(self, instance):
            called['power_on'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'power_on',
                       fake_driver_power_on)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        extra = ['system_metadata', 'metadata']
        inst_obj = instance_obj.Instance.get_by_uuid(self.context,
                                                     instance['uuid'],
                                                     expected_attrs=extra)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save(self.context)
        self.compute.start_instance(self.context, instance=inst_obj)
        self.assertTrue(called['power_on'])
        self.compute.terminate_instance(self.context, instance=inst_obj)

    def test_power_off(self):
        # Ensure instance can be powered off.

        called = {'power_off': False}

        def fake_driver_power_off(self, instance):
            called['power_off'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'power_off',
                       fake_driver_power_off)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        extra = ['system_metadata', 'metadata']
        inst_obj = instance_obj.Instance.get_by_uuid(self.context,
                                                     instance['uuid'],
                                                     expected_attrs=extra)
        inst_obj.task_state = task_states.POWERING_OFF
        inst_obj.save(self.context)
        self.compute.stop_instance(self.context, instance=inst_obj)
        self.assertTrue(called['power_off'])
        self.compute.terminate_instance(self.context, instance=inst_obj)

    def test_pause(self):
        # Ensure instance can be paused and unpaused.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.PAUSING})
        self.compute.pause_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.UNPAUSING})
        self.compute.unpause_instance(self.context, instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_suspend(self):
        # ensure instance can be suspended and resumed.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.SUSPENDING})
        self.compute.suspend_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.RESUMING})
        self.compute.resume_instance(self.context, instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_suspend_error(self):
        # Ensure vm_state is ERROR when suspend error occurs.
        def fake(*args, **kwargs):
            raise test.TestingException()
        self.stubs.Set(self.compute.driver, 'suspend', fake)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)
        self.assertRaises(test.TestingException,
                          self.compute.suspend_instance,
                          self.context,
                          instance=instance)
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['vm_state'], vm_states.ERROR)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_rebuild(self):
        # Ensure instance can be rebuilt.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        image_ref = instance['image_ref']
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata,
                                      bdms=[])
        self.compute.terminate_instance(self.context, instance=instance)

    def test_rebuild_no_image(self):
        # Ensure instance can be rebuilt when started with no image.
        params = {'image_ref': ''}
        instance = self._create_fake_instance(params)
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      '', '', injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_rebuild_launched_at_time(self):
        # Ensure instance can be rebuilt.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        timeutils.set_time_override(old_time)
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        image_ref = instance['image_ref']

        self.compute.run_instance(self.context, instance=instance)
        timeutils.set_time_override(cur_time)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      bdms=[])
        instance = db.instance_get_by_uuid(self.context, instance_uuid,)
        self.assertEquals(cur_time, instance['launched_at'])
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def _test_reboot(self, soft, legacy_nwinfo_driver,
                     test_delete=False, test_unrescue=False,
                     fail_reboot=False, fail_running=False):
        # This is a true unit test, so we don't need the network stubs.
        fake_network.unset_stub_network_methods(self.stubs)

        self.mox.StubOutWithMock(self.compute, '_get_instance_nw_info')
        self.mox.StubOutWithMock(self.compute, '_notify_about_instance_usage')
        self.mox.StubOutWithMock(self.compute, '_instance_update')
        self.mox.StubOutWithMock(self.compute, '_get_power_state')
        self.mox.StubOutWithMock(self.compute.driver, 'legacy_nwinfo')
        self.mox.StubOutWithMock(self.compute.driver, 'reboot')

        instance = dict(uuid='fake-instance',
                        power_state='unknown',
                        vm_state=vm_states.ACTIVE,
                        launched_at=timeutils.utcnow())
        updated_instance1 = dict(uuid='updated-instance1',
                                 power_state='fake',
                                 vm_state=vm_states.ACTIVE,
                                 launched_at=timeutils.utcnow())
        updated_instance2 = dict(uuid='updated-instance2',
                                 power_state='fake',
                                 vm_state=vm_states.ACTIVE,
                                 launched_at=timeutils.utcnow())

        if test_unrescue:
            instance['vm_state'] = vm_states.RESCUED

        fake_nw_model = network_model.NetworkInfo()
        self.mox.StubOutWithMock(fake_nw_model, 'legacy')

        fake_block_dev_info = 'fake_block_dev_info'
        fake_power_state1 = 'fake_power_state1'
        fake_power_state2 = power_state.RUNNING
        fake_power_state3 = 'fake_power_state3'
        reboot_type = soft and 'SOFT' or 'HARD'

        # Beginning of calls we expect.

        # FIXME(comstud): I don't feel like the context needs to
        # be elevated at all.  Hopefully remove elevated from
        # reboot_instance and remove the stub here in a future patch.
        # econtext would just become self.context below then.
        econtext = self.context.elevated()

        self.mox.StubOutWithMock(self.context, 'elevated')
        self.context.elevated().AndReturn(econtext)

        self.compute._get_instance_nw_info(econtext,
                                           instance).AndReturn(
                                                   fake_nw_model)
        self.compute._notify_about_instance_usage(econtext,
                                                  instance,
                                                  'reboot.start')
        self.compute._get_power_state(econtext,
                instance).AndReturn(fake_power_state1)
        self.compute._instance_update(econtext, instance['uuid'],
                power_state=fake_power_state1).AndReturn(updated_instance1)

        # Reboot should check the driver to see if legacy nwinfo is
        # needed.  If it is, the model's legacy() method should be
        # called and the result passed to driver.reboot.  If the
        # driver wants the model, we pass the model.
        self.compute.driver.legacy_nwinfo().AndReturn(legacy_nwinfo_driver)
        if legacy_nwinfo_driver:
            expected_nw_info = 'legacy-nwinfo'
            fake_nw_model.legacy().AndReturn(expected_nw_info)
        else:
            expected_nw_info = fake_nw_model

        # Annoying.  driver.reboot is wrapped in a try/except, and
        # doesn't re-raise.  It eats exception generated by mox if
        # this is called with the wrong args, so we have to hack
        # around it.
        reboot_call_info = {}
        expected_call_info = {
            'args': (econtext, updated_instance1, expected_nw_info,
                     reboot_type),
            'kwargs': {'block_device_info': fake_block_dev_info}}

        def fake_reboot(*args, **kwargs):
            reboot_call_info['args'] = args
            reboot_call_info['kwargs'] = kwargs

            # NOTE(sirp): Since `bad_volumes_callback` is a function defined
            # within `reboot_instance`, we don't have access to its value and
            # can't stub it out, thus we skip that comparison.
            kwargs.pop('bad_volumes_callback')
            if fail_reboot:
                raise exception.InstanceNotFound(instance_id='instance-0000')

        self.stubs.Set(self.compute.driver, 'reboot', fake_reboot)

        # Power state should be updated again
        if not fail_reboot or fail_running:
            new_power_state = fake_power_state2
            self.compute._get_power_state(econtext,
                    updated_instance1).AndReturn(fake_power_state2)
        else:
            new_power_state = fake_power_state3
            self.compute._get_power_state(econtext,
                    updated_instance1).AndReturn(fake_power_state3)

        if test_delete:
            self.compute._instance_update(econtext, updated_instance1['uuid'],
                    power_state=new_power_state,
                    task_state=None,
                    vm_state=vm_states.ACTIVE).AndRaise(
                        exception.InstanceNotFound(
                            instance_id=updated_instance1['uuid']))
            self.compute._notify_about_instance_usage(econtext,
                                                      updated_instance1,
                                                      'reboot.end')
        elif fail_reboot and not fail_running:
            self.compute._instance_update(econtext, updated_instance1['uuid'],
                    vm_state=vm_states.ERROR).AndRaise(
                        exception.InstanceNotFound(
                            instance_id=updated_instance1['uuid']))

        else:
            self.compute._instance_update(econtext, updated_instance1['uuid'],
                    power_state=new_power_state,
                    task_state=None,
                    vm_state=vm_states.ACTIVE).AndReturn(updated_instance2)
            self.compute._notify_about_instance_usage(econtext,
                                                      updated_instance2,
                                                      'reboot.end')

        self.mox.ReplayAll()

        if not fail_reboot or fail_running:
            self.compute.reboot_instance(self.context, instance=instance,
                                         block_device_info=fake_block_dev_info,
                                         reboot_type=reboot_type)
        else:
            self.assertRaises(exception.InstanceNotFound,
                              self.compute.reboot_instance,
                              self.context, instance=instance,
                              block_device_info=fake_block_dev_info,
                              reboot_type=reboot_type)

        self.assertEqual(expected_call_info, reboot_call_info)

    def test_reboot_soft(self):
        self._test_reboot(True, False)

    def test_reboot_soft_and_delete(self):
        self._test_reboot(True, False, True)

    def test_reboot_soft_and_rescued(self):
        self._test_reboot(True, False, False, True)

    def test_reboot_soft_and_delete_and_rescued(self):
        self._test_reboot(True, False, True, True)

    def test_reboot_hard(self):
        self._test_reboot(False, False)

    def test_reboot_hard_and_delete(self):
        self._test_reboot(False, False, True)

    def test_reboot_hard_and_rescued(self):
        self._test_reboot(False, False, False, True)

    def test_reboot_hard_and_delete_and_rescued(self):
        self._test_reboot(False, False, True, True)

    def test_reboot_soft_legacy_nwinfo_driver(self):
        self._test_reboot(True, True)

    def test_reboot_hard_legacy_nwinfo_driver(self):
        self._test_reboot(False, True)

    def test_reboot_fail(self):
        self._test_reboot(False, False, fail_reboot=True)

    def test_reboot_fail_running(self):
        self._test_reboot(False, False, fail_reboot=True,
                          fail_running=True)

    def test_set_admin_password(self):
        # Ensure instance can have its admin password set.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {'task_state': task_states.UPDATING_PASSWORD})

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['vm_state'], vm_states.ACTIVE)
        self.assertEqual(inst_ref['task_state'], task_states.UPDATING_PASSWORD)

        self.compute.set_admin_password(self.context, instance=instance)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['vm_state'], vm_states.ACTIVE)
        self.assertEqual(inst_ref['task_state'], None)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(inst_ref))

    def test_set_admin_password_bad_state(self):
        # Test setting password while instance is rebuilding.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'], {
            "power_state": power_state.NOSTATE,
        })
        instance = jsonutils.to_primitive(db.instance_get_by_uuid(
                                          self.context, instance['uuid']))

        self.assertEqual(instance['power_state'], power_state.NOSTATE)

        def fake_driver_get_info(self2, _instance):
            return {'state': power_state.NOSTATE,
                    'max_mem': 0,
                    'mem': 0,
                    'num_cpu': 2,
                    'cpu_time': 0}

        self.stubs.Set(nova.virt.fake.FakeDriver, 'get_info',
                       fake_driver_get_info)

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.UPDATING_PASSWORD})
        self.assertRaises(exception.InstancePasswordSetFailed,
                          self.compute.set_admin_password,
                          self.context,
                          instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def _do_test_set_admin_password_driver_error(self, exc, expected_vm_state,
                                                 expected_task_state,
                                                 expected_exception):
        """Ensure expected exception is raised if set_admin_password fails."""

        def fake_sleep(_time):
            pass

        self.stubs.Set(time, 'sleep', fake_sleep)

        def fake_driver_set_pass(self2, _instance, _pwd):
            raise exc

        self.stubs.Set(nova.virt.fake.FakeDriver, 'set_admin_password',
                       fake_driver_set_pass)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {'task_state': task_states.UPDATING_PASSWORD})

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['vm_state'], vm_states.ACTIVE)
        self.assertEqual(inst_ref['task_state'], task_states.UPDATING_PASSWORD)

        #error raised from the driver should not reveal internal information
        #so a new error is raised
        self.assertRaises(expected_exception,
                          self.compute.set_admin_password,
                          self.context,
                          instance=jsonutils.to_primitive(inst_ref))

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['vm_state'], expected_vm_state)
        self.assertEqual(inst_ref['task_state'], expected_task_state)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(inst_ref))

    def test_set_admin_password_driver_not_authorized(self):
        """
        Ensure expected exception is raised if set_admin_password not
        authorized.
        """
        exc = exception.NotAuthorized(_('Internal error'))
        expected_exception = exception.InstancePasswordSetFailed
        self._do_test_set_admin_password_driver_error(exc,
                                                vm_states.ERROR,
                                                None,
                                                expected_exception)

    def test_set_admin_password_driver_not_implemented(self):
        """
        Ensure expected exception is raised if set_admin_password not
        implemented by driver.
        """
        exc = NotImplementedError()
        expected_exception = NotImplementedError
        self._do_test_set_admin_password_driver_error(exc,
                                                      vm_states.ACTIVE,
                                                      None,
                                                      expected_exception)

    def test_inject_file(self):
        # Ensure we can write a file to an instance.
        called = {'inject': False}

        def fake_driver_inject_file(self2, instance, path, contents):
            self.assertEqual(path, "/tmp/test")
            self.assertEqual(contents, "File Contents")
            called['inject'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'inject_file',
                       fake_driver_inject_file)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        self.compute.inject_file(self.context, "/tmp/test",
                "File Contents", instance=instance)
        self.assertTrue(called['inject'])
        self.compute.terminate_instance(self.context, instance=instance)

    def test_inject_network_info(self):
        # Ensure we can inject network info.
        called = {'inject': False}

        def fake_driver_inject_network(self, instance, network_info):
            called['inject'] = True

        self.stubs.Set(nova.virt.fake.FakeDriver, 'inject_network_info',
                       fake_driver_inject_network)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        self.compute.inject_network_info(self.context, instance=instance)
        self.assertTrue(called['inject'])
        self.compute.terminate_instance(self.context, instance=instance)

    def test_reset_network(self):
        # Ensure we can reset networking on an instance.
        called = {'count': 0}

        def fake_driver_reset_network(self, instance):
            called['count'] += 1

        self.stubs.Set(nova.virt.fake.FakeDriver, 'reset_network',
                       fake_driver_reset_network)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        self.compute.reset_network(self.context, instance=instance)

        self.assertEqual(called['count'], 1)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_live_snapshot(self):
        # Ensure instance can be live_snapshotted.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        name = "myfakesnapshot"
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.IMAGE_LIVE_SNAPSHOT})
        self.compute.live_snapshot_instance(self.context, name,
                                            instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_snapshot(self):
        # Ensure instance can be snapshotted.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        name = "myfakesnapshot"
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.IMAGE_SNAPSHOT})
        self.compute.snapshot_instance(self.context, name, instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_snapshot_no_image(self):
        params = {'image_ref': ''}
        name = "myfakesnapshot"
        instance = self._create_fake_instance(params)
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.IMAGE_SNAPSHOT})
        self.compute.snapshot_instance(self.context, name, instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_snapshot_fails(self):
        # Ensure task_state is set to None if snapshot fails.
        def fake_snapshot(*args, **kwargs):
            raise test.TestingException()

        self.stubs.Set(self.compute.driver, 'snapshot', fake_snapshot)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.IMAGE_SNAPSHOT})
        self.assertRaises(test.TestingException,
                          self.compute.snapshot_instance,
                          self.context, "failing_snapshot", instance=instance)
        self._assert_state({'task_state': None})
        self.compute.terminate_instance(self.context, instance=instance)

    def _assert_state(self, state_dict):
        """Assert state of VM is equal to state passed as parameter."""
        instances = db.instance_get_all(self.context)
        self.assertEqual(len(instances), 1)

        if 'vm_state' in state_dict:
            self.assertEqual(state_dict['vm_state'], instances[0]['vm_state'])
        if 'task_state' in state_dict:
            self.assertEqual(state_dict['task_state'],
                             instances[0]['task_state'])
        if 'power_state' in state_dict:
            self.assertEqual(state_dict['power_state'],
                             instances[0]['power_state'])

    def test_console_output(self):
        # Make sure we can get console output from instance.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        output = self.compute.get_console_output(self.context,
                instance=instance)
        self.assertEqual(output, 'FAKE CONSOLE OUTPUT\nANOTHER\nLAST LINE')
        self.compute.terminate_instance(self.context, instance=instance)

    def test_console_output_tail(self):
        # Make sure we can get console output from instance.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        output = self.compute.get_console_output(self.context,
                instance=instance, tail_length=2)
        self.assertEqual(output, 'ANOTHER\nLAST LINE')
        self.compute.terminate_instance(self.context, instance=instance)

    def test_novnc_vnc_console(self):
        # Make sure we can a vnc console for an instance.
        self.flags(vnc_enabled=True)
        self.flags(enabled=False, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        # Try with the full instance
        console = self.compute.get_vnc_console(self.context, 'novnc',
                                               instance=instance)
        self.assert_(console)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_validate_console_port_vnc(self):
        self.flags(vnc_enabled=True)
        self.flags(enabled=True, group='spice')
        instance = jsonutils.to_primitive(self._create_fake_instance())

        def fake_driver_get_console(*args, **kwargs):
            return {'host': "fake_host", 'port': "5900",
                    'internal_access_path': None}
        self.stubs.Set(self.compute.driver, "get_vnc_console",
                       fake_driver_get_console)

        self.assertTrue(self.compute.validate_console_port(self.context,
                                                            instance,
                                                            "5900",
                                                            "novnc"))

    def test_validate_console_port_spice(self):
        self.flags(vnc_enabled=True)
        self.flags(enabled=True, group='spice')
        instance = jsonutils.to_primitive(self._create_fake_instance())

        def fake_driver_get_console(*args, **kwargs):
            return {'host': "fake_host", 'port': "5900",
                    'internal_access_path': None}
        self.stubs.Set(self.compute.driver, "get_spice_console",
                       fake_driver_get_console)

        self.assertTrue(self.compute.validate_console_port(self.context,
                                                            instance,
                                                            "5900",
                                                            "spice-html5"))

    def test_validate_console_port_wrong_port(self):
        self.flags(vnc_enabled=True)
        self.flags(enabled=True, group='spice')
        instance = jsonutils.to_primitive(self._create_fake_instance())

        def fake_driver_get_console(*args, **kwargs):
            return {'host': "fake_host", 'port': "5900",
                    'internal_access_path': None}
        self.stubs.Set(self.compute.driver, "get_vnc_console",
                       fake_driver_get_console)

        self.assertFalse(self.compute.validate_console_port(self.context,
                                                            instance,
                                                            "wrongport",
                                                            "spice-html5"))

    def test_xvpvnc_vnc_console(self):
        # Make sure we can a vnc console for an instance.
        self.flags(vnc_enabled=True)
        self.flags(enabled=False, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        console = self.compute.get_vnc_console(self.context, 'xvpvnc',
                                               instance=instance)
        self.assert_(console)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_invalid_vnc_console_type(self):
        # Raise useful error if console type is an unrecognised string.
        self.flags(vnc_enabled=True)
        self.flags(enabled=False, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(rpc_common.ClientException,
                          self.compute.get_vnc_console,
                          self.context, 'invalid', instance=instance)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_vnc_console,
                          self.context, 'invalid', instance=instance)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_missing_vnc_console_type(self):
        # Raise useful error is console type is None.
        self.flags(vnc_enabled=True)
        self.flags(enabled=False, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(rpc_common.ClientException,
                          self.compute.get_vnc_console,
                          self.context, None, instance=instance)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_vnc_console,
                          self.context, None, instance=instance)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_spicehtml5_spice_console(self):
        # Make sure we can a spice console for an instance.
        self.flags(vnc_enabled=False)
        self.flags(enabled=True, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        # Try with the full instance
        console = self.compute.get_spice_console(self.context, 'spice-html5',
                                               instance=instance)
        self.assert_(console)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_invalid_spice_console_type(self):
        # Raise useful error if console type is an unrecognised string
        self.flags(vnc_enabled=False)
        self.flags(enabled=True, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(rpc_common.ClientException,
                          self.compute.get_spice_console,
                          self.context, 'invalid', instance=instance)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_spice_console,
                          self.context, 'invalid', instance=instance)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_missing_spice_console_type(self):
        # Raise useful error is console type is None
        self.flags(vnc_enabled=False)
        self.flags(enabled=True, group='spice')

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(rpc_common.ClientException,
                          self.compute.get_spice_console,
                          self.context, None, instance=instance)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_spice_console,
                          self.context, None, instance=instance)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_vnc_console_instance_not_ready(self):
        self.flags(vnc_enabled=True)
        self.flags(enabled=False, group='spice')
        instance = self._create_fake_instance(
                params={'vm_state': vm_states.BUILDING})
        instance = jsonutils.to_primitive(instance)

        def fake_driver_get_console(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.stubs.Set(self.compute.driver, "get_vnc_console",
                       fake_driver_get_console)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.InstanceNotReady,
                self.compute.get_vnc_console, self.context, 'novnc',
                instance=instance)

    def test_spice_console_instance_not_ready(self):
        self.flags(vnc_enabled=False)
        self.flags(enabled=True, group='spice')
        instance = self._create_fake_instance(
                params={'vm_state': vm_states.BUILDING})
        instance = jsonutils.to_primitive(instance)

        def fake_driver_get_console(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.stubs.Set(self.compute.driver, "get_spice_console",
                       fake_driver_get_console)

        self.stub_out_client_exceptions()

        self.assertRaises(exception.InstanceNotReady,
                self.compute.get_spice_console, self.context, 'spice-html5',
                instance=instance)

    def test_diagnostics(self):
        # Make sure we can get diagnostics for an instance.
        expected_diagnostic = {'cpu0_time': 17300000000,
                             'memory': 524288,
                             'vda_errors': -1,
                             'vda_read': 262144,
                             'vda_read_req': 112,
                             'vda_write': 5778432,
                             'vda_write_req': 488,
                             'vnet1_rx': 2070139,
                             'vnet1_rx_drop': 0,
                             'vnet1_rx_errors': 0,
                             'vnet1_rx_packets': 26701,
                             'vnet1_tx': 140208,
                             'vnet1_tx_drop': 0,
                             'vnet1_tx_errors': 0,
                             'vnet1_tx_packets': 662,
                            }

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        diagnostics = self.compute.get_diagnostics(self.context,
                instance=instance)
        self.assertEqual(diagnostics, expected_diagnostic)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_add_fixed_ip_usage_notification(self):
        def dummy(*args, **kwargs):
            pass

        self.stubs.Set(network_api.API, 'add_fixed_ip_to_instance',
                       dummy)
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       'inject_network_info', dummy)
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       'reset_network', dummy)

        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 0)
        self.compute.add_fixed_ip_to_instance(self.context, network_id=1,
                instance=instance)

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_remove_fixed_ip_usage_notification(self):
        def dummy(*args, **kwargs):
            pass

        self.stubs.Set(network_api.API, 'remove_fixed_ip_from_instance',
                       dummy)
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       'inject_network_info', dummy)
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       'reset_network', dummy)

        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 0)
        self.compute.remove_fixed_ip_from_instance(self.context, 1,
                                                   instance=instance)

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_run_instance_usage_notification(self):
        # Ensure run instance generates appropriate usage notification.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        inst_ref = db.instance_get_by_uuid(self.context, instance_uuid)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'], 'compute.instance.create.start')
        self.assertEquals(msg['payload']['image_name'], 'fake_name')
        # The last event is the one with the sugar in it.
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg['priority'], 'INFO')
        self.assertEquals(msg['event_type'], 'compute.instance.create.end')
        payload = msg['payload']
        self.assertEquals(payload['tenant_id'], self.project_id)
        self.assertEquals(payload['image_name'], 'fake_name')
        self.assertEquals(payload['user_id'], self.user_id)
        self.assertEquals(payload['instance_id'], inst_ref['uuid'])
        self.assertEquals(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEquals(str(payload['instance_type_id']), str(type_id))
        self.assertEquals(payload['state'], 'active')
        self.assertTrue('display_name' in payload)
        self.assertTrue('created_at' in payload)
        self.assertTrue('launched_at' in payload)
        self.assertTrue(payload['launched_at'])
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEquals(payload['image_ref_url'], image_ref_url)
        self.assertEqual('Success', payload['message'])
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(inst_ref))

    def test_run_instance_end_notification_on_abort(self):
        # Test that an end notif is sent if the build is aborted
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']

        def build_inst_abort(*args, **kwargs):
            raise exception.BuildAbortException(reason="already deleted",
                    instance_uuid=instance_uuid)

        self.stubs.Set(self.compute, '_build_instance', build_inst_abort)

        self.compute.run_instance(self.context, instance=instance)
        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'], 'compute.instance.create.start')
        msg = test_notifier.NOTIFICATIONS[1]

        self.assertEquals(msg['event_type'], 'compute.instance.create.end')
        self.assertEquals('INFO', msg['priority'])
        payload = msg['payload']
        message = payload['message']
        self.assertTrue(message.find("already deleted") != -1)

    def test_run_instance_error_notification_on_reschedule(self):
        # Test that error notif is sent if the build got rescheduled
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']

        def build_inst_fail(*args, **kwargs):
            raise exception.RescheduledException(instance_uuid=instance_uuid,
                    reason="something bad happened")

        self.stubs.Set(self.compute, '_build_instance', build_inst_fail)

        self.compute.run_instance(self.context, instance=instance)

        self.assertTrue(len(test_notifier.NOTIFICATIONS) >= 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'], 'compute.instance.create.start')
        msg = test_notifier.NOTIFICATIONS[1]

        self.assertEquals(msg['event_type'], 'compute.instance.create.error')
        self.assertEquals('ERROR', msg['priority'])
        payload = msg['payload']
        message = payload['message']
        self.assertTrue(message.find("something bad happened") != -1)

    def test_run_instance_error_notification_on_failure(self):
        # Test that error notif is sent if build fails hard
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']

        def build_inst_fail(*args, **kwargs):
            raise test.TestingException("i'm dying")

        self.stubs.Set(self.compute, '_build_instance', build_inst_fail)

        self.assertRaises(test.TestingException, self.compute.run_instance,
                self.context, instance=instance)

        self.assertTrue(len(test_notifier.NOTIFICATIONS) >= 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'], 'compute.instance.create.start')
        msg = test_notifier.NOTIFICATIONS[1]

        self.assertEquals(msg['event_type'], 'compute.instance.create.error')
        self.assertEquals('ERROR', msg['priority'])
        payload = msg['payload']
        message = payload['message']
        self.assertTrue(message.find("i'm dying") != -1)

    def test_terminate_usage_notification(self):
        # Ensure terminate_instance generates correct usage notification.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        timeutils.set_time_override(old_time)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        test_notifier.NOTIFICATIONS = []
        timeutils.set_time_override(cur_time)
        self.compute.terminate_instance(self.context, instance=instance)

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 4)

        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['priority'], 'INFO')
        self.assertEquals(msg['event_type'], 'compute.instance.delete.start')
        msg1 = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg1['event_type'],
                                            'compute.instance.shutdown.start')
        msg1 = test_notifier.NOTIFICATIONS[2]
        self.assertEquals(msg1['event_type'], 'compute.instance.shutdown.end')
        msg1 = test_notifier.NOTIFICATIONS[3]
        self.assertEquals(msg1['event_type'], 'compute.instance.delete.end')
        payload = msg1['payload']
        self.assertEquals(payload['tenant_id'], self.project_id)
        self.assertEquals(payload['user_id'], self.user_id)
        self.assertEquals(payload['instance_id'], instance['uuid'])
        self.assertEquals(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEquals(str(payload['instance_type_id']), str(type_id))
        self.assertTrue('display_name' in payload)
        self.assertTrue('created_at' in payload)
        self.assertTrue('launched_at' in payload)
        self.assertTrue('deleted_at' in payload)
        self.assertEqual(payload['deleted_at'], timeutils.strtime(cur_time))
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEquals(payload['image_ref_url'], image_ref_url)

    def test_run_instance_existing(self):
        # Ensure failure when running an instance that already exists.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        self.assertRaises(exception.InstanceExists,
                          self.compute.run_instance,
                          self.context,
                          instance=instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_run_instance_queries_macs(self):
        # run_instance should ask the driver for node mac addresses and pass
        # that to the network_api in use.
        fake_network.unset_stub_network_methods(self.stubs)
        instance = jsonutils.to_primitive(self._create_fake_instance())

        macs = set(['01:23:45:67:89:ab'])
        self.mox.StubOutWithMock(self.compute.network_api,
                                 "allocate_for_instance")
        self.compute.network_api.allocate_for_instance(
            mox.IgnoreArg(),
            mox.IgnoreArg(),
            requested_networks=None,
            vpn=False, macs=macs,
            conductor_api=self.compute.conductor_api,
            security_groups=[]).AndReturn(
                fake_network.fake_get_instance_nw_info(self.stubs, 1, 1,
                                                       spectacular=True))
        self.mox.StubOutWithMock(self.compute.driver, "macs_for_instance")
        self.compute.driver.macs_for_instance(instance).AndReturn(macs)
        self.mox.ReplayAll()
        self.compute.run_instance(self.context, instance=instance)

    def test_instance_set_to_error_on_uncaught_exception(self):
        # Test that instance is set to error state when exception is raised.
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.mox.StubOutWithMock(self.compute.network_api,
                                 "allocate_for_instance")
        self.compute.network_api.allocate_for_instance(
                mox.IgnoreArg(),
                mox.IgnoreArg(),
                requested_networks=None,
                vpn=False, macs=None,
                conductor_api=self.compute.conductor_api,
                security_groups=[]
                ).AndRaise(rpc_common.RemoteError())

        fake_network.unset_stub_network_methods(self.stubs)

        self.mox.ReplayAll()

        self.assertRaises(rpc_common.RemoteError,
                          self.compute.run_instance,
                          self.context,
                          instance=instance)

        instance = db.instance_get_by_uuid(context.get_admin_context(),
                                           instance['uuid'])
        self.assertEqual(vm_states.ERROR, instance['vm_state'])

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def test_delete_instance_succedes_on_volume_fail(self):
        instance = self._create_fake_instance()

        def fake_cleanup_volumes(context, instance):
            raise test.TestingException()

        self.stubs.Set(self.compute, '_cleanup_volumes',
                       fake_cleanup_volumes)

        self.compute._delete_instance(self.context,
                instance=jsonutils.to_primitive(instance),
                bdms={})

    def test_delete_instance_keeps_net_on_power_off_fail(self):
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')
        self.mox.StubOutWithMock(self.compute, '_deallocate_network')
        exp = exception.InstancePowerOffFailure(reason='')
        self.compute.driver.destroy(mox.IgnoreArg(), mox.IgnoreArg(),
                                    mox.IgnoreArg()).AndRaise(exp)
        # mox will detect if _deallocate_network gets called unexpectedly
        self.mox.ReplayAll()
        instance = self._create_fake_instance()
        self.assertRaises(exception.InstancePowerOffFailure,
                          self.compute._delete_instance,
                          self.context,
                          instance=jsonutils.to_primitive(instance),
                          bdms={})

    def test_delete_instance_loses_net_on_other_fail(self):
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')
        self.mox.StubOutWithMock(self.compute, '_deallocate_network')
        exp = test.TestingException()
        self.compute.driver.destroy(mox.IgnoreArg(), mox.IgnoreArg(),
                                    mox.IgnoreArg()).AndRaise(exp)
        self.compute._deallocate_network(mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()
        instance = self._create_fake_instance()
        self.assertRaises(test.TestingException,
                          self.compute._delete_instance,
                          self.context,
                          instance=jsonutils.to_primitive(instance),
                          bdms={})

    def test_delete_instance_deletes_console_auth_tokens(self):
        instance = self._create_fake_instance()
        self.flags(vnc_enabled=True)

        self.tokens_deleted = False

        def fake_delete_tokens(*args, **kwargs):
            self.tokens_deleted = True

        cauth_rpcapi = self.compute.consoleauth_rpcapi
        self.stubs.Set(cauth_rpcapi, 'delete_tokens_for_instance',
                       fake_delete_tokens)

        self.compute._delete_instance(self.context,
                instance=jsonutils.to_primitive(instance),
                bdms={})

        self.assertTrue(self.tokens_deleted)

    def test_delete_instance_deletes_console_auth_tokens_cells(self):
        instance = self._create_fake_instance()
        self.flags(vnc_enabled=True)
        self.flags(enable=True, group='cells')

        self.tokens_deleted = False

        def fake_delete_tokens(*args, **kwargs):
            self.tokens_deleted = True

        cells_rpcapi = self.compute.cells_rpcapi
        self.stubs.Set(cells_rpcapi, 'consoleauth_delete_tokens',
                       fake_delete_tokens)

        self.compute._delete_instance(self.context,
                instance=jsonutils.to_primitive(instance),
                bdms={})

        self.assertTrue(self.tokens_deleted)

    def test_instance_termination_exception_sets_error(self):
        """Test that we handle InstanceTerminationFailure
        which is propagated up from the underlying driver.
        """
        instance = self._create_fake_instance()

        def fake_delete_instance(context, instance, bdms,
                                 reservations=None):
            raise exception.InstanceTerminationFailure(reason='')

        self.stubs.Set(self.compute, '_delete_instance',
                       fake_delete_instance)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)

    def test_network_is_deallocated_on_spawn_failure(self):
        # When a spawn fails the network must be deallocated.
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.mox.StubOutWithMock(self.compute, "_setup_block_device_mapping")
        self.compute._setup_block_device_mapping(
                mox.IgnoreArg(), mox.IgnoreArg(),
                mox.IgnoreArg()).AndRaise(rpc.common.RemoteError('', '', ''))

        self.mox.ReplayAll()

        self.assertRaises(rpc.common.RemoteError,
                          self.compute.run_instance,
                          self.context, instance=instance)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_lock(self):
        # ensure locked instance cannot be changed.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        non_admin_context = context.RequestContext(None,
                                                   None,
                                                   is_admin=False)

        def check_task_state(task_state):
            instance = db.instance_get_by_uuid(self.context, instance_uuid)
            self.assertEqual(instance['task_state'], task_state)

        # should fail with locked nonadmin context
        self.compute_api.lock(self.context, instance)
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertRaises(exception.InstanceIsLocked,
                          self.compute_api.reboot,
                          non_admin_context, instance, 'SOFT')
        check_task_state(None)

        # should fail with invalid task state
        self.compute_api.unlock(self.context, instance)
        instance = db.instance_update(self.context, instance_uuid,
                                      {'task_state': task_states.REBOOTING})
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.reboot,
                          non_admin_context, instance, 'SOFT')
        check_task_state(task_states.REBOOTING)

        # should succeed with admin context
        instance = db.instance_update(self.context, instance_uuid,
                                      {'task_state': None})
        self.compute_api.reboot(self.context, instance, 'SOFT')
        check_task_state(task_states.REBOOTING)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def _test_state_revert(self, instance, operation, pre_task_state,
                           post_task_state=None, kwargs=None):
        if kwargs is None:
            kwargs = {}

        # The API would have set task_state, so do that here to test
        # that the state gets reverted on failure
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": pre_task_state})

        orig_elevated = self.context.elevated
        orig_notify = self.compute._notify_about_instance_usage

        def _get_an_exception(*args, **kwargs):
            raise test.TestingException()

        self.stubs.Set(self.context, 'elevated', _get_an_exception)
        self.stubs.Set(self.compute,
                       '_notify_about_instance_usage', _get_an_exception)

        func = getattr(self.compute, operation)

        raised = False
        try:
            func(self.context, instance=instance, **kwargs)
        except test.TestingException:
            raised = True
        finally:
            # self.context.elevated() is called in tearDown()
            self.stubs.Set(self.context, 'elevated', orig_elevated)
            self.stubs.Set(self.compute,
                           '_notify_about_instance_usage', orig_notify)

        self.assertTrue(raised)

        # Fetch the instance's task_state and make sure it went to expected
        # post-state
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance["task_state"], post_task_state)

    def test_state_revert(self):
        # ensure that task_state is reverted after a failed operation.
        actions = [
            ("reboot_instance", task_states.REBOOTING),
            ("stop_instance", task_states.POWERING_OFF),
            ("start_instance", task_states.POWERING_ON),
            ("terminate_instance", task_states.DELETING,
                                   task_states.DELETING),
            ("power_off_instance", task_states.POWERING_OFF),
            ("power_on_instance", task_states.POWERING_ON),
            ("soft_delete_instance", task_states.SOFT_DELETING),
            ("restore_instance", task_states.RESTORING),
            ("rebuild_instance", task_states.REBUILDING, None,
                                 {'orig_image_ref': None,
                                  'image_ref': None,
                                  'injected_files': [],
                                  'new_pass': ''}),
            ("set_admin_password", task_states.UPDATING_PASSWORD),
            ("rescue_instance", task_states.RESCUING),
            ("unrescue_instance", task_states.UNRESCUING),
            ("revert_resize", task_states.RESIZE_REVERTING, None,
                              {'migration_id': None}),
            ("prep_resize", task_states.RESIZE_PREP, None,
                            {'image': {},
                             'instance_type': {}}),
            ("resize_instance", task_states.RESIZE_PREP, None,
                                {'migration_id': None,
                                 'image': {}}),
            ("pause_instance", task_states.PAUSING),
            ("unpause_instance", task_states.UNPAUSING),
            ("suspend_instance", task_states.SUSPENDING),
            ("resume_instance", task_states.RESUMING),
            ]

        instance = self._create_fake_instance()
        for operation in actions:
            self._test_state_revert(instance, *operation)

    def _ensure_quota_reservations_committed(self, expect_project=False):
        """Mock up commit of quota reservations."""
        reservations = list('fake_res')
        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'commit')
        nova.quota.QUOTAS.commit(mox.IgnoreArg(), reservations,
                                 project_id=(expect_project and
                                             self.context.project_id or
                                             None))
        self.mox.ReplayAll()
        return reservations

    def _ensure_quota_reservations_rolledback(self, expect_project=False):
        """Mock up rollback of quota reservations."""
        reservations = list('fake_res')
        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'rollback')
        nova.quota.QUOTAS.rollback(mox.IgnoreArg(), reservations,
                                   project_id=(expect_project and
                                               self.context.project_id or
                                               None))
        self.mox.ReplayAll()
        return reservations

    def test_quotas_succesful_delete(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        resvs = self._ensure_quota_reservations_committed(True)
        self.compute.terminate_instance(self.context, instance,
                                        bdms=None, reservations=resvs)

    def test_quotas_failed_delete(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())

        def fake_shutdown_instance(*args, **kwargs):
            raise test.TestingException()

        self.stubs.Set(self.compute, '_shutdown_instance',
                       fake_shutdown_instance)

        resvs = self._ensure_quota_reservations_rolledback(True)
        self.assertRaises(test.TestingException,
                          self.compute.terminate_instance,
                          self.context, instance,
                          bdms=None, reservations=resvs)

    def test_quotas_succesful_soft_delete(self):
        instance = jsonutils.to_primitive(self._create_fake_instance(
            params=dict(task_state=task_states.SOFT_DELETING)))
        resvs = self._ensure_quota_reservations_committed(True)
        self.compute.soft_delete_instance(self.context, instance,
                                          reservations=resvs)

    def test_quotas_failed_soft_delete(self):
        instance = jsonutils.to_primitive(self._create_fake_instance(
            params=dict(task_state=task_states.SOFT_DELETING)))

        def fake_soft_delete(*args, **kwargs):
            raise test.TestingException()

        self.stubs.Set(self.compute.driver, 'soft_delete',
                       fake_soft_delete)

        resvs = self._ensure_quota_reservations_rolledback(True)
        self.assertRaises(test.TestingException,
                          self.compute.soft_delete_instance,
                          self.context, instance,
                          reservations=resvs)

    def test_quotas_destroy_of_soft_deleted_instance(self):
        instance = jsonutils.to_primitive(self._create_fake_instance(
            params=dict(vm_state=vm_states.SOFT_DELETED)))
        # Termination should be successful, but quota reservations
        # rolled back because the instance was in SOFT_DELETED state.
        resvs = self._ensure_quota_reservations_rolledback()
        self.compute.terminate_instance(self.context, instance,
                                        bdms=None, reservations=resvs)

    def _test_finish_resize(self, power_on):
        # Contrived test to ensure finish_resize doesn't raise anything and
        # also tests resize from ACTIVE or STOPPED state which determines
        # if the resized instance is powered on or not.
        self.power_on = power_on
        self.fake_finish_migration_called = False

        def fake_finish_migration(context, migration, instance, disk_info,
                                  network_info, image_meta, resize_instance,
                                  block_device_info=None, power_on=True):
            # nova.conf sets the default flavor to m1.small and the test
            # sets the default flavor to m1.tiny so they should be different
            # which makes this a resize
            self.assertTrue(resize_instance)
            # ensure the power_on value is what we expect
            self.assertEqual(self.power_on, power_on)
            self.fake_finish_migration_called = True

        def fake_migration_update(context, id, values):
            # Ensure instance status updates is after the migration finish
            migration_ref = db.migration_get(context, id)
            instance_uuid = migration_ref['instance_uuid']
            instance = db.instance_get_by_uuid(context, instance_uuid)
            self.assertFalse(instance['vm_state'] == vm_states.RESIZED)
            self.assertEqual(instance['task_state'], task_states.RESIZE_FINISH)

        self.stubs.Set(self.compute.driver, 'finish_migration',
                       fake_finish_migration)
        self.stubs.Set(db, 'migration_update', fake_migration_update)

        reservations = self._ensure_quota_reservations_committed()

        vm_state = None
        if power_on:
            vm_state = vm_states.ACTIVE
        else:
            vm_state = vm_states.STOPPED
        params = {'vm_state': vm_state}
        instance = jsonutils.to_primitive(self._create_fake_instance(params))
        instance_type = flavors.get_default_flavor()
        db.instance_update(self.context, instance["uuid"],
                          {"task_state": task_states.RESIZE_PREP})
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={})
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), instance['uuid'], 'pre-migrating')
        db.instance_update(self.context, instance["uuid"],
                           {"task_state": task_states.RESIZE_MIGRATED})
        # NOTE(mriedem): make sure prep_resize set old_vm_state correctly
        inst_ref = get_primitive_instance_by_uuid(self.context,
                                                  instance['uuid'])
        sys_meta = utils.metadata_to_dict(inst_ref['system_metadata'])
        self.assertTrue('old_vm_state' in sys_meta)
        if power_on:
            self.assertEqual(vm_states.ACTIVE, sys_meta['old_vm_state'])
        else:
            self.assertEqual(vm_states.STOPPED, sys_meta['old_vm_state'])
        self.compute.finish_resize(self.context,
                migration=jsonutils.to_primitive(migration_ref),
                disk_info={}, image={}, instance=inst_ref,
                reservations=reservations)
        self.assertTrue(self.fake_finish_migration_called)
        self.compute.terminate_instance(self.context, instance=inst_ref)

    def test_finish_resize_from_active(self):
        self._test_finish_resize(power_on=True)

    def test_finish_resize_from_stopped(self):
        self._test_finish_resize(power_on=False)

    def test_finish_resize_with_volumes(self):
        """Contrived test to ensure finish_resize doesn't raise anything."""

        # create instance
        instance = jsonutils.to_primitive(self._create_fake_instance())

        # create volume
        volume_id = 'fake'
        volume = {'instance_uuid': None,
                  'device_name': None,
                  'volume_id': volume_id}

        # stub out volume attach
        def fake_volume_get(self, context, volume):
            return volume
        self.stubs.Set(cinder.API, "get", fake_volume_get)

        orig_connection_data = {
            'target_discovered': True,
            'target_iqn': 'iqn.2010-10.org.openstack:%s.1' % volume_id,
            'target_portal': '127.0.0.0.1:3260',
            'volume_id': volume_id,
        }
        connection_info = {
            'driver_volume_type': 'iscsi',
            'data': orig_connection_data,
        }

        def fake_init_conn(self, context, volume, session):
            return connection_info
        self.stubs.Set(cinder.API, "initialize_connection", fake_init_conn)

        def fake_attach(self, context, volume_id, instance_uuid, device_name):
            volume['instance_uuid'] = instance_uuid
            volume['device_name'] = device_name
        self.stubs.Set(cinder.API, "attach", fake_attach)

        # stub out virt driver attach
        def fake_get_volume_connector(*args, **kwargs):
            return {}
        self.stubs.Set(self.compute.driver, 'get_volume_connector',
                       fake_get_volume_connector)

        def fake_attach_volume(*args, **kwargs):
            pass
        self.stubs.Set(self.compute.driver, 'attach_volume',
                       fake_attach_volume)

        # attach volume to instance
        self.compute.attach_volume(self.context, volume['volume_id'],
                                   '/dev/vdc', instance)

        # assert volume attached correctly
        self.assertEquals(volume['device_name'], '/dev/vdc')
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEquals(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEquals(bdm['device_name'], volume['device_name'])
            self.assertEquals(bdm['connection_info'],
                              jsonutils.dumps(connection_info))

        # begin resize
        instance_type = flavors.get_default_flavor()
        db.instance_update(self.context, instance["uuid"],
                          {"task_state": task_states.RESIZE_PREP})
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={})

        # NOTE(sirp): `prep_resize` mutates the `system_metadata` attribute in
        # the DB but not on the instance passed in, so to sync the two, we need
        # to refetch the row from the DB
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])

        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), instance['uuid'], 'pre-migrating')

        # fake out detach for prep_resize (and later terminate)
        def fake_terminate_connection(self, context, volume, connector):
            connection_info['data'] = None
        self.stubs.Set(cinder.API, "terminate_connection",
                       fake_terminate_connection)

        self.compute.resize_instance(self.context, instance=instance,
                migration=migration_ref, image={},
                instance_type=jsonutils.to_primitive(instance_type))

        # assert bdm is unchanged
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEquals(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEquals(bdm['device_name'], volume['device_name'])
            cached_connection_info = jsonutils.loads(bdm['connection_info'])
            self.assertEquals(cached_connection_info['data'],
                              orig_connection_data)
        # but connection was terminated
        self.assertEquals(connection_info['data'], None)

        # stub out virt driver finish_migration
        def fake(*args, **kwargs):
            pass
        self.stubs.Set(self.compute.driver, 'finish_migration', fake)

        db.instance_update(self.context, instance["uuid"],
                           {"task_state": task_states.RESIZE_MIGRATED})

        reservations = self._ensure_quota_reservations_committed()

        # new initialize connection
        new_connection_data = dict(orig_connection_data)
        new_iqn = 'iqn.2010-10.org.openstack:%s.2' % volume_id,
        new_connection_data['target_iqn'] = new_iqn

        def fake_init_conn_with_data(self, context, volume, session):
            connection_info['data'] = new_connection_data
            return connection_info
        self.stubs.Set(cinder.API, "initialize_connection",
                       fake_init_conn_with_data)

        self.compute.finish_resize(self.context,
                migration=jsonutils.to_primitive(migration_ref),
                disk_info={}, image={}, instance=instance,
                reservations=reservations)

        # assert volume attached correctly
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEquals(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEquals(bdm['connection_info'],
                              jsonutils.dumps(connection_info))

        # stub out detach
        def fake_detach(self, context, volume_uuid):
            volume['device_path'] = None
            volume['instance_uuid'] = None
        self.stubs.Set(cinder.API, "detach", fake_detach)

        # clean up
        self.compute.terminate_instance(self.context, instance=instance)

    def test_finish_resize_handles_error(self):
        # Make sure we don't leave the instance in RESIZE on error.

        def throw_up(*args, **kwargs):
            raise test.TestingException()

        def fake(*args, **kwargs):
            pass

        self.stubs.Set(self.compute.driver, 'finish_migration', throw_up)

        reservations = self._ensure_quota_reservations_rolledback()

        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_type = flavors.get_default_flavor()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={}, reservations=reservations)
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), instance['uuid'], 'pre-migrating')

        db.instance_update(self.context, instance["uuid"],
                           {"task_state": task_states.RESIZE_MIGRATED})
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertRaises(test.TestingException, self.compute.finish_resize,
                          self.context,
                          migration=jsonutils.to_primitive(migration_ref),
                          disk_info={}, image={}, instance=instance,
                          reservations=reservations)

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def test_rebuild_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        timeutils.set_time_override(old_time)
        inst_ref = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=inst_ref)
        timeutils.set_time_override(cur_time)

        test_notifier.NOTIFICATIONS = []
        instance = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        orig_sys_metadata = db.instance_system_metadata_get(self.context,
                inst_ref['uuid'])
        image_ref = instance["image_ref"]
        new_image_ref = image_ref + '-new_image_ref'
        db.instance_update(self.context, inst_ref['uuid'],
                           {'image_ref': new_image_ref})

        password = "new_password"

        instance = db.instance_get_by_uuid(self.context, inst_ref['uuid'])

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context,
                                      jsonutils.to_primitive(instance),
                                      image_ref, new_image_ref,
                                      injected_files=[],
                                      new_pass=password,
                                      orig_sys_metadata=orig_sys_metadata,
                                      bdms=[])

        instance = db.instance_get_by_uuid(self.context, inst_ref['uuid'])

        image_ref_url = glance.generate_image_url(image_ref)
        new_image_ref_url = glance.generate_image_url(new_image_ref)

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 3)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'],
                          'compute.instance.exists')
        self.assertEquals(msg['payload']['image_ref_url'], image_ref_url)
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg['event_type'],
                          'compute.instance.rebuild.start')
        self.assertEquals(msg['payload']['image_ref_url'], new_image_ref_url)
        self.assertEquals(msg['payload']['image_name'], 'fake_name')
        msg = test_notifier.NOTIFICATIONS[2]
        self.assertEquals(msg['event_type'],
                          'compute.instance.rebuild.end')
        self.assertEquals(msg['priority'], 'INFO')
        payload = msg['payload']
        self.assertEquals(payload['image_name'], 'fake_name')
        self.assertEquals(payload['tenant_id'], self.project_id)
        self.assertEquals(payload['user_id'], self.user_id)
        self.assertEquals(payload['instance_id'], inst_ref['uuid'])
        self.assertEquals(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEquals(str(payload['instance_type_id']), str(type_id))
        self.assertTrue('display_name' in payload)
        self.assertTrue('created_at' in payload)
        self.assertTrue('launched_at' in payload)
        self.assertEqual(payload['launched_at'], timeutils.strtime(cur_time))
        self.assertEquals(payload['image_ref_url'], new_image_ref_url)
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(inst_ref))

    def test_finish_resize_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        timeutils.set_time_override(old_time)
        instance = jsonutils.to_primitive(self._create_fake_instance())
        new_type = flavors.get_flavor_by_name('m1.small')
        new_type = jsonutils.to_primitive(new_type)
        new_type_id = new_type['id']
        self.compute.run_instance(self.context, instance=instance)

        new_instance = db.instance_update(self.context, instance['uuid'],
                                      {'host': 'foo'})
        new_instance = jsonutils.to_primitive(new_instance)
        db.instance_update(self.context, new_instance["uuid"],
                           {"task_state": task_states.RESIZE_PREP})
        self.compute.prep_resize(self.context, instance=new_instance,
                instance_type=new_type, image={})
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), new_instance['uuid'], 'pre-migrating')
        self.compute.resize_instance(self.context, instance=new_instance,
                migration=migration_ref, image={}, instance_type=new_type)
        timeutils.set_time_override(cur_time)
        test_notifier.NOTIFICATIONS = []

        new_instance = db.instance_get_by_uuid(self.context,
                                               new_instance['uuid'])
        self.compute.finish_resize(self.context,
                migration=jsonutils.to_primitive(migration_ref),
                disk_info={}, image={}, instance=new_instance)

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'],
                          'compute.instance.finish_resize.start')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg['event_type'],
                          'compute.instance.finish_resize.end')
        self.assertEquals(msg['priority'], 'INFO')
        payload = msg['payload']
        self.assertEquals(payload['tenant_id'], self.project_id)
        self.assertEquals(payload['user_id'], self.user_id)
        self.assertEquals(payload['instance_id'], new_instance['uuid'])
        self.assertEquals(payload['instance_type'], 'm1.small')
        self.assertEquals(str(payload['instance_type_id']), str(new_type_id))
        self.assertTrue('display_name' in payload)
        self.assertTrue('created_at' in payload)
        self.assertTrue('launched_at' in payload)
        self.assertEqual(payload['launched_at'], timeutils.strtime(cur_time))
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEquals(payload['image_ref_url'], image_ref_url)
        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(new_instance))

    def test_resize_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        timeutils.set_time_override(old_time)
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)
        timeutils.set_time_override(cur_time)
        test_notifier.NOTIFICATIONS = []

        new_instance = db.instance_update(self.context, instance['uuid'],
                                      {'host': 'foo'})
        new_instance = jsonutils.to_primitive(new_instance)
        instance_type = flavors.get_default_flavor()
        self.compute.prep_resize(self.context, instance=new_instance,
                instance_type=instance_type, image={})
        db.migration_get_by_instance_and_status(self.context.elevated(),
                                                new_instance['uuid'],
                                                'pre-migrating')

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 3)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEquals(msg['event_type'],
                          'compute.instance.exists')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEquals(msg['event_type'],
                          'compute.instance.resize.prep.start')
        msg = test_notifier.NOTIFICATIONS[2]
        self.assertEquals(msg['event_type'],
                          'compute.instance.resize.prep.end')
        self.assertEquals(msg['priority'], 'INFO')
        payload = msg['payload']
        self.assertEquals(payload['tenant_id'], self.project_id)
        self.assertEquals(payload['user_id'], self.user_id)
        self.assertEquals(payload['instance_id'], new_instance['uuid'])
        self.assertEquals(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEquals(str(payload['instance_type_id']), str(type_id))
        self.assertTrue('display_name' in payload)
        self.assertTrue('created_at' in payload)
        self.assertTrue('launched_at' in payload)
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEquals(payload['image_ref_url'], image_ref_url)
        self.compute.terminate_instance(self.context, instance=new_instance)

    def test_prep_resize_instance_migration_error_on_same_host(self):
        """Ensure prep_resize raise a migration error if destination is set on
        the same source host and allow_resize_to_same_host is false
        """
        self.flags(host="foo", allow_resize_to_same_host=False)

        instance = jsonutils.to_primitive(self._create_fake_instance())

        reservations = self._ensure_quota_reservations_rolledback()

        self.compute.run_instance(self.context, instance=instance)
        new_instance = db.instance_update(self.context, instance['uuid'],
                                          {'host': self.compute.host})
        new_instance = jsonutils.to_primitive(new_instance)
        instance_type = flavors.get_default_flavor()

        self.assertRaises(exception.MigrationError, self.compute.prep_resize,
                          self.context, instance=new_instance,
                          instance_type=instance_type, image={},
                          reservations=reservations)
        self.compute.terminate_instance(self.context, instance=new_instance)

    def test_prep_resize_instance_migration_error_on_none_host(self):
        """Ensure prep_resize raises a migration error if destination host is
        not defined
        """
        instance = jsonutils.to_primitive(self._create_fake_instance())

        reservations = self._ensure_quota_reservations_rolledback()

        self.compute.run_instance(self.context, instance=instance)
        new_instance = db.instance_update(self.context, instance['uuid'],
                                          {'host': None})
        new_instance = jsonutils.to_primitive(new_instance)
        instance_type = flavors.get_default_flavor()

        self.assertRaises(exception.MigrationError, self.compute.prep_resize,
                          self.context, instance=new_instance,
                          instance_type=instance_type, image={},
                          reservations=reservations)
        self.compute.terminate_instance(self.context, instance=new_instance)

    def test_resize_instance_driver_error(self):
        # Ensure instance status set to Error on resize error.

        def throw_up(*args, **kwargs):
            raise test.TestingException()

        self.stubs.Set(self.compute.driver, 'migrate_disk_and_power_off',
                       throw_up)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_type = flavors.get_default_flavor()

        reservations = self._ensure_quota_reservations_rolledback()

        self.compute.run_instance(self.context, instance=instance)
        new_instance = db.instance_update(self.context, instance['uuid'],
                                      {'host': 'foo'})
        new_instance = jsonutils.to_primitive(new_instance)
        self.compute.prep_resize(self.context, instance=new_instance,
                                 instance_type=instance_type, image={},
                                 reservations=reservations)
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), new_instance['uuid'], 'pre-migrating')

        db.instance_update(self.context, new_instance['uuid'],
                           {"task_state": task_states.RESIZE_PREP})
        #verify
        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=new_instance,
                          migration=migration_ref, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type))
        instance = db.instance_get_by_uuid(self.context, new_instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)

        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(instance))

    def test_resize_instance_driver_rollback(self):
        # Ensure instance status set to Running after rollback.

        def throw_up(*args, **kwargs):
            raise exception.InstanceFaultRollback(test.TestingException())

        self.stubs.Set(self.compute.driver, 'migrate_disk_and_power_off',
                       throw_up)

        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_type = flavors.get_default_flavor()
        reservations = self._ensure_quota_reservations_rolledback()
        self.compute.run_instance(self.context, instance=instance)
        new_instance = db.instance_update(self.context, instance['uuid'],
                                      {'host': 'foo'})
        new_instance = jsonutils.to_primitive(new_instance)
        self.compute.prep_resize(self.context, instance=new_instance,
                                 instance_type=instance_type, image={},
                                 reservations=reservations)
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), new_instance['uuid'], 'pre-migrating')
        db.instance_update(self.context, new_instance['uuid'],
                           {"task_state": task_states.RESIZE_PREP})

        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=new_instance,
                          migration=migration_ref, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type))

        instance = db.instance_get_by_uuid(self.context, new_instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ACTIVE)
        self.assertEqual(instance['task_state'], None)

        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(instance))

    def test_resize_instance(self):
        # Ensure instance can be migrated/resized.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_type = flavors.get_default_flavor()

        self.compute.run_instance(self.context, instance=instance)
        new_instance = db.instance_update(self.context, instance['uuid'],
                                      {'host': 'foo'})
        new_instance = jsonutils.to_primitive(new_instance)
        instance_uuid = new_instance['uuid']
        self.compute.prep_resize(self.context, instance=new_instance,
                instance_type=instance_type, image={})
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), instance_uuid, 'pre-migrating')

        # verify 'old_vm_state' was set on system_metadata
        inst = db.instance_get_by_uuid(self.context, instance_uuid)
        sys_meta = utils.metadata_to_dict(inst['system_metadata'])
        self.assertEqual(vm_states.ACTIVE, sys_meta['old_vm_state'])

        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.RESIZE_PREP})
        self.compute.resize_instance(self.context, instance=new_instance,
                migration=migration_ref, image={},
                instance_type=jsonutils.to_primitive(instance_type))
        inst = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(migration_ref['dest_compute'], inst['host'])

        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(inst))

    def _test_confirm_resize(self, power_on):
        # Common test case method for confirm_resize
        def fake(*args, **kwargs):
            pass

        def fake_confirm_migration_driver(*args, **kwargs):
            # Confirm the instance uses the new type in finish_resize
            inst = args[1]
            sys_meta = utils.metadata_to_dict(inst['system_metadata'])
            self.assertEqual(sys_meta['instance_type_flavorid'], '3')

        old_vm_state = None
        if power_on:
            old_vm_state = vm_states.ACTIVE
        else:
            old_vm_state = vm_states.STOPPED
        params = {'vm_state': old_vm_state}
        instance = jsonutils.to_primitive(self._create_fake_instance(params))

        self.flags(allow_resize_to_same_host=True)
        self.stubs.Set(self.compute.driver, 'finish_migration', fake)
        self.stubs.Set(self.compute.driver, 'confirm_migration',
                       fake_confirm_migration_driver)

        reservations = self._ensure_quota_reservations_committed()

        instance_uuid = instance['uuid']

        self.compute.run_instance(self.context, instance=instance)

        # Confirm the instance size before the resize starts
        inst_ref = db.instance_get_by_uuid(self.context, instance_uuid)
        instance_type_ref = db.instance_type_get(self.context,
                inst_ref['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '1')

        new_inst_ref = db.instance_update(self.context, instance_uuid,
                                          {'vm_state': old_vm_state})

        new_instance_type_ref = db.instance_type_get_by_flavor_id(
                self.context, 3)
        new_instance_type_p = jsonutils.to_primitive(new_instance_type_ref)
        self.compute.prep_resize(self.context,
                instance=jsonutils.to_primitive(new_inst_ref),
                instance_type=new_instance_type_p,
                image={}, reservations=reservations)

        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(),
                inst_ref['uuid'], 'pre-migrating')

        # NOTE(danms): make sure to refresh our inst_ref after prep_resize
        instance = get_primitive_instance_by_uuid(self.context, instance_uuid)
        # NOTE(mriedem): ensure prep_resize set old_vm_state in system_metadata
        sys_meta = utils.metadata_to_dict(instance['system_metadata'])
        self.assertEqual(old_vm_state, sys_meta['old_vm_state'])
        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.RESIZE_PREP})
        self.compute.resize_instance(self.context, instance=instance,
                                     migration=migration_ref,
                                     image={},
                                     instance_type=new_instance_type_p)
        self.compute.finish_resize(self.context,
                    migration=jsonutils.to_primitive(migration_ref),
                    disk_info={}, image={}, instance=instance)

        # Prove that the instance size is now the new size
        rpcinst = get_primitive_instance_by_uuid(self.context, instance_uuid)
        instance_type_ref = db.instance_type_get(self.context,
                rpcinst['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '3')

        # Finally, confirm the resize and verify the new flavor is applied
        db.instance_update(self.context, instance_uuid,
                           {"task_state": None})

        def fake_setup_networks_on_host(cls, ctxt, instance, host,
                                        teardown):
            self.assertEqual(host, migration_ref['source_compute'])
            inst = db.instance_get_by_uuid(ctxt, instance['uuid'])
            self.assertEqual('fake-mini', inst['host'])
            self.assertTrue(teardown)

        self.stubs.Set(network_api.API, 'setup_networks_on_host',
                       fake_setup_networks_on_host)

        def fake_get_power_state(context, instance):
            if power_on:
                return power_state.RUNNING
            else:
                return power_state.SHUTDOWN

        self.stubs.Set(self.compute, '_get_power_state', fake_get_power_state)

        rpcinst = db.instance_get_by_uuid(self.context, rpcinst['uuid'])
        self.compute.confirm_resize(self.context, rpcinst, reservations,
                                    migration_ref)

        instance = get_primitive_instance_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        instance_type_ref = db.instance_type_get(self.context,
                instance['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '3')
        self.assertEqual('fake-mini', migration_ref['source_compute'])
        self.assertEqual(old_vm_state, instance['vm_state'])
        self.compute.terminate_instance(self.context, instance=instance)

    def test_confirm_resize_from_active(self):
        self._test_confirm_resize(power_on=True)

    def test_confirm_resize_from_stopped(self):
        self._test_confirm_resize(power_on=False)

    def _test_finish_revert_resize(self, power_on,
                                   remove_old_vm_state=False):
        """
        Convenience method that does most of the work for the
        test_finish_revert_resize tests.
        :param power_on -- True if testing resize from ACTIVE state, False if
        testing resize from STOPPED state.
        :param remove_old_vm_state -- True if testing a case where the
        'old_vm_state' system_metadata is not present when the
        finish_revert_resize method is called.
        """
        def fake(*args, **kwargs):
            pass

        def fake_finish_revert_migration_driver(*args, **kwargs):
            # Confirm the instance uses the old type in finish_revert_resize
            inst = args[0]
            sys_meta = utils.metadata_to_dict(inst['system_metadata'])
            self.assertEqual(sys_meta['instance_type_flavorid'], '1')

        old_vm_state = None
        if power_on:
            old_vm_state = vm_states.ACTIVE
        else:
            old_vm_state = vm_states.STOPPED
        params = {'vm_state': old_vm_state}
        instance = jsonutils.to_primitive(self._create_fake_instance(params))

        self.stubs.Set(self.compute.driver, 'finish_migration', fake)
        self.stubs.Set(self.compute.driver, 'finish_revert_migration',
                       fake_finish_revert_migration_driver)

        reservations = self._ensure_quota_reservations_committed()

        instance_uuid = instance['uuid']

        self.compute.run_instance(self.context, instance=instance)

        # Confirm the instance size before the resize starts
        inst_ref = db.instance_get_by_uuid(self.context, instance_uuid)
        instance_type_ref = db.instance_type_get(self.context,
                inst_ref['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '1')

        old_vm_state = instance['vm_state']
        new_inst_ref = db.instance_update(self.context, instance_uuid,
                                          {'host': 'foo',
                                           'vm_state': old_vm_state})

        new_instance_type_ref = db.instance_type_get_by_flavor_id(
                self.context, 3)
        new_instance_type_p = jsonutils.to_primitive(new_instance_type_ref)
        self.compute.prep_resize(self.context,
                instance=jsonutils.to_primitive(new_inst_ref),
                instance_type=new_instance_type_p,
                image={}, reservations=reservations)

        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(),
                inst_ref['uuid'], 'pre-migrating')

        # NOTE(danms): make sure to refresh our inst_ref after prep_resize
        instance = get_primitive_instance_by_uuid(self.context, instance_uuid)
        # NOTE(mriedem): ensure prep_resize set old_vm_state in system_metadata
        sys_meta = utils.metadata_to_dict(instance['system_metadata'])
        self.assertEqual(old_vm_state, sys_meta['old_vm_state'])
        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.RESIZE_PREP})
        self.compute.resize_instance(self.context, instance=instance,
                                     migration=migration_ref,
                                     image={},
                                     instance_type=new_instance_type_p)
        self.compute.finish_resize(self.context,
                    migration=jsonutils.to_primitive(migration_ref),
                    disk_info={}, image={}, instance=instance)

        # Prove that the instance size is now the new size
        rpcinst = get_primitive_instance_by_uuid(self.context, instance_uuid)
        instance_type_ref = db.instance_type_get(self.context,
                rpcinst['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '3')

        # Finally, revert and confirm the old flavor has been applied
        db.instance_update(self.context, instance_uuid,
                           {"task_state": task_states.RESIZE_REVERTING})
        self.compute.revert_resize(self.context,
                migration_id=migration_ref['id'], instance=rpcinst,
                reservations=reservations)

        def fake_setup_networks_on_host(cls, ctxt, instance, host,
                                        teardown=False):
            self.assertEqual(host, migration_ref['source_compute'])
            inst = db.instance_get_by_uuid(ctxt, instance['uuid'])
            self.assertEqual(host, inst['host'])
            self.assertFalse(teardown)

        self.stubs.Set(network_api.API, 'setup_networks_on_host',
                       fake_setup_networks_on_host)

        rpcinst = db.instance_get_by_uuid(self.context, rpcinst['uuid'])
        if remove_old_vm_state:
            # need to wipe out the old_vm_state from system_metadata
            # before calling finish_revert_resize
            sys_meta = utils.metadata_to_dict(rpcinst['system_metadata'])
            sys_meta.pop('old_vm_state')
            rpcinst = db.instance_update(self.context, rpcinst['uuid'],
                                         {'system_metadata': sys_meta})

        self.compute.finish_revert_resize(self.context,
                migration=jsonutils.to_primitive(migration_ref),
                instance=rpcinst, reservations=reservations)

        instance = get_primitive_instance_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        instance_type_ref = db.instance_type_get(self.context,
                instance['instance_type_id'])
        self.assertEqual(instance_type_ref['flavorid'], '1')
        self.assertEqual(instance['host'], migration_ref['source_compute'])
        if remove_old_vm_state:
            self.assertEqual(vm_states.ACTIVE, instance['vm_state'])
        else:
            self.assertEqual(old_vm_state, instance['vm_state'])

        self.compute.terminate_instance(self.context, instance=instance)

    def test_finish_revert_resize_from_active(self):
        self._test_finish_revert_resize(power_on=True)

    def test_finish_revert_resize_from_stopped(self):
        self._test_finish_revert_resize(power_on=False)

    def test_finish_revert_resize_from_stopped_remove_old_vm_state(self):
        # in  this case we resize from STOPPED but end up with ACTIVE
        # because the old_vm_state value is not present in
        # finish_revert_resize
        self._test_finish_revert_resize(power_on=False,
                                        remove_old_vm_state=True)

    def _test_cleanup_stored_instance_types(self, old, new, revert=False):
        migration = dict(old_instance_type_id=old,
                         new_instance_type_id=new)
        instance = dict(system_metadata=list())
        instance['system_metadata'].append(dict(key='instance_type_id',
                                                value=old))
        sys_meta = dict(instance_type_id=old)
        self.mox.StubOutWithMock(flavors, 'extract_flavor')
        self.mox.StubOutWithMock(flavors, 'delete_flavor_info')
        self.mox.StubOutWithMock(flavors, 'save_flavor_info')
        if revert:
            flavors.extract_flavor(instance, 'old_').AndReturn(
                {'instance_type_id': old})
            flavors.save_flavor_info(
                sys_meta, {'instance_type_id': old}).AndReturn(sys_meta)
        else:
            flavors.extract_flavor(instance).AndReturn(
                {'instance_type_id': new})
        flavors.delete_flavor_info(
            sys_meta, 'old_').AndReturn(sys_meta)
        flavors.delete_flavor_info(
            sys_meta, 'new_').AndReturn(sys_meta)

        self.mox.ReplayAll()
        res = self.compute._cleanup_stored_instance_types(migration, instance,
                                                          revert)
        self.assertEqual(res,
                         (sys_meta,
                          {'instance_type_id': revert and old or new}))

    def test_cleanup_stored_instance_types_for_resize(self):
        self._test_cleanup_stored_instance_types('1', '2')

    def test_cleanup_stored_instance_types_for_resize_with_update(self):
        self._test_cleanup_stored_instance_types('1', '2', True)

    def test_cleanup_stored_instance_types_for_migration(self):
        self._test_cleanup_stored_instance_types('1', '1')

    def test_cleanup_stored_instance_types_for_migration_with_update(self):
        self._test_cleanup_stored_instance_types('1', '1', True)

    def test_get_by_flavor_id(self):
        type = flavors.get_flavor_by_flavor_id(1)
        self.assertEqual(type['name'], 'm1.tiny')

    def test_resize_same_source_fails(self):
        """Ensure instance fails to migrate when source and destination are
        the same host.
        """
        reservations = self._ensure_quota_reservations_rolledback()
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance_type = flavors.get_default_flavor()
        self.assertRaises(exception.MigrationError, self.compute.prep_resize,
                self.context, instance=instance,
                instance_type=instance_type, image={},
                reservations=reservations)
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def test_resize_instance_handles_migration_error(self):
        # Ensure vm_state is ERROR when error occurs.
        def raise_migration_failure(*args):
            raise test.TestingException()
        self.stubs.Set(self.compute.driver,
                'migrate_disk_and_power_off',
                raise_migration_failure)

        reservations = self._ensure_quota_reservations_rolledback()

        inst_ref = jsonutils.to_primitive(self._create_fake_instance())
        instance_type = flavors.get_default_flavor()

        self.compute.run_instance(self.context, instance=inst_ref)
        inst_ref = db.instance_update(self.context, inst_ref['uuid'],
                                      {'host': 'foo'})
        inst_ref = jsonutils.to_primitive(inst_ref)
        self.compute.prep_resize(self.context, instance=inst_ref,
                                 instance_type=instance_type,
                                 image={}, reservations=reservations)
        migration_ref = db.migration_get_by_instance_and_status(
                self.context.elevated(), inst_ref['uuid'], 'pre-migrating')
        db.instance_update(self.context, inst_ref['uuid'],
                           {"task_state": task_states.RESIZE_PREP})
        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=inst_ref,
                          migration=migration_ref, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type))
        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        self.assertEqual(inst_ref['vm_state'], vm_states.ERROR)
        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(inst_ref))

    def test_check_can_live_migrate_source_works_correctly(self):
        # Confirm check_can_live_migrate_source works on positive path.
        def fake_method(*args, **kwargs):
            return {}
        self.stubs.Set(self.compute.driver, 'check_can_live_migrate_source',
                       fake_method)
        inst_ref = jsonutils.to_primitive(self._create_fake_instance(
                                          {'host': 'fake_host_2'}))

        self.mox.StubOutWithMock(db, 'instance_get')
        dest_check_data = {"test": "data"}

        self.mox.ReplayAll()
        ret = self.compute.check_can_live_migrate_source(self.context,
                                              dest_check_data=dest_check_data,
                                              instance=inst_ref)
        self.assertTrue(type(ret) == dict)

    def test_check_can_live_migrate_destination_works_correctly(self):
        # Confirm check_can_live_migrate_destination works on positive path.
        def fake_method(*args, **kwargs):
            return {}
        self.stubs.Set(self.compute.compute_rpcapi,
                       'check_can_live_migrate_source',
                       fake_method)
        inst_ref = jsonutils.to_primitive(self._create_fake_instance(
                                          {'host': 'fake_host_2'}))
        compute_info = {"compute": "info"}

        self.mox.StubOutWithMock(self.compute,
                                 '_get_compute_info')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_can_live_migrate_destination')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_can_live_migrate_destination_cleanup')

        dest_check_data = {"test": "data", "migrate_data": {"test": "data"}}
        self.compute._get_compute_info(
            self.context, inst_ref['host']).AndReturn(compute_info)
        self.compute._get_compute_info(
            self.context, CONF.host).AndReturn(compute_info)
        self.compute.driver.check_can_live_migrate_destination(self.context,
                inst_ref,
                compute_info, compute_info,
                True, False).AndReturn(dest_check_data)
        self.compute.compute_rpcapi.check_can_live_migrate_source(self.context,
                inst_ref, dest_check_data)
        self.compute.driver.check_can_live_migrate_destination_cleanup(
                self.context, dest_check_data)

        self.mox.ReplayAll()
        ret = self.compute.check_can_live_migrate_destination(self.context,
                block_migration=True, disk_over_commit=False,
                instance=inst_ref)
        self.assertTrue(type(ret) == dict)
        self.assertTrue("test" in ret)

    def test_check_can_live_migrate_destination_fails_dest_check(self):
        inst_ref = jsonutils.to_primitive(self._create_fake_instance(
                                          {'host': 'fake_host_2'}))
        compute_info = {"compute": "info"}

        self.mox.StubOutWithMock(self.compute,
                                 '_get_compute_info')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_can_live_migrate_destination')

        self.compute._get_compute_info(
            self.context, inst_ref['host']).AndReturn(compute_info)
        self.compute._get_compute_info(
            self.context, CONF.host).AndReturn(compute_info)
        self.compute.driver.check_can_live_migrate_destination(self.context,
                inst_ref,
                compute_info, compute_info,
                True, False).AndRaise(exception.Invalid())

        self.mox.ReplayAll()
        self.assertRaises(exception.Invalid,
                          self.compute.check_can_live_migrate_destination,
                          self.context, block_migration=True,
                          disk_over_commit=False, instance=inst_ref)

    def test_check_can_live_migrate_destination_fails_source(self):
        # Confirm check_can_live_migrate_destination works on positive path.
        inst_ref = jsonutils.to_primitive(self._create_fake_instance(
                                          {'host': 'fake_host_2'}))
        compute_info = {"compute": "info"}

        self.mox.StubOutWithMock(self.compute,
                                 '_get_compute_info')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_can_live_migrate_destination')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'check_can_live_migrate_source')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_can_live_migrate_destination_cleanup')

        dest_check_data = {"test": "data"}
        self.compute._get_compute_info(
            self.context, inst_ref['host']).AndReturn(compute_info)
        self.compute._get_compute_info(
            self.context, CONF.host).AndReturn(compute_info)
        self.compute.driver.check_can_live_migrate_destination(self.context,
                inst_ref,
                compute_info, compute_info,
                True, False).AndReturn(dest_check_data)
        self.compute.compute_rpcapi.check_can_live_migrate_source(self.context,
                inst_ref, dest_check_data).AndRaise(exception.Invalid())
        self.compute.driver.check_can_live_migrate_destination_cleanup(
                self.context, dest_check_data)

        self.mox.ReplayAll()
        self.assertRaises(exception.Invalid,
                          self.compute.check_can_live_migrate_destination,
                          self.context, block_migration=True,
                          disk_over_commit=False, instance=inst_ref)

    def test_pre_live_migration_instance_has_no_fixed_ip(self):
        # Confirm raising exception if instance doesn't have fixed_ip.
        # creating instance testdata
        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.mox.ReplayAll()
        self.assertRaises(exception.FixedIpNotFoundForInstance,
                          self.compute.pre_live_migration, self.context,
                          instance=instance)

    def test_pre_live_migration_works_correctly(self):
        # Confirm setup_compute_volume is called when volume is mounted.
        def stupid(*args, **kwargs):
            return fake_network.fake_get_instance_nw_info(self.stubs,
                                                          spectacular=True)
        self.stubs.Set(nova.compute.manager.ComputeManager,
                       '_get_instance_nw_info', stupid)

        # creating instance testdata
        instance = jsonutils.to_primitive(self._create_fake_instance(
                                          {'host': 'dummy'}))
        c = context.get_admin_context()
        nw_info = fake_network.fake_get_instance_nw_info(self.stubs)

        # creating mocks
        self.mox.StubOutWithMock(self.compute.driver, 'pre_live_migration')
        self.compute.driver.pre_live_migration(mox.IsA(c), mox.IsA(instance),
                                               {'block_device_mapping': []},
                                               mox.IgnoreArg(),
                                               mox.IgnoreArg())
        self.mox.StubOutWithMock(self.compute.driver,
                                 'ensure_filtering_rules_for_instance')
        self.compute.driver.ensure_filtering_rules_for_instance(
            mox.IsA(instance), nw_info)

        test_notifier.NOTIFICATIONS = []
        # start test
        self.mox.ReplayAll()
        migrate_data = {'is_shared_storage': False}
        ret = self.compute.pre_live_migration(c, instance=instance,
                                              block_migration=False,
                                              migrate_data=migrate_data)
        self.assertEqual(ret, None)
        self.assertEqual(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg['event_type'],
                         'compute.instance.live_migration.pre.start')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg['event_type'],
                         'compute.instance.live_migration.pre.end')

        # cleanup
        db.instance_destroy(c, instance['uuid'])

    def test_live_migration_exception_rolls_back(self):
        # Confirm exception when pre_live_migration fails.
        c = context.get_admin_context()

        src_host = 'fake-src-host'
        dest_host = 'fake-dest-host'
        instance = dict(uuid='fake_instance', host=src_host,
                        name='fake-name')
        updated_instance = 'fake_updated_instance'
        fake_bdms = [dict(volume_id='vol1-id'), dict(volume_id='vol2-id')]

        # creating mocks
        self.mox.StubOutWithMock(rpc, 'call')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'get_instance_disk_info')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'pre_live_migration')
        self.mox.StubOutWithMock(self.compute, '_instance_update')
        self.mox.StubOutWithMock(self.compute, '_get_instance_volume_bdms')
        self.mox.StubOutWithMock(self.compute.network_api,
                                 'setup_networks_on_host')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'remove_volume_connection')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'rollback_live_migration_at_destination')

        self.compute.driver.get_instance_disk_info(
                instance['name']).AndReturn('fake_disk')
        self.compute.compute_rpcapi.pre_live_migration(c,
                instance, True, 'fake_disk', dest_host,
                {}).AndRaise(test.TestingException())

        self.compute._instance_update(c, instance['uuid'],
                host=src_host, vm_state=vm_states.ACTIVE,
                task_state=None,
                expected_task_state=task_states.MIGRATING).AndReturn(
                        updated_instance)
        self.compute.network_api.setup_networks_on_host(c,
                updated_instance, self.compute.host)
        self.compute._get_instance_volume_bdms(c,
                updated_instance).AndReturn(fake_bdms)
        self.compute.compute_rpcapi.remove_volume_connection(
                c, updated_instance, 'vol1-id', dest_host)
        self.compute.compute_rpcapi.remove_volume_connection(
                c, updated_instance, 'vol2-id', dest_host)
        self.compute.compute_rpcapi.rollback_live_migration_at_destination(
                c, updated_instance, dest_host)

        # start test
        self.mox.ReplayAll()
        self.assertRaises(test.TestingException,
                          self.compute.live_migration,
                          c, dest=dest_host, block_migration=True,
                          instance=instance)

    def test_live_migration_works_correctly(self):
        # Confirm live_migration() works as expected correctly.
        # creating instance testdata
        c = context.get_admin_context()
        instance_ref = self._create_fake_instance({'host': 'dummy'})
        inst_uuid = instance_ref['uuid']
        inst_id = instance_ref['id']

        instance = jsonutils.to_primitive(db.instance_get(c, inst_id))
        # start test
        self.mox.ReplayAll()
        migrate_data = {'is_shared_storage': False}
        ret = self.compute.live_migration(c, dest=instance['host'],
                                          instance=instance,
                                          migrate_data=migrate_data)
        self.assertEqual(ret, None)

        # cleanup
        db.instance_destroy(c, inst_uuid)

    def test_post_live_migration_no_shared_storage_working_correctly(self):
        """Confirm post_live_migration() works correctly as expected
           for non shared storage migration.
        """
        # Create stubs
        result = {}

        def fakedestroy(*args, **kwargs):
            result['destroyed'] = True
        self.stubs.Set(self.compute.driver, 'destroy', fakedestroy)
        dest = 'desthost'
        srchost = self.compute.host

        # creating testdata
        c = context.get_admin_context()
        inst_ref = jsonutils.to_primitive(self._create_fake_instance({
                                          'host': srchost,
                                          'state_description': 'migrating',
                                          'state': power_state.PAUSED}))
        inst_uuid = inst_ref['uuid']
        inst_id = inst_ref['id']

        db.instance_update(c, inst_uuid,
                           {'task_state': task_states.MIGRATING,
                            'power_state': power_state.PAUSED})
        # creating mocks
        self.mox.StubOutWithMock(self.compute.driver, 'unfilter_instance')
        self.compute.driver.unfilter_instance(inst_ref, [])
        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'network_migrate_instance_start')
        migration = {'source_compute': srchost, 'dest_compute': dest, }
        self.compute.conductor_api.network_migrate_instance_start(c, inst_ref,
                                                                  migration)
        self.mox.StubOutWithMock(rpc, 'call')
        rpc.call(c, rpc.queue_get_for(c, CONF.compute_topic, dest),
            {"method": "post_live_migration_at_destination",
             "namespace": None,
             "args": {'instance': inst_ref, 'block_migration': False},
             "version": compute_rpcapi.ComputeAPI.BASE_RPC_API_VERSION},
            None)
        rpc.call(c, 'network', {'method': 'setup_networks_on_host',
                                'namespace': None,
                                'args': {'instance_id': inst_id,
                                         'host': self.compute.host,
                                         'teardown': True},
                                'version': '1.0'}, None)
        # start test
        self.mox.ReplayAll()
        migrate_data = {'is_shared_storage': False}
        self.compute._post_live_migration(c, inst_ref, dest,
                                          migrate_data=migrate_data)
        self.assertTrue('destroyed' in result)
        self.assertTrue(result['destroyed'] == True)

    def test_post_live_migration_working_correctly(self):
        # Confirm post_live_migration() works as expected correctly.
        dest = 'desthost'
        srchost = self.compute.host

        # creating testdata
        c = context.get_admin_context()
        inst_ref = jsonutils.to_primitive(self._create_fake_instance({
                                'host': srchost,
                                'state_description': 'migrating',
                                'state': power_state.PAUSED}))
        inst_uuid = inst_ref['uuid']
        inst_id = inst_ref['id']

        db.instance_update(c, inst_uuid,
                           {'task_state': task_states.MIGRATING,
                            'power_state': power_state.PAUSED})

        # creating mocks
        self.mox.StubOutWithMock(self.compute.driver, 'unfilter_instance')
        self.compute.driver.unfilter_instance(inst_ref, [])
        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'network_migrate_instance_start')
        migration = {'source_compute': srchost,
                     'dest_compute': dest, }
        self.compute.conductor_api.network_migrate_instance_start(c, inst_ref,
                                                                  migration)
        self.mox.StubOutWithMock(rpc, 'call')
        rpc.call(c, rpc.queue_get_for(c, CONF.compute_topic, dest),
            {"method": "post_live_migration_at_destination",
             "namespace": None,
             "args": {'instance': inst_ref, 'block_migration': False},
             "version": compute_rpcapi.ComputeAPI.BASE_RPC_API_VERSION},
            None)
        self.mox.StubOutWithMock(self.compute.driver, 'unplug_vifs')
        self.compute.driver.unplug_vifs(inst_ref, [])
        rpc.call(c, 'network', {'method': 'setup_networks_on_host',
                                'namespace': None,
                                'args': {'instance_id': inst_id,
                                         'host': self.compute.host,
                                         'teardown': True},
                                'version': '1.0'}, None)

        # start test
        self.mox.ReplayAll()
        self.compute._post_live_migration(c, inst_ref, dest)

    def _begin_post_live_migration_at_destination(self):
        self.mox.StubOutWithMock(self.compute.network_api,
                                 'setup_networks_on_host')
        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'network_migrate_instance_finish')
        self.mox.StubOutWithMock(self.compute, '_get_power_state')
        self.mox.StubOutWithMock(self.compute, '_get_compute_info')

        params = {'task_state': task_states.MIGRATING,
                  'power_state': power_state.PAUSED, }
        self.instance = jsonutils.to_primitive(
                                  self._create_fake_instance(params))

        self.admin_ctxt = context.get_admin_context()
        self.instance = db.instance_get_by_uuid(self.admin_ctxt,
                                                self.instance['uuid'])

        self.compute.network_api.setup_networks_on_host(self.admin_ctxt,
                                                        self.instance,
                                                        self.compute.host)
        migration = {'source_compute': self.instance['host'],
                     'dest_compute': self.compute.host, }
        self.compute.conductor_api.network_migrate_instance_finish(
                self.admin_ctxt, self.instance, migration)
        fake_net_info = []
        fake_block_dev_info = {'foo': 'bar'}
        self.compute.driver.post_live_migration_at_destination(self.admin_ctxt,
                self.instance,
                fake_net_info,
                False,
                fake_block_dev_info)
        self.compute._get_power_state(self.admin_ctxt,
                                      self.instance).AndReturn(
                                                     'fake_power_state')

    def _finish_post_live_migration_at_destination(self):
        self.compute.network_api.setup_networks_on_host(self.admin_ctxt,
                mox.IgnoreArg(), self.compute.host)

        test_notifier.NOTIFICATIONS = []
        self.mox.ReplayAll()

        self.compute.post_live_migration_at_destination(self.admin_ctxt,
                                                        self.instance)

        self.assertEqual(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg['event_type'],
                         'compute.instance.live_migration.post.dest.start')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg['event_type'],
                         'compute.instance.live_migration.post.dest.end')

        return self.compute.conductor_api.instance_get_by_uuid(self.admin_ctxt,
                                                        self.instance['uuid'])

    def test_post_live_migration_at_destination_with_compute_info(self):
        """The instance's node property should be updated correctly."""
        self._begin_post_live_migration_at_destination()
        hypervisor_hostname = 'fake_hypervisor_hostname'
        fake_compute_info = {'hypervisor_hostname': hypervisor_hostname}
        self.compute._get_compute_info(mox.IgnoreArg(),
                                       mox.IgnoreArg()).AndReturn(
                                                        fake_compute_info)
        updated = self._finish_post_live_migration_at_destination()
        self.assertEqual(updated['node'], hypervisor_hostname)

    def test_post_live_migration_at_destination_without_compute_info(self):
        """The instance's node property should be set to None if we fail to
           get compute_info.
        """
        self._begin_post_live_migration_at_destination()
        self.compute._get_compute_info(mox.IgnoreArg(),
                                       mox.IgnoreArg()).AndRaise(
                                                        exception.NotFound())
        updated = self._finish_post_live_migration_at_destination()
        self.assertIsNone(updated['node'])

    def test_rollback_live_migration_at_destination_correctly(self):
        # creating instance testdata
        c = context.get_admin_context()
        instance_ref = self._create_fake_instance({'host': 'dummy'})
        inst_uuid = instance_ref['uuid']
        inst_id = instance_ref['id']

        instance = jsonutils.to_primitive(db.instance_get(c, inst_id))
        test_notifier.NOTIFICATIONS = []
        # start test
        self.mox.ReplayAll()
        ret = self.compute.rollback_live_migration_at_destination(c,
                                          instance=instance)
        self.assertEqual(ret, None)
        self.assertEqual(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg['event_type'],
                       'compute.instance.live_migration.rollback.dest.start')
        msg = test_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg['event_type'],
                        'compute.instance.live_migration.rollback.dest.end')

        # cleanup
        db.instance_destroy(c, inst_uuid)

    def test_run_kill_vm(self):
        # Detect when a vm is terminated behind the scenes.
        self.stubs.Set(compute_manager.ComputeManager,
                '_report_driver_status', nop_report_driver_status)

        instance = jsonutils.to_primitive(self._create_fake_instance())

        self.compute.run_instance(self.context, instance=instance)

        instances = db.instance_get_all(self.context)
        LOG.info(_("Running instances: %s"), instances)
        self.assertEqual(len(instances), 1)

        instance_name = instances[0]['name']
        self.compute.driver.test_remove_vm(instance_name)

        # Force the compute manager to do its periodic poll
        ctxt = context.get_admin_context()
        self.compute._sync_power_states(ctxt)

        instances = db.instance_get_all(self.context)
        LOG.info(_("After force-killing instances: %s"), instances)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['task_state'], None)

    def test_add_instance_fault(self):
        instance = self._create_fake_instance()
        exc_info = None

        def fake_db_fault_create(ctxt, values):
            self.assertTrue(values['details'].startswith('test'))
            self.assertTrue('raise NotImplementedError' in values['details'])
            del values['details']

            expected = {
                'code': 500,
                'message': 'NotImplementedError',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEquals(expected, values)

        try:
            raise NotImplementedError('test')
        except NotImplementedError:
            exc_info = sys.exc_info()

        self.stubs.Set(nova.db, 'instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  self.compute.conductor_api,
                                                  instance,
                                                  NotImplementedError('test'),
                                                  exc_info)

    def test_add_instance_fault_with_remote_error(self):
        instance = self._create_fake_instance()
        exc_info = None

        def fake_db_fault_create(ctxt, values):
            self.assertTrue(values['details'].startswith('Remote error'))
            self.assertTrue('raise rpc_common.RemoteError'
                in values['details'])
            del values['details']

            expected = {
                'code': 500,
                'instance_uuid': instance['uuid'],
                'message': 'My Test Message',
                'host': self.compute.host
            }
            self.assertEquals(expected, values)

        try:
            raise rpc_common.RemoteError('test', 'My Test Message')
        except rpc_common.RemoteError as exc:
            exc_info = sys.exc_info()

        self.stubs.Set(nova.db, 'instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
            self.compute.conductor_api, instance, exc, exc_info)

    def test_add_instance_fault_user_error(self):
        instance = self._create_fake_instance()
        exc_info = None

        def fake_db_fault_create(ctxt, values):

            expected = {
                'code': 400,
                'message': 'Invalid',
                'details': 'fake details',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEquals(expected, values)

        user_exc = exception.Invalid('fake details', code=400)

        try:
            raise user_exc
        except exception.Invalid:
            exc_info = sys.exc_info()

        self.stubs.Set(nova.db, 'instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
            self.compute.conductor_api, instance, user_exc, exc_info)

    def test_add_instance_fault_no_exc_info(self):
        instance = self._create_fake_instance()

        def fake_db_fault_create(ctxt, values):
            expected = {
                'code': 500,
                'message': 'NotImplementedError',
                'details': 'test',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEquals(expected, values)

        self.stubs.Set(nova.db, 'instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  self.compute.conductor_api,
                                                  instance,
                                                  NotImplementedError('test'))

    def test_cleanup_running_deleted_instances(self):
        admin_context = context.get_admin_context()
        deleted_at = (timeutils.utcnow() -
                      datetime.timedelta(hours=1, minutes=5))
        instance = self._create_fake_instance({"deleted_at": deleted_at,
                                               "deleted": True})

        self.compute.host = instance['host']

        self.mox.StubOutWithMock(self.compute, '_get_instances_on_driver')
        self.compute._get_instances_on_driver(
            admin_context, {'deleted': True,
                            'soft_deleted': False,
                            'host': self.compute.host}).AndReturn([instance])
        self.flags(running_deleted_instance_timeout=3600,
                   running_deleted_instance_action='reap')

        bdms = []

        self.mox.StubOutWithMock(self.compute, "_shutdown_instance")
        self.compute._shutdown_instance(admin_context,
                                        instance,
                                        bdms).AndReturn(None)

        self.mox.StubOutWithMock(self.compute, "_cleanup_volumes")
        self.compute._cleanup_volumes(admin_context,
                                      instance['uuid'],
                                      bdms).AndReturn(None)

        self.mox.ReplayAll()
        self.compute._cleanup_running_deleted_instances(admin_context)

    def test_running_deleted_instances(self):
        admin_context = context.get_admin_context()

        self.compute.host = 'host'

        instance1 = {}
        instance1['deleted'] = True
        instance1['deleted_at'] = "sometimeago"

        self.mox.StubOutWithMock(self.compute, '_get_instances_on_driver')
        self.compute._get_instances_on_driver(
            admin_context, {'deleted': True,
                            'soft_deleted': False,
                            'host': self.compute.host}).AndReturn([instance1])

        self.mox.StubOutWithMock(timeutils, 'is_older_than')
        timeutils.is_older_than('sometimeago',
                    CONF.running_deleted_instance_timeout).AndReturn(True)

        self.mox.ReplayAll()
        val = self.compute._running_deleted_instances(admin_context)
        self.assertEqual(val, [instance1])

    def test_get_instance_nw_info(self):
        fake_network.unset_stub_network_methods(self.stubs)

        fake_instance = {'uuid': 'fake-instance'}
        fake_nw_info = network_model.NetworkInfo()

        self.mox.StubOutWithMock(self.compute.network_api,
                                 'get_instance_nw_info')
        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'instance_info_cache_update')
        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'instance_get_by_uuid')

        self.compute.conductor_api.instance_get_by_uuid(
            self.context, fake_instance['uuid']).AndReturn(fake_instance)
        self.compute.network_api.get_instance_nw_info(self.context,
                fake_instance, conductor_api=self.compute.conductor_api
                ).AndReturn(fake_nw_info)

        self.mox.ReplayAll()

        result = self.compute._get_instance_nw_info(self.context,
                                                    fake_instance)
        self.assertEqual(fake_nw_info, result)

    def test_heal_instance_info_cache(self):
        # Update on every call for the test
        self.flags(heal_instance_info_cache_interval=-1)
        ctxt = context.get_admin_context()

        instance_map = {}
        instances = []
        for x in xrange(5):
            uuid = 'fake-uuid-%s' % x
            instance_map[uuid] = {'uuid': uuid, 'host': CONF.host}
            instances.append(instance_map[uuid])

        call_info = {'get_all_by_host': 0, 'get_by_uuid': 0,
                'get_nw_info': 0, 'expected_instance': None}

        def fake_instance_get_all_by_host(context, host, columns_to_join):
            call_info['get_all_by_host'] += 1
            self.assertEqual(columns_to_join, [])
            return instances[:]

        def fake_instance_get_by_uuid(context, instance_uuid):
            if instance_uuid not in instance_map:
                raise exception.InstanceNotFound(instance_id=instance_uuid)
            call_info['get_by_uuid'] += 1
            return instance_map[instance_uuid]

        # NOTE(comstud): Override the stub in setUp()
        def fake_get_instance_nw_info(context, instance):
            # Note that this exception gets caught in compute/manager
            # and is ignored.  However, the below increment of
            # 'get_nw_info' won't happen, and you'll get an assert
            # failure checking it below.
            self.assertEqual(call_info['expected_instance'], instance)
            call_info['get_nw_info'] += 1

        self.stubs.Set(self.compute.conductor_api, 'instance_get_all_by_host',
                fake_instance_get_all_by_host)
        self.stubs.Set(self.compute.conductor_api, 'instance_get_by_uuid',
                fake_instance_get_by_uuid)
        self.stubs.Set(self.compute, '_get_instance_nw_info',
                fake_get_instance_nw_info)

        call_info['expected_instance'] = instances[0]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(1, call_info['get_all_by_host'])
        self.assertEqual(0, call_info['get_by_uuid'])
        self.assertEqual(1, call_info['get_nw_info'])

        call_info['expected_instance'] = instances[1]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(1, call_info['get_all_by_host'])
        self.assertEqual(1, call_info['get_by_uuid'])
        self.assertEqual(2, call_info['get_nw_info'])

        # Make an instance switch hosts
        instances[2]['host'] = 'not-me'
        # Make an instance disappear
        instance_map.pop(instances[3]['uuid'])
        # '2' and '3' should be skipped..
        call_info['expected_instance'] = instances[4]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(call_info['get_all_by_host'], 1)
        # Incremented for '2' and '4'.. '3' caused a raise above.
        self.assertEqual(call_info['get_by_uuid'], 3)
        self.assertEqual(call_info['get_nw_info'], 3)
        # Should be no more left.
        self.assertEqual(len(self.compute._instance_uuids_to_heal), 0)

        # This should cause a DB query now so we get first instance
        # back again
        call_info['expected_instance'] = instances[0]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(call_info['get_all_by_host'], 2)
        # Stays the same, because the instance came from the DB
        self.assertEqual(call_info['get_by_uuid'], 3)
        self.assertEqual(call_info['get_nw_info'], 4)

    def test_poll_rescued_instances(self):
        timed_out_time = timeutils.utcnow() - datetime.timedelta(minutes=5)
        not_timed_out_time = timeutils.utcnow()

        instances = [{'uuid': 'fake_uuid1', 'vm_state': vm_states.RESCUED,
                      'launched_at': timed_out_time},
                     {'uuid': 'fake_uuid2', 'vm_state': vm_states.RESCUED,
                      'launched_at': timed_out_time},
                     {'uuid': 'fake_uuid3', 'vm_state': vm_states.RESCUED,
                      'launched_at': not_timed_out_time}]
        unrescued_instances = {'fake_uuid1': False, 'fake_uuid2': False}

        def fake_instance_get_all_by_filters(context, filters,
                                             columns_to_join):
            self.assertEqual(columns_to_join, [])
            return instances

        def fake_unrescue(context, instance):
            unrescued_instances[instance['uuid']] = True

        self.stubs.Set(self.compute.conductor_api,
                       'instance_get_all_by_filters',
                       fake_instance_get_all_by_filters)
        self.stubs.Set(self.compute.conductor_api, 'compute_unrescue',
                       fake_unrescue)

        self.flags(rescue_timeout=60)
        ctxt = context.get_admin_context()

        self.compute._poll_rescued_instances(ctxt)

        for instance in unrescued_instances.values():
            self.assertTrue(instance)

    def test_poll_unconfirmed_resizes(self):
        instances = [{'uuid': 'fake_uuid1', 'vm_state': vm_states.RESIZED,
                      'task_state': None},
                     {'uuid': 'noexist'},
                     {'uuid': 'fake_uuid2', 'vm_state': vm_states.ERROR,
                      'task_state': None},
                     {'uuid': 'fake_uuid3', 'vm_state': vm_states.ACTIVE,
                      'task_state': task_states.REBOOTING},
                     {'uuid': 'fake_uuid4', 'vm_state': vm_states.RESIZED,
                      'task_state': None},
                     {'uuid': 'fake_uuid5', 'vm_state': vm_states.ACTIVE,
                      'task_state': None},
                     {'uuid': 'fake_uuid6', 'vm_state': vm_states.RESIZED,
                      'task_state': 'deleting'}]
        expected_migration_status = {'fake_uuid1': 'confirmed',
                                     'noexist': 'error',
                                     'fake_uuid2': 'error',
                                     'fake_uuid3': 'error',
                                     'fake_uuid4': None,
                                     'fake_uuid5': 'error',
                                     'fake_uuid6': 'error'}
        migrations = []
        for i, instance in enumerate(instances, start=1):
            migrations.append({'id': i,
                               'instance_uuid': instance['uuid'],
                               'status': None})

        def fake_instance_get_by_uuid(context, instance_uuid,
                cols_to_join=None):
            # raise InstanceNotFound exception for uuid 'noexist'
            if instance_uuid == 'noexist':
                raise exception.InstanceNotFound(instance_id=instance_uuid)
            for instance in instances:
                if instance['uuid'] == instance_uuid:
                    return instance

        def fake_migration_get_unconfirmed_by_dest_compute(context,
                resize_confirm_window, dest_compute):
            self.assertEqual(dest_compute, CONF.host)
            return migrations

        def fake_migration_update(context, m, status):
            for migration in migrations:
                if migration['id'] == m['id']:
                    migration['status'] = status

        def fake_confirm_resize(context, instance, migration_ref=None):
            # raise exception for 'fake_uuid4' to check migration status
            # does not get set to 'error' on confirm_resize failure.
            if instance['uuid'] == 'fake_uuid4':
                raise test.TestingException
            self.assertNotEqual(migration_ref, None)
            for migration in migrations:
                if (migration['instance_uuid'] ==
                    migration_ref['instance_uuid']):
                    migration['status'] = 'confirmed'

        self.stubs.Set(db, 'instance_get_by_uuid',
                fake_instance_get_by_uuid)
        self.stubs.Set(db, 'migration_get_unconfirmed_by_dest_compute',
                fake_migration_get_unconfirmed_by_dest_compute)
        self.stubs.Set(self.compute.conductor_api, 'migration_update',
                fake_migration_update)
        self.stubs.Set(self.compute.conductor_api, 'compute_confirm_resize',
                fake_confirm_resize)

        def fetch_instance_migration_status(instance_uuid):
            for migration in migrations:
                if migration['instance_uuid'] == instance_uuid:
                    return migration['status']

        self.flags(resize_confirm_window=60)
        ctxt = context.get_admin_context()

        self.compute._poll_unconfirmed_resizes(ctxt)

        for uuid, status in expected_migration_status.iteritems():
            self.assertEqual(status, fetch_instance_migration_status(uuid))

    def test_instance_build_timeout_disabled(self):
        self.flags(instance_build_timeout=0)
        ctxt = context.get_admin_context()
        called = {'get_all': False, 'set_error_state': 0}
        created_at = timeutils.utcnow() + datetime.timedelta(seconds=-60)

        def fake_instance_get_all_by_filters(context, filters, *args, **kw):
            called['get_all'] = True
            self.assertIn('host', filters)
            self.assertEqual(kw['columns_to_join'], [])
            return instances[:]

        self.stubs.Set(db, 'instance_get_all_by_filters',
                fake_instance_get_all_by_filters)

        def fake_set_instance_error_state(_ctxt, instance_uuid, **kwargs):
            called['set_error_state'] += 1

        self.stubs.Set(self.compute, '_set_instance_error_state',
                fake_set_instance_error_state)

        instance_map = {}
        instances = []
        for x in xrange(5):
            uuid = 'fake-uuid-%s' % x
            instance_map[uuid] = {'uuid': uuid, 'host': CONF.host,
                    'vm_state': vm_states.BUILDING,
                    'created_at': created_at}
            instances.append(instance_map[uuid])

        self.compute._check_instance_build_time(ctxt)
        self.assertFalse(called['get_all'])
        self.assertEqual(called['set_error_state'], 0)

    def test_instance_build_timeout(self):
        self.flags(instance_build_timeout=30)
        ctxt = context.get_admin_context()
        called = {'get_all': False, 'set_error_state': 0}
        created_at = timeutils.utcnow() + datetime.timedelta(seconds=-60)

        def fake_instance_get_all_by_filters(*args, **kwargs):
            called['get_all'] = True
            return instances[:]

        self.stubs.Set(db, 'instance_get_all_by_filters',
                fake_instance_get_all_by_filters)

        def fake_set_instance_error_state(_ctxt, instance_uuid, **kwargs):
            called['set_error_state'] += 1

        self.stubs.Set(self.compute, '_set_instance_error_state',
                fake_set_instance_error_state)

        instance_map = {}
        instances = []
        for x in xrange(5):
            uuid = 'fake-uuid-%s' % x
            instance_map[uuid] = {'uuid': uuid, 'host': CONF.host,
                    'vm_state': vm_states.BUILDING,
                    'created_at': created_at}
            instances.append(instance_map[uuid])

        self.compute._check_instance_build_time(ctxt)
        self.assertTrue(called['get_all'])
        self.assertEqual(called['set_error_state'], 5)

    def test_instance_build_timeout_mixed_instances(self):
        self.flags(instance_build_timeout=30)
        ctxt = context.get_admin_context()
        called = {'get_all': False, 'set_error_state': 0}
        created_at = timeutils.utcnow() + datetime.timedelta(seconds=-60)

        def fake_instance_get_all_by_filters(*args, **kwargs):
            called['get_all'] = True
            return instances[:]

        self.stubs.Set(db, 'instance_get_all_by_filters',
                fake_instance_get_all_by_filters)

        def fake_set_instance_error_state(_ctxt, instance_uuid, **kwargs):
            called['set_error_state'] += 1

        self.stubs.Set(self.compute, '_set_instance_error_state',
                fake_set_instance_error_state)

        instance_map = {}
        instances = []
        #expired instances
        for x in xrange(4):
            uuid = 'fake-uuid-%s' % x
            instance_map[uuid] = {'uuid': uuid, 'host': CONF.host,
                    'vm_state': vm_states.BUILDING,
                    'created_at': created_at}
            instances.append(instance_map[uuid])

        #not expired
        uuid = 'fake-uuid-5'
        instance_map[uuid] = {
            'uuid': uuid,
            'host': CONF.host,
            'vm_state': vm_states.BUILDING,
            'created_at': timeutils.utcnow(),
        }
        instances.append(instance_map[uuid])

        self.compute._check_instance_build_time(ctxt)
        self.assertTrue(called['get_all'])
        self.assertEqual(called['set_error_state'], 4)

    def test_get_resource_tracker_fail(self):
        self.assertRaises(exception.NovaException,
                          self.compute._get_resource_tracker,
                          'invalidnodename')

    def test_instance_update_host_check(self):
        # make sure rt usage doesn't happen if the host or node is different
        def fail_get(nodename):
            raise test.TestingException(_("wrong host/node"))
        self.stubs.Set(self.compute, '_get_resource_tracker', fail_get)

        instance = self._create_fake_instance({'host': 'someotherhost'})
        self.compute._instance_update(self.context, instance['uuid'])

        instance = self._create_fake_instance({'node': 'someothernode'})
        self.compute._instance_update(self.context, instance['uuid'])

        params = {'host': 'someotherhost', 'node': 'someothernode'}
        instance = self._create_fake_instance(params)
        self.compute._instance_update(self.context, instance['uuid'])

    def test_destroy_evacuated_instance_on_shared_storage(self):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host}))
        ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance({'host': 'otherhost'})

        instances.append(evacuated_instance)

        self.mox.StubOutWithMock(self.compute,
                                 '_get_instances_on_driver')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_nw_info')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_volume_block_device_info')
        self.mox.StubOutWithMock(self.compute,
                                 '_is_instance_storage_shared')
        self.mox.StubOutWithMock(self.compute, '_legacy_nw_info')
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')

        self.compute._get_instances_on_driver(
                fake_context, {'deleted': False}).AndReturn(instances)
        self.compute._get_instance_nw_info(fake_context,
                                           evacuated_instance).AndReturn(
                                                   'fake_network_info')
        self.compute._get_instance_volume_block_device_info(
                fake_context, evacuated_instance).AndReturn('fake_bdi')
        self.compute._is_instance_storage_shared(fake_context,
                        evacuated_instance).AndReturn(True)
        self.compute._legacy_nw_info('fake_network_info').AndReturn(
                'fake_legacy_network_info')
        self.compute.driver.destroy(evacuated_instance,
                                    'fake_legacy_network_info',
                                    'fake_bdi',
                                    False)

        self.mox.ReplayAll()
        self.compute._destroy_evacuated_instances(fake_context)

    def test_destroy_evacuated_instance_with_disks(self):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host}))
        ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance({'host': 'otherhost'})

        instances.append(evacuated_instance)

        self.mox.StubOutWithMock(self.compute,
                                 '_get_instances_on_driver')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_nw_info')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_volume_block_device_info')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_instance_shared_storage_local')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'check_instance_shared_storage')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_instance_shared_storage_cleanup')
        self.mox.StubOutWithMock(self.compute, '_legacy_nw_info')
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')

        self.compute._get_instances_on_driver(
                fake_context, {'deleted': False}).AndReturn(instances)
        self.compute._get_instance_nw_info(fake_context,
                                           evacuated_instance).AndReturn(
                                                   'fake_network_info')
        self.compute._get_instance_volume_block_device_info(
                fake_context, evacuated_instance).AndReturn('fake_bdi')
        self.compute.driver.check_instance_shared_storage_local(fake_context,
                evacuated_instance).AndReturn({'filename': 'tmpfilename'})
        self.compute.compute_rpcapi.check_instance_shared_storage(fake_context,
                evacuated_instance,
                {'filename': 'tmpfilename'}).AndReturn(False)
        self.compute.driver.check_instance_shared_storage_cleanup(fake_context,
                {'filename': 'tmpfilename'})
        self.compute._legacy_nw_info('fake_network_info').AndReturn(
                'fake_legacy_network_info')
        self.compute.driver.destroy(evacuated_instance,
                                    'fake_legacy_network_info',
                                    'fake_bdi',
                                    True)

        self.mox.ReplayAll()
        self.compute._destroy_evacuated_instances(fake_context)

    def test_destroy_evacuated_instance_not_implemented(self):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host})),
            jsonutils.to_primitive(self._create_fake_instance(
                                                {'host': self.compute.host}))
        ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance({'host': 'otherhost'})

        instances.append(evacuated_instance)

        self.mox.StubOutWithMock(self.compute,
                                 '_get_instances_on_driver')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_nw_info')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_volume_block_device_info')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_instance_shared_storage_local')
        self.mox.StubOutWithMock(self.compute.compute_rpcapi,
                                 'check_instance_shared_storage')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'check_instance_shared_storage_cleanup')
        self.mox.StubOutWithMock(self.compute, '_legacy_nw_info')
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')

        self.compute._get_instances_on_driver(
                fake_context, {'deleted': False}).AndReturn(instances)
        self.compute._get_instance_nw_info(fake_context,
                                           evacuated_instance).AndReturn(
                                                   'fake_network_info')
        self.compute._get_instance_volume_block_device_info(
                fake_context, evacuated_instance).AndReturn('fake_bdi')
        self.compute.driver.check_instance_shared_storage_local(fake_context,
                evacuated_instance).AndRaise(NotImplementedError())
        self.compute._legacy_nw_info('fake_network_info').AndReturn(
                'fake_legacy_network_info')
        self.compute.driver.destroy(evacuated_instance,
                                    'fake_legacy_network_info',
                                    'fake_bdi',
                                    True)

        self.mox.ReplayAll()
        self.compute._destroy_evacuated_instances(fake_context)

    def test_init_host(self):
        our_host = self.compute.host
        fake_context = 'fake-context'
        inst = dict(fakes.stub_instance(1),
                    deleted_at=None, created_at=None, updated_at=None,
                    deleted=0, info_cache={'instance_uuid': 'fake-uuid',
                                           'network_info': None})
        startup_instances = [inst, inst, inst]

        def _do_mock_calls(defer_iptables_apply):
            self.compute.driver.init_host(host=our_host)
            context.get_admin_context().AndReturn(fake_context)
            db.instance_get_all_by_host(
                    fake_context, our_host, columns_to_join=['info_cache']
                    ).AndReturn(startup_instances)
            if defer_iptables_apply:
                self.compute.driver.filter_defer_apply_on()
            self.compute._destroy_evacuated_instances(fake_context)
            self.compute._init_instance(fake_context,
                                        mox.IsA(instance_obj.Instance))
            self.compute._init_instance(fake_context,
                                        mox.IsA(instance_obj.Instance))
            self.compute._init_instance(fake_context,
                                        mox.IsA(instance_obj.Instance))
            if defer_iptables_apply:
                self.compute.driver.filter_defer_apply_off()
            self.compute._report_driver_status(fake_context)
            self.compute.publish_service_capabilities(fake_context)

        self.mox.StubOutWithMock(self.compute.driver, 'init_host')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'filter_defer_apply_on')
        self.mox.StubOutWithMock(self.compute.driver,
                'filter_defer_apply_off')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')
        self.mox.StubOutWithMock(context, 'get_admin_context')
        self.mox.StubOutWithMock(self.compute,
                '_destroy_evacuated_instances')
        self.mox.StubOutWithMock(self.compute,
                '_init_instance')
        self.mox.StubOutWithMock(self.compute,
                '_report_driver_status')
        self.mox.StubOutWithMock(self.compute,
                'publish_service_capabilities')

        # Test with defer_iptables_apply
        self.flags(defer_iptables_apply=True)
        _do_mock_calls(True)

        self.mox.ReplayAll()
        self.compute.init_host()
        self.mox.VerifyAll()

        # Test without defer_iptables_apply
        self.mox.ResetAll()
        self.flags(defer_iptables_apply=False)
        _do_mock_calls(False)

        self.mox.ReplayAll()
        self.compute.init_host()
        # tearDown() uses context.get_admin_context(), so we have
        # to do the verification here and unstub it.
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def test_init_host_with_deleted_migration(self):
        our_host = self.compute.host
        not_our_host = 'not-' + our_host
        fake_context = 'fake-context'

        deleted_instance = {
            'name': 'fake-name',
            'host': not_our_host,
            'uuid': 'fake-uuid',
            }

        self.mox.StubOutWithMock(self.compute.driver, 'init_host')
        self.mox.StubOutWithMock(self.compute.driver, 'destroy')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')
        self.mox.StubOutWithMock(context, 'get_admin_context')
        self.mox.StubOutWithMock(self.compute, 'init_virt_events')
        self.mox.StubOutWithMock(self.compute, '_get_instances_on_driver')
        self.mox.StubOutWithMock(self.compute, '_init_instance')
        self.mox.StubOutWithMock(self.compute, '_report_driver_status')
        self.mox.StubOutWithMock(self.compute, 'publish_service_capabilities')
        self.mox.StubOutWithMock(self.compute, '_get_instance_nw_info')

        self.compute.driver.init_host(host=our_host)
        context.get_admin_context().AndReturn(fake_context)
        db.instance_get_all_by_host(fake_context, our_host,
                                    columns_to_join=['info_cache']
                                    ).AndReturn([])
        self.compute.init_virt_events()

        # simulate failed instance
        self.compute._get_instances_on_driver(
            fake_context, {'deleted': False}).AndReturn([deleted_instance])
        self.compute._get_instance_nw_info(fake_context, deleted_instance
            ).AndRaise(exception.InstanceNotFound(
                instance_id=deleted_instance['uuid']))
        # ensure driver.destroy is called so that driver may
        # clean up any dangling files
        self.compute.driver.destroy(deleted_instance,
            mox.IgnoreArg(), mox.IgnoreArg(), mox.IgnoreArg())

        self.compute._report_driver_status(fake_context)
        self.compute.publish_service_capabilities(fake_context)

        self.mox.ReplayAll()
        self.compute.init_host()
        # tearDown() uses context.get_admin_context(), so we have
        # to do the verification here and unstub it.
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def test_init_instance_failed_resume_sets_error(self):
        instance = {
            'uuid': 'fake-uuid',
            'info_cache': None,
            'power_state': power_state.RUNNING,
            'vm_state': vm_states.ACTIVE,
            'task_state': None,
        }
        self.flags(resume_guests_state_on_host_boot=True)
        self.mox.StubOutWithMock(self.compute, '_get_power_state')
        self.mox.StubOutWithMock(self.compute.driver, 'plug_vifs')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'resume_state_on_host_boot')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_volume_block_device_info')
        self.mox.StubOutWithMock(self.compute,
                                 '_set_instance_error_state')
        self.compute._get_power_state(mox.IgnoreArg(),
                instance).AndReturn(power_state.SHUTDOWN)
        self.compute.driver.plug_vifs(instance, mox.IgnoreArg())
        self.compute._get_instance_volume_block_device_info(mox.IgnoreArg(),
                instance).AndReturn('fake-bdm')
        self.compute.driver.resume_state_on_host_boot(mox.IgnoreArg(),
                instance, mox.IgnoreArg(),
                'fake-bdm').AndRaise(test.TestingException)
        self.compute._set_instance_error_state(mox.IgnoreArg(),
                instance['uuid'])
        self.mox.ReplayAll()
        self.compute._init_instance('fake-context', instance)

    def _test_init_instance_reverts_crashed_migrations(self,
                                                       old_vm_state=None):
        power_on = True if (not old_vm_state or
                            old_vm_state == vm_states.ACTIVE) else False
        sys_meta = {
            'old_vm_state': old_vm_state
            }
        instance = {
            'uuid': 'foo',
            'vm_state': vm_states.ERROR,
            'task_state': task_states.RESIZE_MIGRATING,
            'power_state': power_state.SHUTDOWN,
            'system_metadata': sys_meta
            }
        fixed = dict(instance, task_state=None)
        self.mox.StubOutWithMock(compute_utils, 'get_nw_info_for_instance')
        self.mox.StubOutWithMock(utils, 'instance_sys_meta')
        self.mox.StubOutWithMock(self.compute.driver, 'plug_vifs')
        self.mox.StubOutWithMock(self.compute.driver,
                                 'finish_revert_migration')
        self.mox.StubOutWithMock(self.compute,
                                 '_get_instance_volume_block_device_info')
        self.mox.StubOutWithMock(self.compute.driver, 'get_info')
        self.mox.StubOutWithMock(self.compute, '_instance_update')

        compute_utils.get_nw_info_for_instance(instance).AndReturn(
            network_model.NetworkInfo())
        self.compute.driver.plug_vifs(instance, [])
        utils.instance_sys_meta(instance).AndReturn(sys_meta)
        self.compute._get_instance_volume_block_device_info(
            self.context, instance).AndReturn([])
        self.compute.driver.finish_revert_migration(instance, [], [], power_on)
        self.compute._instance_update(self.context, instance['uuid'],
                                      task_state=None).AndReturn(fixed)
        self.compute.driver.get_info(fixed).AndReturn(
            {'state': power_state.SHUTDOWN})

        self.mox.ReplayAll()

        self.compute._init_instance(self.context, instance)

    def test_init_instance_reverts_crashed_migration_from_active(self):
        self._test_init_instance_reverts_crashed_migrations(
                                                old_vm_state=vm_states.ACTIVE)

    def test_init_instance_reverts_crashed_migration_from_stopped(self):
        self._test_init_instance_reverts_crashed_migrations(
                                                old_vm_state=vm_states.STOPPED)

    def test_init_instance_reverts_crashed_migration_no_old_state(self):
        self._test_init_instance_reverts_crashed_migrations(old_vm_state=None)

    def _test_init_instance_update_nw_info_cache_helper(self, legacy_nwinfo):
        self.compute.driver.legacy_nwinfo = lambda *a, **k: legacy_nwinfo

        cached_nw_info = fake_network_cache_model.new_vif()
        cached_nw_info = network_model.NetworkInfo([cached_nw_info])
        old_cached_nw_info = copy.deepcopy(cached_nw_info)

        # Folsom has no 'type' in network cache info.
        del old_cached_nw_info[0]['type']
        fake_info_cache = {'network_info': old_cached_nw_info.json()}
        instance = {
            'uuid': 'a-foo-uuid',
            'vm_state': vm_states.ACTIVE,
            'task_state': None,
            'power_state': power_state.RUNNING,
            'info_cache': fake_info_cache,
            }

        self.mox.StubOutWithMock(self.compute, '_get_power_state')
        self.compute._get_power_state(mox.IgnoreArg(),
                instance).AndReturn(power_state.RUNNING)

        if legacy_nwinfo:
            self.mox.StubOutWithMock(self.compute, '_get_instance_nw_info')
            # Call network API to get instance network info, and force
            # an update to instance's info_cache.
            self.compute._get_instance_nw_info(self.context,
                instance).AndReturn(cached_nw_info)

            self.mox.StubOutWithMock(self.compute.driver, 'plug_vifs')
            self.compute.driver.plug_vifs(instance, cached_nw_info.legacy())
        else:
            self.mox.StubOutWithMock(self.compute.driver, 'plug_vifs')
            self.compute.driver.plug_vifs(instance, cached_nw_info)

        self.mox.ReplayAll()

        self.compute._init_instance(self.context, instance)

    def test_init_instance_update_nw_info_cache_legacy(self):
        """network_info in legacy is form [(network_dict, info_dict)]."""
        self._test_init_instance_update_nw_info_cache_helper(True)

    def test_init_instance_update_nw_info_cache(self):
        """network_info is NetworkInfo list-like object."""
        self._test_init_instance_update_nw_info_cache_helper(False)

    def test_get_instances_on_driver(self):
        fake_context = context.get_admin_context()

        driver_instances = []
        for x in xrange(10):
            instance = dict(uuid=uuidutils.generate_uuid())
            driver_instances.append(instance)

        self.mox.StubOutWithMock(self.compute.driver,
                'list_instance_uuids')
        self.mox.StubOutWithMock(self.compute.conductor_api,
                'instance_get_all_by_filters')

        self.compute.driver.list_instance_uuids().AndReturn(
                [inst['uuid'] for inst in driver_instances])
        self.compute.conductor_api.instance_get_all_by_filters(
                fake_context,
                {'uuid': [inst['uuid'] for
                          inst in driver_instances]},
                columns_to_join=[]).AndReturn(
                        driver_instances)

        self.mox.ReplayAll()

        result = self.compute._get_instances_on_driver(fake_context)
        self.assertEqual(driver_instances, result)

    def test_get_instances_on_driver_fallback(self):
        # Test getting instances when driver doesn't support
        # 'list_instance_uuids'
        self.compute.host = 'host'
        filters = {'host': self.compute.host}
        fake_context = context.get_admin_context()

        all_instances = []
        driver_instances = []
        for x in xrange(10):
            instance = dict(name=uuidutils.generate_uuid())
            if x % 2:
                driver_instances.append(instance)
            all_instances.append(instance)

        self.mox.StubOutWithMock(self.compute.driver,
                'list_instance_uuids')
        self.mox.StubOutWithMock(self.compute.driver,
                'list_instances')
        self.mox.StubOutWithMock(self.compute.conductor_api,
                'instance_get_all_by_filters')

        self.compute.driver.list_instance_uuids().AndRaise(
                NotImplementedError())
        self.compute.driver.list_instances().AndReturn(
                [inst['name'] for inst in driver_instances])
        self.compute.conductor_api.instance_get_all_by_filters(
                fake_context, filters,
                columns_to_join=[]).AndReturn(all_instances)

        self.mox.ReplayAll()

        result = self.compute._get_instances_on_driver(fake_context, filters)
        self.assertEqual(driver_instances, result)

    def test_instance_usage_audit(self):
        instances = [{'uuid': 'foo'}]
        self.flags(instance_usage_audit=True)
        self.stubs.Set(compute_utils, 'has_audit_been_run',
                       lambda *a, **k: False)
        self.stubs.Set(self.compute.conductor_api,
                       'instance_get_active_by_window_joined',
                       lambda *a, **k: instances)
        self.stubs.Set(compute_utils, 'start_instance_usage_audit',
                       lambda *a, **k: None)
        self.stubs.Set(compute_utils, 'finish_instance_usage_audit',
                       lambda *a, **k: None)

        self.mox.StubOutWithMock(self.compute.conductor_api,
                                 'notify_usage_exists')
        self.compute.conductor_api.notify_usage_exists(
            self.context, instances[0], ignore_missing_network_data=False)
        self.mox.ReplayAll()
        self.compute._instance_usage_audit(self.context)

    def test_add_remove_fixed_ip_updates_instance_updated_at(self):
        def _noop(*args, **kwargs):
            pass

        self.stubs.Set(self.compute.network_api,
                       'add_fixed_ip_to_instance', _noop)
        self.stubs.Set(self.compute.network_api,
                       'remove_fixed_ip_from_instance', _noop)

        instance = self._create_fake_instance()
        updated_at_1 = instance['updated_at']

        self.compute.add_fixed_ip_to_instance(self.context, 'fake', instance)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        updated_at_2 = instance['updated_at']

        self.compute.remove_fixed_ip_from_instance(self.context, 'fake',
                                                   instance)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        updated_at_3 = instance['updated_at']

        updated_ats = (updated_at_1, updated_at_2, updated_at_3)
        self.assertEqual(len(updated_ats), len(set(updated_ats)))

    def test_reclaim_queued_deletes(self):
        self.flags(reclaim_instance_interval=3600)
        ctxt = context.get_admin_context()

        # Active
        self._create_fake_instance(params={'host': CONF.host})

        # Deleted not old enough
        self._create_fake_instance(params={'host': CONF.host,
                                           'vm_state': vm_states.SOFT_DELETED,
                                           'deleted_at': timeutils.utcnow()})

        # Deleted old enough (only this one should be reclaimed)
        deleted_at = (timeutils.utcnow() -
                      datetime.timedelta(hours=1, minutes=5))
        instance = self._create_fake_instance(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'deleted_at': deleted_at})

        # Restoring
        # NOTE(hanlind): This specifically tests for a race condition
        # where restoring a previously soft deleted instance sets
        # deleted_at back to None, causing reclaim to think it can be
        # deleted, see LP #1186243.
        self._create_fake_instance(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'task_state': task_states.RESTORING})

        self.mox.StubOutWithMock(self.compute, '_delete_instance')
        instance_ref = get_primitive_instance_by_uuid(ctxt, instance['uuid'])
        self.compute._delete_instance(ctxt, instance_ref, [])

        self.mox.ReplayAll()

        self.compute._reclaim_queued_deletes(ctxt)

    def test_sync_power_states(self):
        ctxt = self.context.elevated()
        self._create_fake_instance({'host': self.compute.host})
        self._create_fake_instance({'host': self.compute.host})
        self.mox.StubOutWithMock(self.compute.driver, 'get_info')
        self.mox.StubOutWithMock(self.compute, '_sync_instance_power_state')
        self.compute.driver.get_info(mox.IgnoreArg()).AndReturn(
            {'state': power_state.RUNNING})
        self.compute._sync_instance_power_state(ctxt, mox.IgnoreArg(),
                                                power_state.RUNNING)
        self.compute.driver.get_info(mox.IgnoreArg()).AndReturn(
            {'state': power_state.SHUTDOWN})
        self.compute._sync_instance_power_state(ctxt, mox.IgnoreArg(),
                                                power_state.SHUTDOWN)
        self.mox.ReplayAll()
        self.compute._sync_power_states(ctxt)

    def _get_sync_instance(self, power_state, vm_state, task_state=None):
        instance = instance_obj.Instance()
        instance.uuid = 'fake-uuid'
        instance.power_state = power_state
        instance.vm_state = vm_state
        instance.host = self.compute.host
        instance.task_state = task_state
        self.mox.StubOutWithMock(instance, 'refresh')
        self.mox.StubOutWithMock(instance, 'save')
        return instance

    def test_sync_instance_power_state_match(self):
        instance = self._get_sync_instance(power_state.RUNNING,
                                           vm_states.ACTIVE)
        instance.refresh()
        self.mox.ReplayAll()
        self.compute._sync_instance_power_state(self.context, instance,
                                                power_state.RUNNING)

    def test_sync_instance_power_state_running_stopped(self):
        instance = self._get_sync_instance(power_state.RUNNING,
                                           vm_states.ACTIVE)
        instance.refresh()
        instance.save()
        self.mox.ReplayAll()
        self.compute._sync_instance_power_state(self.context, instance,
                                                power_state.SHUTDOWN)
        self.assertEqual(instance.power_state, power_state.SHUTDOWN)

    def _test_sync_to_stop(self, power_state, vm_state, driver_power_state,
                           stop=True):
        instance = self._get_sync_instance(power_state, vm_state)
        instance.refresh()
        instance.save()
        self.mox.StubOutWithMock(self.compute.conductor_api, 'compute_stop')
        if stop:
            self.compute.conductor_api.compute_stop(self.context, instance)
        self.mox.ReplayAll()
        self.compute._sync_instance_power_state(self.context, instance,
                                                driver_power_state)
        self.mox.VerifyAll()
        self.mox.UnsetStubs()

    def test_sync_instance_power_state_to_stop(self):
        for ps in (power_state.SHUTDOWN, power_state.CRASHED,
                   power_state.SUSPENDED):
            self._test_sync_to_stop(power_state.RUNNING, vm_states.ACTIVE, ps)
        self._test_sync_to_stop(power_state.SHUTDOWN, vm_states.STOPPED,
                                power_state.RUNNING)

    def test_sync_instance_power_state_to_no_stop(self):
        for ps in (power_state.PAUSED, power_state.NOSTATE):
            self._test_sync_to_stop(power_state.RUNNING, vm_states.ACTIVE, ps,
                                    stop=False)
        for vs in (vm_states.SOFT_DELETED, vm_states.DELETED):
            for ps in (power_state.NOSTATE, power_state.SHUTDOWN):
                self._test_sync_to_stop(power_state.RUNNING, vs, ps,
                                        stop=False)


class ComputeAPITestCase(BaseTestCase):

    def setUp(self):
        def fake_get_nw_info(cls, ctxt, instance):
            self.assertTrue(ctxt.is_admin)
            return fake_network.fake_get_instance_nw_info(self.stubs, 1, 1,
                                                          spectacular=True)

        super(ComputeAPITestCase, self).setUp()
        self.stubs.Set(network_api.API, 'get_instance_nw_info',
                       fake_get_nw_info)
        self.security_group_api = (
            openstack_driver.get_openstack_security_group_driver())

        self.compute_api = compute.API(
                                   security_group_api=self.security_group_api)
        self.fake_image = {
            'id': 1,
            'name': 'fake_name',
            'status': 'active',
            'properties': {'kernel_id': 'fake_kernel_id',
                           'ramdisk_id': 'fake_ramdisk_id'},
        }

        def fake_show(obj, context, image_id):
            if image_id:
                return self.fake_image
            else:
                raise exception.ImageNotFound(image_id=image_id)

        self.fake_show = fake_show

    def _run_instance(self, params=None):
        instance = jsonutils.to_primitive(self._create_fake_instance(params))
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)
        return instance, instance_uuid

    def test_create_with_too_little_ram(self):
        # Test an instance type with too little memory.

        inst_type = flavors.get_default_flavor()
        inst_type['memory_mb'] = 1

        self.fake_image['min_ram'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeMemoryTooSmall,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Now increase the inst_type memory and make sure all is fine.
        inst_type['memory_mb'] = 2
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_create_with_too_little_disk(self):
        # Test an instance type with too little disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1

        self.fake_image['min_disk'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Now increase the inst_type disk space and make sure all is fine.
        inst_type['root_gb'] = 2
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_create_with_too_large_image(self):
        # Test an instance type with too little disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1

        self.fake_image['size'] = '1073741825'

        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Reduce image to 1 GB limit and ensure it works
        self.fake_image['size'] = '1073741824'
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_create_just_enough_ram_and_disk(self):
        # Test an instance type with just enough ram and disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 2
        inst_type['memory_mb'] = 2

        self.fake_image['min_ram'] = 2
        self.fake_image['min_disk'] = 2
        self.fake_image['name'] = 'fake_name'
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_create_with_no_ram_and_disk_reqs(self):
        # Test an instance type with no min_ram or min_disk.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1
        inst_type['memory_mb'] = 1

        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_create_with_deleted_image(self):
        # If we're given a deleted image by glance, we should not be able to
        # build from it
        inst_type = flavors.get_default_flavor()

        self.fake_image['name'] = 'fake_name'
        self.fake_image['status'] = 'DELETED'
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        expected_message = (
            exception.ImageNotActive.message % {'image_id':
            self.fake_image['id']})
        with testtools.ExpectedException(exception.ImageNotActive,
                                         expected_message):
            self.compute_api.create(self.context, inst_type,
                                    self.fake_image['id'])

    def test_create_instance_defaults_display_name(self):
        # Verify that an instance cannot be created without a display_name.
        cases = [dict(), dict(display_name=None)]
        for instance in cases:
            (ref, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                'fake-image-uuid', **instance)
            try:
                self.assertNotEqual(ref[0]['display_name'], None)
            finally:
                db.instance_destroy(self.context, ref[0]['uuid'])

    def test_create_instance_sets_system_metadata(self):
        # Make sure image properties are copied into system metadata.
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href='fake-image-uuid')
        try:
            sys_metadata = db.instance_system_metadata_get(self.context,
                    ref[0]['uuid'])

            image_props = {'image_kernel_id': 'fake_kernel_id',
                     'image_ramdisk_id': 'fake_ramdisk_id',
                     'image_something_else': 'meow', }
            for key, value in image_props.iteritems():
                self.assertTrue(key in sys_metadata)
                self.assertEqual(value, sys_metadata[key])

        finally:
            db.instance_destroy(self.context, ref[0]['uuid'])

    def test_create_saves_type_in_system_metadata(self):
        instance_type = flavors.get_default_flavor()
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=instance_type,
                image_href=None)
        try:
            sys_metadata = db.instance_system_metadata_get(self.context,
                    ref[0]['uuid'])

            instance_type_props = ['name', 'memory_mb', 'vcpus', 'root_gb',
                                   'ephemeral_gb', 'flavorid', 'swap',
                                   'rxtx_factor', 'vcpu_weight']
            for key in instance_type_props:
                sys_meta_key = "instance_type_%s" % key
                self.assertTrue(sys_meta_key in sys_metadata)
                self.assertEqual(str(instance_type[key]),
                                 str(sys_metadata[sys_meta_key]))

        finally:
            db.instance_destroy(self.context, ref[0]['uuid'])

    def test_create_instance_associates_security_groups(self):
        # Make sure create associates security groups.
        group = self._create_group()
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=None,
                security_group=['testgroup'])
        try:
            self.assertEqual(len(db.security_group_get_by_instance(
                             self.context, ref[0]['uuid'])), 1)
            group = db.security_group_get(self.context, group['id'])
            self.assert_(len(group['instances']) == 1)
        finally:
            db.security_group_destroy(self.context, group['id'])
            db.instance_destroy(self.context, ref[0]['uuid'])

    def test_create_instance_with_invalid_security_group_raises(self):
        instance_type = flavors.get_default_flavor()

        pre_build_len = len(db.instance_get_all(self.context))
        self.assertRaises(exception.SecurityGroupNotFoundForProject,
                          self.compute_api.create,
                          self.context,
                          instance_type=instance_type,
                          image_href=None,
                          security_group=['this_is_a_fake_sec_group'])
        self.assertEqual(pre_build_len,
                         len(db.instance_get_all(self.context)))

    def test_create_with_large_user_data(self):
        # Test an instance type with too much user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceUserDataTooLarge,
            self.compute_api.create, self.context, inst_type,
            self.fake_image['id'], user_data=('1' * 65536))

    def test_create_with_malformed_user_data(self):
        # Test an instance type with malformed user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceUserDataMalformed,
            self.compute_api.create, self.context, inst_type,
            self.fake_image['id'], user_data='banana')

    def test_create_with_base64_user_data(self):
        # Test an instance type with ok much user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        # NOTE(mikal): a string of length 48510 encodes to 65532 characters of
        # base64
        (refs, resv_id) = self.compute_api.create(
            self.context, inst_type, self.fake_image['id'],
            user_data=base64.encodestring('1' * 48510))
        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_default_hostname_generator(self):
        fake_uuids = [str(uuid.uuid4()) for x in xrange(4)]

        orig_populate = self.compute_api._populate_instance_for_create

        def _fake_populate(base_options, *args, **kwargs):
            base_options['uuid'] = fake_uuids.pop(0)
            return orig_populate(base_options, *args, **kwargs)

        self.stubs.Set(self.compute_api,
                '_populate_instance_for_create',
                _fake_populate)

        cases = [(None, 'server-%s' % fake_uuids[0]),
                 ('Hello, Server!', 'hello-server'),
                 ('<}\x1fh\x10e\x08l\x02l\x05o\x12!{>', 'hello'),
                 ('hello_server', 'hello-server')]
        for display_name, hostname in cases:
            (ref, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None,
                display_name=display_name)
            try:
                self.assertEqual(ref[0]['hostname'], hostname)
            finally:
                db.instance_destroy(self.context, ref[0]['uuid'])

    def test_destroy_instance_disassociates_security_groups(self):
        # Make sure destroying disassociates security groups.
        group = self._create_group()

        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=None,
                security_group=['testgroup'])
        try:
            db.instance_destroy(self.context, ref[0]['uuid'])
            group = db.security_group_get(self.context, group['id'])
            self.assert_(len(group['instances']) == 0)
        finally:
            db.security_group_destroy(self.context, group['id'])

    def test_destroy_security_group_disassociates_instances(self):
        # Make sure destroying security groups disassociates instances.
        group = self._create_group()

        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=None,
                security_group=['testgroup'])

        try:
            db.security_group_destroy(self.context, group['id'])
            admin_deleted_context = context.get_admin_context(
                    read_deleted="only")
            group = db.security_group_get(admin_deleted_context, group['id'])
            self.assert_(len(group['instances']) == 0)
        finally:
            db.instance_destroy(self.context, ref[0]['uuid'])

    def test_start(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.STOPPED

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        self.mox.StubOutWithMock(
                self.compute_api.compute_rpcapi,
                'start_instance')

        instance.save(expected_task_state=None)
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.START)
        self.compute_api.compute_rpcapi.start_instance(
                self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.start(self.context, instance)
        self.assertEqual(task_states.POWERING_ON,
                         instance.task_state)

    def test_start_invalid_state(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.ACTIVE
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.start,
                          self.context, instance)

    def test_start_no_host(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.STOPPED
        instance.host = ''
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.start,
                          self.context, instance)

    def test_stop(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.task_state = None
        # Make sure this gets reset
        instance.progress = 99

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        self.mox.StubOutWithMock(
                self.compute_api.compute_rpcapi,
                'stop_instance')

        instance.save(expected_task_state=None)
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.STOP)
        self.compute_api.compute_rpcapi.stop_instance(
                self.context, instance, cast=True)

        self.mox.ReplayAll()

        self.compute_api.stop(self.context, instance)
        self.assertEqual(task_states.POWERING_OFF,
                         instance.task_state)
        self.assertEqual(0, instance.progress)

    def test_stop_invalid_state(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.PAUSED
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.stop,
                          self.context, instance)

    def test_stop_a_stopped_inst(self):
        instance = jsonutils.to_primitive(self._create_fake_instance(
                                          {'vm_state': vm_states.STOPPED}))

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.stop,
                          self.context, instance)

        db.instance_destroy(self.context, instance['uuid'])

    def test_stop_no_host(self):
        # Undo setUp() stubs (test_compute_cells)
        self.stubs.UnsetAll()
        instance = self._create_instance_obj()
        instance.host = ''
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.stop,
                          self.context, instance)

    def test_delete(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        self.compute_api.delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.DELETING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_in_resizing(self):
        def fake_quotas_reserve(context, expire=None, project_id=None,
                                                             **deltas):
            old_type = flavors.get_flavor_by_name('m1.tiny')
            # ensure using old instance type to create reservations
            self.assertEqual(deltas['cores'], -old_type['vcpus'])
            self.assertEqual(deltas['ram'], -old_type['memory_mb'])

        self.stubs.Set(QUOTAS, 'reserve', fake_quotas_reserve)

        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        # create a fake migration record (manager does this)
        new_inst_type = flavors.get_flavor_by_name('m1.small')
        db.migration_create(self.context.elevated(),
                 {'instance_uuid': instance['uuid'],
                  'old_instance_type_id': instance['instance_type_id'],
                  'new_instance_type_id': new_inst_type['id'],
                  'status': 'post-migrating'})

        # update instance type to resized one
        db.instance_update(self.context, instance['uuid'],
                           {'instance_type_id': new_inst_type['id'],
                            'vcpus': new_inst_type['vcpus'],
                            'memory_mb': new_inst_type['memory_mb'],
                            'task_state': task_states.RESIZE_FINISH})

        self.compute_api.delete(self.context, instance)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_in_resized(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        instance['vm_state'] = vm_states.RESIZED

        self.compute_api.delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.DELETING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_with_down_host(self):
        self.network_api_called = False

        def dummy(*args, **kwargs):
            self.network_api_called = True

        self.stubs.Set(self.compute_api.network_api, 'deallocate_for_instance',
                       dummy)

        #use old time to disable machine
        old_time = datetime.datetime(2012, 4, 1)

        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})
        timeutils.set_time_override(old_time)
        self.compute_api.delete(self.context, instance)
        timeutils.clear_time_override()

        self.assertEqual(instance['task_state'], None)
        self.assertTrue(self.network_api_called)

        # fetch the instance state from db and verify deletion.
        deleted_context = context.RequestContext('fake', 'fake',
                                                 read_deleted='yes')
        instance = db.instance_get_by_uuid(deleted_context, instance_uuid)
        self.assertEqual(instance['vm_state'], vm_states.DELETED)
        self.assertEqual(instance['task_state'], None)
        self.assertTrue(instance['deleted'])

    def test_delete_fast_if_host_not_set(self):
        instance = self._create_fake_instance({'host': None})
        self.compute_api.delete(self.context, instance)
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          self.context, instance['uuid'])

    def test_delete_handles_host_setting_race_condition(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})
        instance['host'] = None  # make it think host was never set
        self.compute_api.delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.DELETING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_fail(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        instance = db.instance_update(self.context, instance_uuid,
                                      {'disable_terminate': True})
        self.compute_api.delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_soft(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        # Make sure this is not called on the API side.
        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'commit')
        self.mox.ReplayAll()

        self.compute_api.soft_delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.SOFT_DELETING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_soft_fail(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})
        instance = db.instance_update(self.context, instance_uuid,
                                      {'disable_terminate': True})

        self.compute_api.soft_delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_delete_soft_rollback(self):
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'rollback')
        nova.quota.QUOTAS.rollback(mox.IgnoreArg(), mox.IgnoreArg(),
                                   project_id=mox.IgnoreArg())
        self.mox.ReplayAll()

        def fail(*args, **kwargs):
            raise test.TestingException()
        self.stubs.Set(self.compute_api.compute_rpcapi, 'soft_delete_instance',
                       fail)

        self.assertRaises(test.TestingException, self.compute_api.soft_delete,
                          self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.SOFT_DELETING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_force_delete(self):
        # Ensure instance can be deleted after a soft delete.
        instance = jsonutils.to_primitive(self._create_fake_instance(params={
                'host': CONF.host}))
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.compute_api.soft_delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.SOFT_DELETING)

        # set the state that the instance gets when soft_delete finishes
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'vm_state': vm_states.SOFT_DELETED,
                                       'task_state': None})

        self.compute_api.force_delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.DELETING)

    def test_suspend(self):
        # Ensure instance can be suspended.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        self.assertEqual(instance['task_state'], None)

        self.compute_api.suspend(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.SUSPENDING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_resume(self):
        # Ensure instance can be resumed (if suspended).
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        db.instance_update(self.context, instance['uuid'],
                           {'vm_state': vm_states.SUSPENDED})
        instance = db.instance_get(self.context, instance['id'])

        self.assertEqual(instance['task_state'], None)

        self.compute_api.resume(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['task_state'], task_states.RESUMING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_pause(self):
        # Ensure instance can be paused.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        self.assertEqual(instance['task_state'], None)

        self.compute_api.pause(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.PAUSING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_unpause(self):
        # Ensure instance can be unpaused.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        self.assertEqual(instance['task_state'], None)

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.PAUSING})
        self.compute.pause_instance(self.context, instance=instance)
        # set the state that the instance gets when pause finishes
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'vm_state': vm_states.PAUSED})

        self.compute_api.unpause(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.UNPAUSING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_restore(self):
        # Ensure instance can be restored from a soft delete.
        instance, instance_uuid = self._run_instance(params={
                'host': CONF.host})

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.compute_api.soft_delete(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.SOFT_DELETING)

        # set the state that the instance gets when soft_delete finishes
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'vm_state': vm_states.SOFT_DELETED,
                                       'task_state': None})

        # Ensure quotas are committed
        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'commit')
        nova.quota.QUOTAS.commit(mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()

        self.compute_api.restore(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.RESTORING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)
        # Set some image metadata that should get wiped out and reset
        # as well as some other metadata that should be preserved.
        db.instance_system_metadata_update(self.context, instance_uuid,
                {'image_kernel_id': 'old-data',
                 'image_ramdisk_id': 'old_data',
                 'image_something_else': 'old-data',
                 'image_should_remove': 'bye-bye',
                 'preserved': 'preserve this!'},
                True)

        # Make sure Compute API updates the image_ref before casting to
        # compute manager.
        orig_update = self.compute_api.update
        info = {'image_ref': None}

        def update_wrapper(*args, **kwargs):
            if 'image_ref' in kwargs:
                info['image_ref'] = kwargs['image_ref']
            return orig_update(*args, **kwargs)

        self.stubs.Set(self.compute_api, 'update', update_wrapper)

        image_ref = instance["image_ref"] + '-new_image_ref'
        password = "new_password"
        self.compute_api.rebuild(self.context, instance, image_ref, password)
        self.assertEqual(info['image_ref'], image_ref)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.REBUILDING)
        sys_metadata = db.instance_system_metadata_get(self.context,
                instance_uuid)
        self.assertEqual(sys_metadata,
                {'image_kernel_id': 'fake_kernel_id',
                'image_ramdisk_id': 'fake_ramdisk_id',
                'image_something_else': 'meow',
                'preserved': 'preserve this!'})
        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild_no_image(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': ''}))
        instance_uuid = instance['uuid']
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)
        self.compute.run_instance(self.context, instance=instance)
        self.compute_api.rebuild(self.context, instance, '', 'new_password')

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.REBUILDING)

    def test_rebuild_with_deleted_image(self):
        # If we're given a deleted image by glance, we should not be able to
        # rebuild from it
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        self.fake_image['name'] = 'fake_name'
        self.fake_image['status'] = 'DELETED'
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        expected_message = (
            exception.ImageNotActive.message % {'image_id':
            self.fake_image['id']})
        with testtools.ExpectedException(exception.ImageNotActive,
                                         expected_message):
            self.compute_api.rebuild(self.context, instance,
                                     self.fake_image['id'], 'new_password')

    def test_rebuild_with_too_little_ram(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        def fake_extract_flavor(_inst):
            return dict(memory_mb=64, root_gb=1)

        self.stubs.Set(flavors, 'extract_flavor',
                       fake_extract_flavor)

        self.fake_image['min_ram'] = 128
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeMemoryTooSmall,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image memory requirements and make sure it works
        self.fake_image['min_ram'] = 64

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')
        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild_with_too_little_disk(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        def fake_extract_flavor(_inst):
            return dict(memory_mb=64, root_gb=1)

        self.stubs.Set(flavors, 'extract_flavor',
                       fake_extract_flavor)

        self.fake_image['min_disk'] = 2
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image disk requirements and make sure it works
        self.fake_image['min_disk'] = 1

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')
        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild_with_just_enough_ram_and_disk(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        def fake_extract_flavor(_inst):
            return dict(memory_mb=64, root_gb=1)

        self.stubs.Set(flavors, 'extract_flavor',
                       fake_extract_flavor)

        self.fake_image['min_ram'] = 64
        self.fake_image['min_disk'] = 1
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')
        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild_with_no_ram_and_disk_reqs(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        def fake_extract_flavor(_inst):
            return dict(memory_mb=64, root_gb=1)

        self.stubs.Set(flavors, 'extract_flavor',
                       fake_extract_flavor)
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')
        db.instance_destroy(self.context, instance['uuid'])

    def test_rebuild_with_too_large_image(self):
        instance = jsonutils.to_primitive(
            self._create_fake_instance(params={'image_ref': '1'}))

        def fake_extract_flavor(_inst):
            return dict(memory_mb=64, root_gb=1)

        self.stubs.Set(flavors, 'extract_flavor',
                       fake_extract_flavor)

        self.fake_image['size'] = '1073741825'
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image to 1 GB limit and ensure it works
        self.fake_image['size'] = '1073741824'
        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')
        db.instance_destroy(self.context, instance['uuid'])

    def _stub_out_reboot(self, device_name):
        def fake_reboot_instance(rpcapi, context, instance,
                                 block_device_info,
                                 reboot_type):
            self.assertEqual(
                block_device_info['block_device_mapping'][0]['mount_device'],
                device_name)
        self.stubs.Set(nova.compute.rpcapi.ComputeAPI, 'reboot_instance',
                       fake_reboot_instance)

        self.stubs.Set(nova.virt.fake.FakeDriver, 'legacy_nwinfo',
                       lambda x: False)

    def test_reboot_soft(self):
        # Ensure instance can be soft rebooted.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        volume_id = 'fake'
        device_name = '/dev/vdc'
        volume = {'instance_uuid': instance['uuid'],
                  'device_name': device_name,
                  'delete_on_termination': False,
                  'connection_info': '{"foo": "bar"}',
                  'volume_id': volume_id}
        db.block_device_mapping_create(self.context, volume)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['task_state'], None)

        reboot_type = "SOFT"
        self._stub_out_reboot(device_name)
        self.compute_api.reboot(self.context, inst_ref, reboot_type)

        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        self.assertEqual(inst_ref['task_state'], task_states.REBOOTING)

        db.instance_destroy(self.context, inst_ref['uuid'])

    def test_reboot_hard(self):
        # Ensure instance can be hard rebooted.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        volume_id = 'fake'
        device_name = '/dev/vdc'
        volume = {'instance_uuid': instance['uuid'],
                  'device_name': device_name,
                  'delete_on_termination': False,
                  'connection_info': '{"foo": "bar"}',
                  'volume_id': volume_id}
        db.block_device_mapping_create(self.context, volume)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(inst_ref['task_state'], None)

        reboot_type = "HARD"
        self._stub_out_reboot(device_name)
        self.compute_api.reboot(self.context, inst_ref, reboot_type)

        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        self.assertEqual(inst_ref['task_state'], task_states.REBOOTING_HARD)

        db.instance_destroy(self.context, inst_ref['uuid'])

    def test_hard_reboot_of_soft_rebooting_instance(self):
        # Ensure instance can be hard rebooted while soft rebooting.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBOOTING})

        reboot_type = "HARD"
        self.compute_api.reboot(self.context, inst_ref, reboot_type)

        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        self.assertEqual(inst_ref['task_state'], task_states.REBOOTING_HARD)

        db.instance_destroy(self.context, inst_ref['uuid'])

    def test_soft_reboot_of_rebooting_instance(self):
        # Ensure instance can't be soft rebooted while rebooting.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])

        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBOOTING})

        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        reboot_type = "SOFT"
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.reboot,
                          self.context,
                          inst_ref,
                          reboot_type)

    def test_soft_reboot_of_rescued_instance(self):
        # Ensure instance can't be rebooted while in rescued state.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)

        inst_ref = db.instance_get_by_uuid(self.context, instance['uuid'])

        db.instance_update(self.context, instance['uuid'],
                           {"vm_state": vm_states.RESCUED})

        inst_ref = db.instance_get_by_uuid(self.context, inst_ref['uuid'])

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.reboot,
                          self.context,
                          inst_ref,
                          'SOFT')

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.reboot,
                          self.context,
                          inst_ref,
                          'HARD')

    def test_hostname_create(self):
        # Ensure instance hostname is set during creation.
        inst_type = flavors.get_flavor_by_name('m1.tiny')
        (instances, _) = self.compute_api.create(self.context,
                                                 inst_type,
                                                 None,
                                                 display_name='test host')

        self.assertEqual('test-host', instances[0]['hostname'])

    def test_set_admin_password(self):
        # Ensure instance can have its admin password set.
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        inst_ref = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(inst_ref['vm_state'], vm_states.ACTIVE)
        self.assertEqual(inst_ref['task_state'], None)

        def fake_rpc_method(context, topic, msg, do_cast=True):
            self.assertFalse(do_cast)

        self.stubs.Set(rpc, 'call', fake_rpc_method)

        self.compute_api.set_admin_password(self.context, inst_ref)

        inst_ref = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(inst_ref['vm_state'], vm_states.ACTIVE)
        self.assertEqual(inst_ref['task_state'],
                         task_states.UPDATING_PASSWORD)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(inst_ref))

    def test_rescue_unrescue(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        self.compute.run_instance(self.context, instance=instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['vm_state'], vm_states.ACTIVE)
        self.assertEqual(instance['task_state'], None)

        self.compute_api.rescue(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['vm_state'], vm_states.ACTIVE)
        self.assertEqual(instance['task_state'], task_states.RESCUING)

        params = {'vm_state': vm_states.RESCUED, 'task_state': None}
        db.instance_update(self.context, instance_uuid, params)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.compute_api.unrescue(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['vm_state'], vm_states.RESCUED)
        self.assertEqual(instance['task_state'], task_states.UNRESCUING)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(instance))

    def test_rescue_volume_backed(self):
        # Instance started without an image
        volume_backed_inst_1 = jsonutils.to_primitive(
            self._create_fake_instance({'image_ref': ''}))

        # Instance started with a placeholder image (for metadata)
        volume_backed_inst_2 = jsonutils.to_primitive(
            self._create_fake_instance(
                {'image_ref': 'my_placeholder_img',
                 'root_device_name': '/dev/vda'})
            )
        volume_backed_uuid_1 = volume_backed_inst_1['uuid']
        volume_backed_uuid_2 = volume_backed_inst_2['uuid']

        def fake_get_instance_bdms(*args, **kwargs):
            return [{'device_name': '/dev/vda',
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'volume_id': 'bf0b6b00-a20c-11e2-9e96-0800200c9a66'}]

        self.stubs.Set(self.compute_api, 'get_instance_bdms',
                       fake_get_instance_bdms)

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id, 'status': 'in-use'}

        self.stubs.Set(cinder.API, 'get', fake_volume_get)

        self.compute.run_instance(self.context,
                                  instance=volume_backed_inst_1)
        self.compute.run_instance(self.context,
                                  instance=volume_backed_inst_2)

        self.assertRaises(exception.InstanceNotRescuable,
                          self.compute_api.rescue, self.context,
                          volume_backed_inst_1)
        self.assertRaises(exception.InstanceNotRescuable,
                          self.compute_api.rescue, self.context,
                          volume_backed_inst_2)

        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(volume_backed_inst_1))
        self.compute.terminate_instance(self.context,
                instance=jsonutils.to_primitive(volume_backed_inst_2))

    def test_snapshot(self):
        # Ensure a snapshot of an instance can be created.
        instance = self._create_fake_instance()
        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'snap1')
        properties = image['properties']
        self.assertTrue('backup_type' not in properties)
        self.assertEqual(properties['image_type'], 'snapshot')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_given_image_uuid(self):
        """Ensure a snapshot of an instance can be created when image UUID
        is already known.
        """
        instance = self._create_fake_instance()
        name = 'snap1'
        extra_properties = {'extra_param': 'value1'}
        recv_meta = self.compute_api.snapshot(self.context, instance, name,
                                              extra_properties)
        image_id = recv_meta['id']

        def fake_show(meh, context, id):
            return recv_meta

        instance = db.instance_update(self.context, instance['uuid'],
                {'task_state': None})
        fake_image.stub_out_image_service(self.stubs)
        self.stubs.Set(fake_image._FakeImageService, 'show', fake_show)
        image = self.compute_api.snapshot(self.context, instance, name,
                                          extra_properties,
                                          image_id=image_id)
        self.assertEqual(image, recv_meta)

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_minram_mindisk_VHD(self):
        """Ensure a snapshots min_ram and min_disk are correct.

        A snapshot of a non-shrinkable VHD should have min_disk
        set to that of the original instances flavor.
        """

        self.fake_image.update(disk_format='vhd',
                               min_ram=1, min_disk=1)
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        instance = self._create_fake_instance(type_name='m1.small')

        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'snap1')
        instance_type = flavors.extract_flavor(instance)
        self.assertEqual(image['min_ram'], self.fake_image['min_ram'])
        self.assertEqual(image['min_disk'], instance_type['root_gb'])
        properties = image['properties']
        self.assertTrue('backup_type' not in properties)
        self.assertEqual(properties['image_type'], 'snapshot')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

    def test_snapshot_minram_mindisk(self):
        """Ensure a snapshots min_ram and min_disk are correct.

        A snapshot of an instance should have min_ram and min_disk
        set to that of the instances original image unless that
        image had a disk format of vhd.
        """

        self.fake_image['disk_format'] = 'raw'
        self.fake_image['min_ram'] = 512
        self.fake_image['min_disk'] = 1
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        instance = self._create_fake_instance()

        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'snap1')
        self.assertEqual(image['min_ram'], 512)
        self.assertEqual(image['min_disk'], 1)
        properties = image['properties']
        self.assertTrue('backup_type' not in properties)
        self.assertEqual(properties['image_type'], 'snapshot')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_minram_mindisk_img_missing_minram(self):
        """Ensure a snapshots min_ram and min_disk are correct.

        Do not show an attribute that the orig img did not have.
        """

        self.fake_image['disk_format'] = 'raw'
        self.fake_image['min_disk'] = 1
        self.stubs.Set(fake_image._FakeImageService, 'show', self.fake_show)

        instance = self._create_fake_instance()

        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'snap1')
        self.assertFalse('min_ram' in image)
        self.assertEqual(image['min_disk'], 1)
        properties = image['properties']
        self.assertTrue('backup_type' not in properties)
        self.assertEqual(properties['image_type'], 'snapshot')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_minram_mindisk_no_image(self):
        """Ensure a snapshots min_ram and min_disk are correct.

        A snapshots min_ram and min_disk should be set to default if
        an instances original image cannot be found.
        """

        def fake_show(*args):
            raise exception.ImageNotFound(image_id="fake")

        if not self.__class__.__name__ == "CellsComputeAPITestCase":
            # Cells tests will call this a 2nd time in child cell with
            # the newly created image_id, and we want that one to succeed.
            self.stubs.Set(fake_image._FakeImageService, 'show', fake_show)

        instance = self._create_fake_instance()

        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'snap1')

        # min_ram and min_disk are not returned when set to default
        self.assertFalse('min_ram' in image)
        self.assertFalse('min_disk' in image)

        properties = image['properties']
        self.assertTrue('backup_type' not in properties)
        self.assertEqual(properties['image_type'], 'snapshot')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_image_metadata_inheritance(self):
        # Ensure image snapshots inherit metadata from the base image
        self.flags(non_inheritable_image_properties=['spam'])

        def fake_instance_system_metadata_get(context, uuid):
            return dict(image_a=1, image_b=2, image_c='c', d='d', spam='spam')

        self.stubs.Set(db, 'instance_system_metadata_get',
                       fake_instance_system_metadata_get)

        instance = self._create_fake_instance()
        image = self.compute_api.snapshot(self.context, instance, 'snap1',
                                          {'extra_param': 'value1'})

        properties = image['properties']
        self.assertEqual(properties['a'], 1)
        self.assertEqual(properties['b'], 2)
        self.assertEqual(properties['c'], 'c')
        self.assertEqual(properties['d'], 'd')
        self.assertFalse('spam' in properties)

    def _do_test_snapshot_image_service_fails(self, method, image_id):
        # Ensure task_state remains at None if image service fails.
        def fake_fails(*args, **kwargs):
            raise test.TestingException()

        restore = getattr(fake_image._FakeImageService, method)
        self.stubs.Set(fake_image._FakeImageService, method, fake_fails)

        instance = self._create_fake_instance()
        self.assertRaises(test.TestingException,
                          self.compute_api.snapshot,
                          self.context,
                          instance,
                          'no_image_snapshot',
                          image_id=image_id)

        self.stubs.Set(fake_image._FakeImageService, method, restore)
        db_instance = db.instance_get_all(self.context)[0]
        self.assertIsNone(db_instance['task_state'])

    def test_snapshot_image_creation_fails(self):
        self._do_test_snapshot_image_service_fails('create', None)

    def test_snapshot_image_show_fails(self):
        self._do_test_snapshot_image_service_fails('show', 'image')

    def _do_test_backup_image_service_fails(self, method, image_id):
        # Ensure task_state remains at None if image service fails.
        def fake_fails(*args, **kwargs):
            raise test.TestingException()

        restore = getattr(fake_image._FakeImageService, method)
        self.stubs.Set(fake_image._FakeImageService, method, fake_fails)

        instance = self._create_fake_instance()
        self.assertRaises(test.TestingException,
                          self.compute_api.backup,
                          self.context,
                          instance,
                          'no_image_backup',
                          'DAILY',
                          0,
                          image_id=image_id)

        self.stubs.Set(fake_image._FakeImageService, method, restore)
        db_instance = db.instance_get_all(self.context)[0]
        self.assertIsNone(db_instance['task_state'])

    def test_backup_image_creation_fails(self):
        self._do_test_backup_image_service_fails('create', None)

    def test_backup_image_show_fails(self):
        self._do_test_backup_image_service_fails('show', 'image')

    def test_backup(self):
        # Can't backup an instance which is already being backed up.
        instance = self._create_fake_instance()
        image = self.compute_api.backup(self.context, instance,
                                        'backup1', 'DAILY', None,
                                        {'extra_param': 'value1'})

        self.assertEqual(image['name'], 'backup1')
        properties = image['properties']
        self.assertEqual(properties['backup_type'], 'DAILY')
        self.assertEqual(properties['image_type'], 'backup')
        self.assertEqual(properties['instance_uuid'], instance['uuid'])
        self.assertEqual(properties['extra_param'], 'value1')

        db.instance_destroy(self.context, instance['uuid'])

    def test_backup_conflict(self):
        # Can't backup an instance which is already being backed up.
        instance = self._create_fake_instance()
        instance_values = {'task_state': task_states.IMAGE_BACKUP}
        db.instance_update(self.context, instance['uuid'], instance_values)
        instance = self.compute_api.get(self.context, instance['uuid'])

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.backup,
                          self.context,
                          instance,
                          None,
                          None,
                          None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_snapshot_conflict(self):
        # Can't snapshot an instance which is already being snapshotted.
        instance = self._create_fake_instance()
        instance_values = {'task_state': task_states.IMAGE_SNAPSHOT}
        db.instance_update(self.context, instance['uuid'], instance_values)
        instance = self.compute_api.get(self.context, instance['uuid'])

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.snapshot,
                          self.context,
                          instance,
                          None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_resize_confirm_through_api(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        self.compute.run_instance(self.context, instance=instance)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.compute_api.resize(self.context, instance, '4')

        # Do the prep/finish_resize steps (manager does this)
        old_type = flavors.extract_flavor(instance)
        new_type = flavors.get_flavor_by_flavor_id('4')
        sys_meta = utils.metadata_to_dict(instance['system_metadata'])
        sys_meta = flavors.save_flavor_info(sys_meta,
                                            old_type, 'old_')
        sys_meta = flavors.save_flavor_info(sys_meta,
                                            new_type, 'new_')
        sys_meta = flavors.save_flavor_info(sys_meta,
                                            new_type)

        # create a fake migration record (manager does this)
        db.migration_create(self.context.elevated(),
                {'instance_uuid': instance['uuid'],
                 'status': 'finished'})
        # set the state that the instance gets when resize finishes
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'task_state': None,
                                       'vm_state': vm_states.RESIZED,
                                       'system_metadata': sys_meta})

        self.compute_api.confirm_resize(self.context, instance)
        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(instance))

    def test_resize_revert_through_api(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.compute.run_instance(self.context, instance=instance)

        self.compute_api.resize(self.context, instance, '4')

        # create a fake migration record (manager does this)
        db.migration_create(self.context.elevated(),
                {'instance_uuid': instance['uuid'],
                 'status': 'finished'})
        # set the state that the instance gets when resize finishes
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'task_state': None,
                                       'vm_state': vm_states.RESIZED})

        self.compute_api.revert_resize(self.context, instance)

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.RESIZED)
        self.assertEqual(instance['task_state'], task_states.RESIZE_REVERTING)

        self.compute.terminate_instance(self.context,
            instance=jsonutils.to_primitive(instance))

    def test_resize_invalid_flavor_fails(self):
        # Ensure invalid flavors raise.
        instance = self._create_fake_instance()
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(exception.NotFound, self.compute_api.resize,
                self.context, instance, 200)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_deleted_flavor_fails(self):
        instance = self._create_fake_instance()
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)

        name = 'test_resize_new_flavor'
        flavorid = 11
        flavors.create(name, 128, 1, 0, ephemeral_gb=0, flavorid=flavorid,
                       swap=0, rxtx_factor=1.0, is_public=True)
        flavors.destroy(name)
        self.assertRaises(exception.FlavorNotFound, self.compute_api.resize,
                self.context, instance, flavorid)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_same_flavor_fails(self):
        # Ensure invalid flavors raise.
        instance = self._create_fake_instance()
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)

        self.compute.run_instance(self.context, instance=instance)

        self.assertRaises(exception.CannotResizeToSameFlavor,
                          self.compute_api.resize, self.context, instance, 1)

        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_quota_exceeds_fails(self):
        instance = self._create_fake_instance()
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)

        name = 'test_resize_with_big_mem'
        flavorid = 11
        flavors.create(name, 102400, 1, 0, ephemeral_gb=0, flavorid=flavorid,
                       swap=0, rxtx_factor=1.0, is_public=True)
        self.assertRaises(exception.TooManyInstances, self.compute_api.resize,
                self.context, instance, flavorid)

        flavors.destroy(name)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_by_admin_for_tenant_with_sufficient_quota(self):
        user_project_id = 'user'
        instance = self._create_fake_instance({'project_id': user_project_id})
        self.context.is_admin = True
        db.quota_create(self.context, self.context.project_id, 'ram', 0)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)
        name = 'test_resize_with_big_mem'
        flavor_id = 11
        flavors.create(name, 1024, 1, 0, ephemeral_gb=0, flavorid=flavor_id,
                       swap=0, rxtx_factor=1.0, is_public=True)
        deltas = {'ram': 512}
        reservations = ['reservation_id']

        self.mox.StubOutWithMock(self.compute_api, '_reserve_quota_delta')

        self.compute_api._reserve_quota_delta(self.context,
                                              deltas,
                                              project_id=user_project_id). \
            AndReturn(reservations)

        CONF.cells.enable = True
        self.mox.StubOutWithMock(nova.quota.QUOTAS, 'commit')
        nova.quota.QUOTAS.commit(self.context, reservations,
                                 project_id=user_project_id)
        self.mox.ReplayAll()

        self.compute_api.resize(self.context, instance, flavor_id)

        flavors.destroy(name)
        db.quota_destroy_all_by_project(self.context, self.context.project_id)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_revert_deleted_flavor_fails(self):
        orig_name = 'test_resize_revert_orig_flavor'
        orig_flavorid = 11
        flavors.create(orig_name, 128, 1, 0, ephemeral_gb=0,
                       flavorid=orig_flavorid, swap=0, rxtx_factor=1.0,
                       is_public=True)

        instance = self._create_fake_instance(type_name=orig_name)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)

        old_instance_type_id = instance['instance_type_id']
        new_flavor = flavors.get_flavor_by_name('m1.tiny')
        new_flavorid = new_flavor['flavorid']
        new_instance_type_id = new_flavor['id']
        self.compute_api.resize(self.context, instance, new_flavorid)

        db.migration_create(self.context.elevated(),
                {'instance_uuid': instance['uuid'],
                 'old_instance_type_id': old_instance_type_id,
                 'new_instance_type_id': new_instance_type_id,
                 'status': 'finished'})
        instance = db.instance_update(self.context, instance['uuid'],
                                      {'task_state': None,
                                       'vm_state': vm_states.RESIZED})
        flavors.destroy(orig_name)
        self.assertRaises(exception.InstanceTypeNotFound,
                          self.compute_api.revert_resize,
                          self.context, instance)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_no_image(self):
        def _fake_prep_resize(_context, **args):
            image = args['image']
            self.assertEqual(image, {})

        instance = self._create_fake_instance(params={'image_ref': ''})
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)

        self.stubs.Set(self.compute_api.scheduler_rpcapi,
                       'prep_resize', _fake_prep_resize)

        self.compute_api.resize(self.context, instance, None)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_migrate(self):
        instance = self._create_fake_instance()
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)
        # Migrate simply calls resize() without a flavor_id.
        self.compute_api.resize(self.context, instance, None)
        self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_request_spec(self):
        def _fake_cast(_context, _topic, msg):
            request_spec = msg['args']['request_spec']
            filter_properties = msg['args']['filter_properties']
            instance_properties = request_spec['instance_properties']
            # resize with flavor_id = None will still send instance_type
            self.assertEqual(request_spec['instance_type'],
                             orig_instance_type)
            self.assertEqual(request_spec['instance_uuids'],
                             [instance['uuid']])
            self.assertEqual(FAKE_IMAGE_REF, request_spec['image']['id'])
            self.assertEqual(instance_properties['uuid'], instance['uuid'])
            self.assertEqual(instance_properties['host'], 'host2')
            # Ensure the instance passed to us has been updated with
            # progress set to 0 and task_state set to RESIZE_PREP.
            self.assertEqual(instance_properties['task_state'],
                    task_states.RESIZE_PREP)
            self.assertEqual(instance_properties['progress'], 0)
            self.assertIn('host2', filter_properties['ignore_hosts'])

        def _noop(*args, **kwargs):
            pass

        self.stubs.Set(self.compute.cells_rpcapi,
                       'consoleauth_delete_tokens', _noop)
        self.stubs.Set(self.compute.consoleauth_rpcapi,
                       'delete_tokens_for_instance', _noop)

        self.stubs.Set(rpc, 'cast', _fake_cast)

        instance = self._create_fake_instance(dict(host='host2'))
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        orig_instance_type = flavors.extract_flavor(instance)
        self.compute.run_instance(self.context, instance=instance)
        # We need to set the host to something 'known'.  Unfortunately,
        # the compute manager is using a cached copy of CONF.host,
        # so we can't just self.flags(host='host2') before calling
        # run_instance above.  Also, set progress to 10 so we ensure
        # it is reset to 0 in compute_api.resize().  (verified in
        # _fake_cast above).
        instance = db.instance_update(self.context, instance['uuid'],
                dict(host='host2', progress=10))
        # different host
        self.flags(host='host3')
        try:
            self.compute_api.resize(self.context, instance, None)
        finally:
            self.compute.terminate_instance(self.context, instance=instance)

    def test_resize_request_spec_noavoid(self):
        def _fake_cast(_context, topic, msg):
            request_spec = msg['args']['request_spec']
            filter_properties = msg['args']['filter_properties']
            instance_properties = request_spec['instance_properties']
            self.assertEqual(instance_properties['host'], 'host2')
            # Ensure the instance passed to us has been updated with
            # progress set to 0 and task_state set to RESIZE_PREP.
            self.assertEqual(instance_properties['task_state'],
                    task_states.RESIZE_PREP)
            self.assertEqual(instance_properties['progress'], 0)
            self.assertNotIn('host2', filter_properties['ignore_hosts'])

        def _noop(*args, **kwargs):
            pass

        self.stubs.Set(self.compute.cells_rpcapi,
                       'consoleauth_delete_tokens', _noop)
        self.stubs.Set(self.compute.consoleauth_rpcapi,
                       'delete_tokens_for_instance', _noop)

        self.stubs.Set(rpc, 'cast', _fake_cast)
        self.flags(allow_resize_to_same_host=True)
        self.flags(allow_migrate_to_same_host=True)

        instance = self._create_fake_instance(dict(host='host2'))
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        instance = jsonutils.to_primitive(instance)
        self.compute.run_instance(self.context, instance=instance)
        # We need to set the host to something 'known'.  Unfortunately,
        # the compute manager is using a cached copy of CONF.host,
        # so we can't just self.flags(host='host2') before calling
        # run_instance above.  Also, set progress to 10 so we ensure
        # it is reset to 0 in compute_api.resize().  (verified in
        # _fake_cast above).
        instance = db.instance_update(self.context, instance['uuid'],
                dict(host='host2', progress=10))
        # different host
        try:
            self.compute_api.resize(self.context, instance, None)
        finally:
            self.compute.terminate_instance(self.context, instance=instance)

    def test_get(self):
        # Test get instance.
        exp_instance = self._create_fake_instance()
        expected = dict(exp_instance.iteritems())
        expected['name'] = exp_instance['name']

        def fake_db_get(_context, _instance_uuid):
            return exp_instance

        self.stubs.Set(db, 'instance_get_by_uuid', fake_db_get)

        instance = self.compute_api.get(self.context, exp_instance['uuid'])
        self.assertEquals(expected, instance)

    def test_get_with_admin_context(self):
        # Test get instance.
        c = context.get_admin_context()
        exp_instance = self._create_fake_instance()
        expected = dict(exp_instance.iteritems())
        expected['name'] = exp_instance['name']

        def fake_db_get(context, instance_uuid):
            return exp_instance

        self.stubs.Set(db, 'instance_get_by_uuid', fake_db_get)

        instance = self.compute_api.get(c, exp_instance['uuid'])
        self.assertEquals(expected, instance)

    def test_get_with_integer_id(self):
        # Test get instance with an integer id.
        exp_instance = self._create_fake_instance()
        expected = dict(exp_instance.iteritems())
        expected['name'] = exp_instance['name']

        def fake_db_get(_context, _instance_id):
            return exp_instance

        self.stubs.Set(db, 'instance_get', fake_db_get)

        instance = self.compute_api.get(self.context, exp_instance['id'])
        self.assertEquals(expected, instance)

    def test_get_all_by_name_regexp(self):
        # Test searching instances by name (display_name).
        c = context.get_admin_context()
        instance1 = self._create_fake_instance({'display_name': 'woot'})
        instance2 = self._create_fake_instance({
                'display_name': 'woo'})
        instance3 = self._create_fake_instance({
                'display_name': 'not-woot'})

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^woo.*'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance1['uuid'] in instance_uuids)
        self.assertTrue(instance2['uuid'] in instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^woot.*'})
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertEqual(len(instances), 1)
        self.assertTrue(instance1['uuid'] in instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '.*oot.*'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance1['uuid'] in instance_uuids)
        self.assertTrue(instance3['uuid'] in instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^n.*'})
        self.assertEqual(len(instances), 1)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance3['uuid'] in instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': 'noth.*'})
        self.assertEqual(len(instances), 0)

        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])

    def test_get_all_by_multiple_options_at_once(self):
        # Test searching by multiple options at once.
        c = context.get_admin_context()
        network_manager = fake_network.FakeNetworkManager()
        self.stubs.Set(self.compute_api.network_api,
                       'get_instance_uuids_by_ip_filter',
                       network_manager.get_instance_uuids_by_ip_filter)

        instance1 = self._create_fake_instance({
                'display_name': 'woot',
                'id': 1,
                'uuid': '00000000-0000-0000-0000-000000000010'})
        instance2 = self._create_fake_instance({
                'display_name': 'woo',
                'id': 20,
                'uuid': '00000000-0000-0000-0000-000000000020'})
        instance3 = self._create_fake_instance({
                'display_name': 'not-woot',
                'id': 30,
                'uuid': '00000000-0000-0000-0000-000000000030'})

        # ip ends up matching 2nd octet here.. so all 3 match ip
        # but 'name' only matches one
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1', 'name': 'not.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance3['uuid'])

        # ip ends up matching any ip with a '1' in the last octet..
        # so instance 1 and 3.. but name should only match #1
        # but 'name' only matches one
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1$', 'name': '^woo.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        # same as above but no match on name (name matches instance1
        # but the ip query doesn't
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.2$', 'name': '^woot.*'})
        self.assertEqual(len(instances), 0)

        # ip matches all 3... ipv6 matches #2+#3...name matches #3
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1',
                             'name': 'not.*',
                             'ip6': '^.*12.*34.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance3['uuid'])

        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])

    def test_get_all_by_image(self):
        # Test searching instances by image.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance({'image_ref': '1234'})
        instance2 = self._create_fake_instance({'image_ref': '4567'})
        instance3 = self._create_fake_instance({'image_ref': '4567'})

        instances = self.compute_api.get_all(c, search_opts={'image': '123'})
        self.assertEqual(len(instances), 0)

        instances = self.compute_api.get_all(c, search_opts={'image': '1234'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        instances = self.compute_api.get_all(c, search_opts={'image': '4567'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance2['uuid'] in instance_uuids)
        self.assertTrue(instance3['uuid'] in instance_uuids)

        # Test passing a list as search arg
        instances = self.compute_api.get_all(c,
                                    search_opts={'image': ['1234', '4567']})
        self.assertEqual(len(instances), 3)

        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])

    def test_get_all_by_flavor(self):
        # Test searching instances by image.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance({'instance_type_id': 1})
        instance2 = self._create_fake_instance({'instance_type_id': 2})
        instance3 = self._create_fake_instance({'instance_type_id': 2})

        # NOTE(comstud): Migrations set up the instance_types table
        # for us.  Therefore, we assume the following is true for
        # these tests:
        # instance_type_id 1 == flavor 3
        # instance_type_id 2 == flavor 1
        # instance_type_id 3 == flavor 4
        # instance_type_id 4 == flavor 5
        # instance_type_id 5 == flavor 2

        instances = self.compute_api.get_all(c,
                search_opts={'flavor': 5})
        self.assertEqual(len(instances), 0)

        # ensure unknown filter maps to an exception
        self.assertRaises(exception.FlavorNotFound,
                          self.compute_api.get_all, c,
                          search_opts={'flavor': 99})

        instances = self.compute_api.get_all(c, search_opts={'flavor': 3})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['id'], instance1['id'])

        instances = self.compute_api.get_all(c, search_opts={'flavor': 1})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance2['uuid'] in instance_uuids)
        self.assertTrue(instance3['uuid'] in instance_uuids)

        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])

    def test_get_all_by_state(self):
        # Test searching instances by state.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance({
            'power_state': power_state.SHUTDOWN,
        })
        instance2 = self._create_fake_instance({
            'power_state': power_state.RUNNING,
        })
        instance3 = self._create_fake_instance({
            'power_state': power_state.RUNNING,
        })

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.SUSPENDED})
        self.assertEqual(len(instances), 0)

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.SHUTDOWN})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.RUNNING})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance2['uuid'] in instance_uuids)
        self.assertTrue(instance3['uuid'] in instance_uuids)

        # Test passing a list as search arg
        instances = self.compute_api.get_all(c,
                search_opts={'power_state': [power_state.SHUTDOWN,
                        power_state.RUNNING]})
        self.assertEqual(len(instances), 3)

        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])

    def test_get_all_by_metadata(self):
        # Test searching instances by metadata.

        c = context.get_admin_context()
        instance0 = self._create_fake_instance()
        instance1 = self._create_fake_instance({
                'metadata': {'key1': 'value1'}})
        instance2 = self._create_fake_instance({
                'metadata': {'key2': 'value2'}})
        instance3 = self._create_fake_instance({
                'metadata': {'key3': 'value3'}})
        instance4 = self._create_fake_instance({
                'metadata': {'key3': 'value3',
                             'key4': 'value4'}})

        # get all instances
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {}})
        self.assertEqual(len(instances), 5)

        # wrong key/value combination
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {'key1': 'value3'}})
        self.assertEqual(len(instances), 0)

        # non-existing keys
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {'key5': 'value1'}})
        self.assertEqual(len(instances), 0)

        # find existing instance
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {'key2': 'value2'}})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance2['uuid'])

        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {'key3': 'value3'}})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertTrue(instance3['uuid'] in instance_uuids)
        self.assertTrue(instance4['uuid'] in instance_uuids)

        # multiple criteria as a dict
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': {'key3': 'value3',
                                          'key4': 'value4'}})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance4['uuid'])

        # multiple criteria as a list
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': [{'key4': 'value4'},
                                          {'key3': 'value3'}]})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance4['uuid'])

        db.instance_destroy(c, instance0['uuid'])
        db.instance_destroy(c, instance1['uuid'])
        db.instance_destroy(c, instance2['uuid'])
        db.instance_destroy(c, instance3['uuid'])
        db.instance_destroy(c, instance4['uuid'])

    def test_instance_metadata(self):
        meta_changes = [None]
        self.flags(notify_on_any_change=True)

        def fake_change_instance_metadata(inst, ctxt, diff, instance=None,
                                          instance_uuid=None):
            meta_changes[0] = diff
        self.stubs.Set(compute_rpcapi.ComputeAPI, 'change_instance_metadata',
                       fake_change_instance_metadata)

        _context = context.get_admin_context()
        instance = self._create_fake_instance({'metadata': {'key1': 'value1'}})
        instance = dict(instance.iteritems())

        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key1': 'value1'})

        self.compute_api.update_instance_metadata(_context, instance,
                                                  {'key2': 'value2'})
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key1': 'value1', 'key2': 'value2'})
        self.assertEqual(meta_changes, [{'key2': ['+', 'value2']}])

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 1)
        msg = test_notifier.NOTIFICATIONS[0]
        payload = msg['payload']
        self.assertTrue('metadata' in payload)
        self.assertEquals(payload['metadata'], metadata)

        new_metadata = {'key2': 'bah', 'key3': 'value3'}
        self.compute_api.update_instance_metadata(_context, instance,
                                                  new_metadata, delete=True)
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, new_metadata)
        self.assertEqual(meta_changes, [{
                    'key1': ['-'],
                    'key2': ['+', 'bah'],
                    'key3': ['+', 'value3'],
                    }])

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 2)
        msg = test_notifier.NOTIFICATIONS[1]
        payload = msg['payload']
        self.assertTrue('metadata' in payload)
        self.assertEquals(payload['metadata'], metadata)

        self.compute_api.delete_instance_metadata(_context, instance, 'key2')
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key3': 'value3'})
        self.assertEqual(meta_changes, [{'key2': ['-']}])

        self.assertEquals(len(test_notifier.NOTIFICATIONS), 3)
        msg = test_notifier.NOTIFICATIONS[2]
        payload = msg['payload']
        self.assertTrue('metadata' in payload)
        self.assertEquals(payload['metadata'], {})

        db.instance_destroy(_context, instance['uuid'])

    def test_disallow_metadata_changes_during_building(self):
        def fake_change_instance_metadata(inst, ctxt, diff, instance=None,
                                          instance_uuid=None):
            pass
        self.stubs.Set(compute_rpcapi.ComputeAPI, 'change_instance_metadata',
                       fake_change_instance_metadata)

        instance = self._create_fake_instance({'vm_state': vm_states.BUILDING})
        instance = dict(instance)

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.delete_instance_metadata, self.context,
                instance, "key")

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.update_instance_metadata, self.context,
                instance, "key")

    def test_get_instance_faults(self):
        # Get an instances latest fault.
        instance = self._create_fake_instance()

        fault_fixture = {
                'code': 404,
                'instance_uuid': instance['uuid'],
                'message': "HTTPNotFound",
                'details': "Stock details for test",
                'created_at': datetime.datetime(2010, 10, 10, 12, 0, 0),
            }

        def return_fault(_ctxt, instance_uuids):
            return dict.fromkeys(instance_uuids, [fault_fixture])

        self.stubs.Set(nova.db,
                       'instance_fault_get_by_instance_uuids',
                       return_fault)

        _context = context.get_admin_context()
        output = self.compute_api.get_instance_faults(_context, [instance])
        expected = {instance['uuid']: [fault_fixture]}
        self.assertEqual(output, expected)

        db.instance_destroy(_context, instance['uuid'])

    @staticmethod
    def _parse_db_block_device_mapping(bdm_ref):
        attr_list = ('delete_on_termination', 'device_name', 'no_device',
                     'virtual_name', 'volume_id', 'volume_size', 'snapshot_id')
        bdm = {}
        for attr in attr_list:
            val = bdm_ref.get(attr, None)
            if val:
                bdm[attr] = val

        return bdm

    def test_update_block_device_mapping(self):
        swap_size = 1
        instance_type = {'swap': swap_size}
        instance = self._create_fake_instance()
        mappings = [
                {'virtual': 'ami', 'device': 'sda1'},
                {'virtual': 'root', 'device': '/dev/sda1'},

                {'virtual': 'swap', 'device': 'sdb4'},
                {'virtual': 'swap', 'device': 'sdb3'},
                {'virtual': 'swap', 'device': 'sdb2'},
                {'virtual': 'swap', 'device': 'sdb1'},

                {'virtual': 'ephemeral0', 'device': 'sdc1'},
                {'virtual': 'ephemeral1', 'device': 'sdc2'},
                {'virtual': 'ephemeral2', 'device': 'sdc3'}]
        block_device_mapping = [
                # root
                {'device_name': '/dev/sda1',
                 'snapshot_id': '00000000-aaaa-bbbb-cccc-000000000000',
                 'delete_on_termination': False},


                # overwrite swap
                {'device_name': '/dev/sdb2',
                 'snapshot_id': '11111111-aaaa-bbbb-cccc-111111111111',
                 'delete_on_termination': False},
                {'device_name': '/dev/sdb3',
                 'snapshot_id': '22222222-aaaa-bbbb-cccc-222222222222'},
                {'device_name': '/dev/sdb4',
                 'no_device': True},

                # overwrite ephemeral
                {'device_name': '/dev/sdc2',
                 'snapshot_id': '33333333-aaaa-bbbb-cccc-333333333333',
                 'delete_on_termination': False},
                {'device_name': '/dev/sdc3',
                 'snapshot_id': '44444444-aaaa-bbbb-cccc-444444444444'},
                {'device_name': '/dev/sdc4',
                 'no_device': True},

                # volume
                {'device_name': '/dev/sdd1',
                 'snapshot_id': '55555555-aaaa-bbbb-cccc-555555555555',
                 'delete_on_termination': False},
                {'device_name': '/dev/sdd2',
                 'snapshot_id': '66666666-aaaa-bbbb-cccc-666666666666'},
                {'device_name': '/dev/sdd3',
                 'snapshot_id': '77777777-aaaa-bbbb-cccc-777777777777'},
                {'device_name': '/dev/sdd4',
                 'no_device': True}]

        self.compute_api._update_image_block_device_mapping(
            self.context, instance_type, instance['uuid'], mappings)

        bdms = [self._parse_db_block_device_mapping(bdm_ref)
                for bdm_ref in block_device.legacy_mapping(
                        db.block_device_mapping_get_all_by_instance(
                            self.context, instance['uuid']))]
        expected_result = [
            {'virtual_name': 'swap', 'device_name': '/dev/sdb1',
             'volume_size': swap_size, 'delete_on_termination': True},
            {'virtual_name': 'ephemeral0', 'device_name': '/dev/sdc1',
             'delete_on_termination': True},

            # NOTE(yamahata): ATM only ephemeral0 is supported.
            #                 they're ignored for now
            #{'virtual_name': 'ephemeral1', 'device_name': '/dev/sdc2'},
            #{'virtual_name': 'ephemeral2', 'device_name': '/dev/sdc3'}
            ]
        bdms.sort()
        expected_result.sort()
        self.assertThat(bdms, matchers.DictListMatches(expected_result))

        self.compute_api._update_block_device_mapping(
            self.context, flavors.get_default_flavor(),
            instance['uuid'], block_device_mapping)
        bdms = [self._parse_db_block_device_mapping(bdm_ref)
                for bdm_ref in block_device.legacy_mapping(
                    db.block_device_mapping_get_all_by_instance(
                        self.context, instance['uuid']))]
        expected_result = [
            {'snapshot_id': '00000000-aaaa-bbbb-cccc-000000000000',
               'device_name': '/dev/sda1'},

            {'virtual_name': 'swap', 'device_name': '/dev/sdb1',
             'volume_size': swap_size, 'delete_on_termination': True},
            {'snapshot_id': '11111111-aaaa-bbbb-cccc-111111111111',
               'device_name': '/dev/sdb2'},
            {'snapshot_id': '22222222-aaaa-bbbb-cccc-222222222222',
                'device_name': '/dev/sdb3'},
            {'no_device': True, 'device_name': '/dev/sdb4'},

            {'virtual_name': 'ephemeral0', 'device_name': '/dev/sdc1',
             'delete_on_termination': True},
            {'snapshot_id': '33333333-aaaa-bbbb-cccc-333333333333',
                'device_name': '/dev/sdc2'},
            {'snapshot_id': '44444444-aaaa-bbbb-cccc-444444444444',
                'device_name': '/dev/sdc3'},
            {'no_device': True, 'device_name': '/dev/sdc4'},

            {'snapshot_id': '55555555-aaaa-bbbb-cccc-555555555555',
                'device_name': '/dev/sdd1'},
            {'snapshot_id': '66666666-aaaa-bbbb-cccc-666666666666',
                'device_name': '/dev/sdd2'},
            {'snapshot_id': '77777777-aaaa-bbbb-cccc-777777777777',
                'device_name': '/dev/sdd3'},
            {'no_device': True, 'device_name': '/dev/sdd4'}]
        bdms.sort()
        expected_result.sort()
        self.assertThat(bdms, matchers.DictListMatches(expected_result))

        for bdm in db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid']):
            db.block_device_mapping_destroy(self.context, bdm['id'])
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.compute.terminate_instance(self.context, instance)

    def test_populate_instance_for_bdm(self):
        # Test that the image bdm is created
        instance_type = {'swap': 1}
        instance = self._create_fake_instance(
            {'root_device_name': 'vda'}
        )
        image = {'uuid': FAKE_IMAGE_REF}
        fake_bdms = [{'device_name': '/dev/vda',
                      'snapshot_id': '33333333-aaaa-bbbb-cccc-333333333333',
                      'delete_on_termination': False}]

        # Has an image but no bdms
        self.compute_api._populate_instance_for_bdm(self.context,
                                                    instance,
                                                    instance_type,
                                                    image, [])
        bdms = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEqual(len(bdms), 1)
        self.assertEqual(bdms[0]['image_id'], FAKE_IMAGE_REF)
        for bdm in bdms:
            db.block_device_mapping_destroy(self.context, bdm['id'])

        # Has an image and is volume backed - legacy style
        self.compute_api._populate_instance_for_bdm(self.context,
                                                    instance,
                                                    instance_type,
                                                    image, fake_bdms)
        bdms = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEqual(len(bdms), 1)
        self.assertEqual(bdms[0]['snapshot_id'],
                         '33333333-aaaa-bbbb-cccc-333333333333')
        for bdm in bdms:
            db.block_device_mapping_destroy(self.context, bdm['id'])

        # Is volume backed and has no image
        instance['image_ref'] = ''
        self.compute_api._populate_instance_for_bdm(self.context,
                                                    instance,
                                                    instance_type,
                                                    image, fake_bdms)
        bdms = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEqual(len(bdms), 1)
        self.assertEqual(bdms[0]['snapshot_id'],
                         '33333333-aaaa-bbbb-cccc-333333333333')
        for bdm in bdms:
            db.block_device_mapping_destroy(self.context, bdm['id'])

    def test_volume_size(self):
        ephemeral_size = 2
        swap_size = 3
        inst_type = {'ephemeral_gb': ephemeral_size, 'swap': swap_size}
        self.assertEqual(self.compute_api._volume_size(inst_type,
                                                       'ephemeral0'),
                         ephemeral_size)
        self.assertEqual(self.compute_api._volume_size(inst_type,
                                                       'ephemeral1'),
                         0)
        self.assertEqual(self.compute_api._volume_size(inst_type,
                                                       'swap'),
                         swap_size)

    def test_reservation_id_one_instance(self):
        """Verify building an instance has a reservation_id that
        matches return value from create.
        """
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None)
        try:
            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0]['reservation_id'], resv_id)
        finally:
            db.instance_destroy(self.context, refs[0]['uuid'])

    def test_reservation_ids_two_instances(self):
        """Verify building 2 instances at once results in a
        reservation_id being returned equal to reservation id set
        in both instances.
        """
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None,
                min_count=2, max_count=2)
        try:
            self.assertEqual(len(refs), 2)
            self.assertNotEqual(resv_id, None)
        finally:
            for instance in refs:
                self.assertEqual(instance['reservation_id'], resv_id)

        db.instance_destroy(self.context, refs[0]['uuid'])

    def test_multi_instance_display_name_template(self):
        self.flags(multi_instance_display_name_template='%(name)s')
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None,
                min_count=2, max_count=2, display_name='x')
        self.assertEqual(refs[0]['display_name'], 'x')
        self.assertEqual(refs[0]['hostname'], 'x')
        self.assertEqual(refs[1]['display_name'], 'x')
        self.assertEqual(refs[1]['hostname'], 'x')

        self.flags(multi_instance_display_name_template='%(name)s-%(count)s')
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None,
                min_count=2, max_count=2, display_name='x')
        self.assertEqual(refs[0]['display_name'], 'x-1')
        self.assertEqual(refs[0]['hostname'], 'x-1')
        self.assertEqual(refs[1]['display_name'], 'x-2')
        self.assertEqual(refs[1]['hostname'], 'x-2')

        self.flags(multi_instance_display_name_template='%(name)s-%(uuid)s')
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(), None,
                min_count=2, max_count=2, display_name='x')
        self.assertEqual(refs[0]['display_name'], 'x-%s' % refs[0]['uuid'])
        self.assertEqual(refs[0]['hostname'], 'x-%s' % refs[0]['uuid'])
        self.assertEqual(refs[1]['display_name'], 'x-%s' % refs[1]['uuid'])
        self.assertEqual(refs[1]['hostname'], 'x-%s' % refs[1]['uuid'])

    def test_instance_architecture(self):
        # Test the instance architecture.
        i_ref = self._create_fake_instance()
        self.assertEqual(i_ref['architecture'], 'x86_64')
        db.instance_destroy(self.context, i_ref['uuid'])

    def test_instance_unknown_architecture(self):
        # Test if the architecture is unknown.
        instance = jsonutils.to_primitive(self._create_fake_instance(
                        params={'architecture': ''}))
        try:
            self.compute.run_instance(self.context, instance=instance)
            instance = db.instance_get_by_uuid(self.context,
                    instance['uuid'])
            self.assertNotEqual(instance['architecture'], 'Unknown')
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_instance_name_template(self):
        # Test the instance_name template.
        self.flags(instance_name_template='instance-%d')
        i_ref = self._create_fake_instance()
        self.assertEqual(i_ref['name'], 'instance-%d' % i_ref['id'])
        db.instance_destroy(self.context, i_ref['uuid'])

        self.flags(instance_name_template='instance-%(uuid)s')
        i_ref = self._create_fake_instance()
        self.assertEqual(i_ref['name'], 'instance-%s' % i_ref['uuid'])
        db.instance_destroy(self.context, i_ref['uuid'])

        self.flags(instance_name_template='%(id)d-%(uuid)s')
        i_ref = self._create_fake_instance()
        self.assertEqual(i_ref['name'], '%d-%s' %
                (i_ref['id'], i_ref['uuid']))
        db.instance_destroy(self.context, i_ref['uuid'])

        # not allowed.. default is uuid
        self.flags(instance_name_template='%(name)s')
        i_ref = self._create_fake_instance()
        self.assertEqual(i_ref['name'], i_ref['uuid'])
        db.instance_destroy(self.context, i_ref['uuid'])

    def test_add_remove_fixed_ip(self):
        instance = self._create_fake_instance(params={'host': CONF.host})
        self.compute_api.add_fixed_ip(self.context, instance, '1')
        self.compute_api.remove_fixed_ip(self.context, instance, '192.168.1.1')
        self.compute_api.delete(self.context, instance)

    def test_attach_volume_invalid(self):
        self.assertRaises(exception.InvalidDevicePath,
                self.compute_api.attach_volume,
                self.context,
                {'locked': False, 'vm_state': vm_states.ACTIVE},
                None,
                '/invalid')

    def test_no_attach_volume_in_rescue_state(self):
        def fake(*args, **kwargs):
            pass

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}

        self.stubs.Set(cinder.API, 'get', fake_volume_get)
        self.stubs.Set(cinder.API, 'check_attach', fake)
        self.stubs.Set(cinder.API, 'reserve_volume', fake)

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.attach_volume,
                self.context,
                {'uuid': 'fake_uuid', 'locked': False,
                'vm_state': vm_states.RESCUED},
                None,
                '/dev/vdb')

    def test_no_detach_volume_in_rescue_state(self):
        # Ensure volume can be detached from instance

        params = {'vm_state': vm_states.RESCUED}
        instance = self._create_fake_instance(params=params)

        volume = {'id': 1, 'attach_status': 'in-use',
                  'instance_uuid': instance['uuid']}

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.detach_volume,
                self.context, instance, volume)

    def test_no_rescue_in_volume_state_attaching(self):
        # Make sure a VM cannot be rescued while volume is being attached
        instance = self._create_fake_instance()

        def fake_get_instance_bdms(*args, **kwargs):
            return [{'device_name': '/dev/vda',
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'volume_id': 'bf0b6b00-a20c-11e2-9e96-0800200c9a66'}]

        self.stubs.Set(self.compute_api, 'get_instance_bdms',
                       fake_get_instance_bdms)

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id, 'status': 'attaching'}

        self.stubs.Set(cinder.API, 'get', fake_volume_get)

        volume = {'id': 'bf0b6b00-a20c-11e2-9e96-0800200c9a66',
                  'state': 'active', 'instance_uuid': instance['uuid']}

        self.assertRaises(exception.InvalidVolume,
                self.compute_api.rescue, self.context, instance)

    def test_vnc_console(self):
        # Make sure we can a vnc console for an instance.

        fake_instance = {'uuid': 'fake_uuid',
                         'host': 'fake_compute_host'}
        fake_console_type = "novnc"
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_console_host',
                             'port': 'fake_console_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance['uuid']}
        fake_connect_info2 = copy.deepcopy(fake_connect_info)
        fake_connect_info2['access_url'] = 'fake_console_url'

        self.mox.StubOutWithMock(rpc, 'call')

        rpc_msg1 = {'method': 'get_vnc_console',
                    'namespace': None,
                    'args': {'instance': fake_instance,
                             'console_type': fake_console_type},
                   'version': compute_rpcapi.ComputeAPI.BASE_RPC_API_VERSION}
        rpc_msg2 = {'method': 'authorize_console',
                    'namespace': None,
                    'args': fake_connect_info,
                    'version': '1.2'}

        rpc.call(self.context, 'compute.%s' % fake_instance['host'],
                rpc_msg1, None).AndReturn(fake_connect_info2)
        rpc.call(self.context, CONF.consoleauth_topic,
                rpc_msg2, None).AndReturn(None)

        self.mox.ReplayAll()

        console = self.compute_api.get_vnc_console(self.context,
                fake_instance, fake_console_type)
        self.assertEqual(console, {'url': 'fake_console_url'})

    def test_get_vnc_console_no_host(self):
        instance = self._create_fake_instance(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_vnc_console,
                          self.context, instance, 'novnc')

        db.instance_destroy(self.context, instance['uuid'])

    def test_spice_console(self):
        # Make sure we can a spice console for an instance.

        fake_instance = {'uuid': 'fake_uuid',
                         'host': 'fake_compute_host'}
        fake_console_type = "spice-html5"
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_console_host',
                             'port': 'fake_console_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance['uuid']}
        fake_connect_info2 = copy.deepcopy(fake_connect_info)
        fake_connect_info2['access_url'] = 'fake_console_url'

        self.mox.StubOutWithMock(rpc, 'call')

        rpc_msg1 = {'method': 'get_spice_console',
                    'namespace': None,
                    'args': {'instance': fake_instance,
                             'console_type': fake_console_type},
                   'version': '2.24'}
        rpc_msg2 = {'method': 'authorize_console',
                    'namespace': None,
                    'args': fake_connect_info,
                    'version': '1.2'}

        rpc.call(self.context, 'compute.%s' % fake_instance['host'],
                rpc_msg1, None).AndReturn(fake_connect_info2)
        rpc.call(self.context, CONF.consoleauth_topic,
                rpc_msg2, None).AndReturn(None)

        self.mox.ReplayAll()

        console = self.compute_api.get_spice_console(self.context,
                fake_instance, fake_console_type)
        self.assertEqual(console, {'url': 'fake_console_url'})

    def test_get_spice_console_no_host(self):
        instance = self._create_fake_instance(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_spice_console,
                          self.context, instance, 'spice')

        db.instance_destroy(self.context, instance['uuid'])

    def test_console_output(self):
        fake_instance = {'uuid': 'fake_uuid',
                         'host': 'fake_compute_host'}
        fake_tail_length = 699
        fake_console_output = 'fake console output'

        self.mox.StubOutWithMock(rpc, 'call')

        rpc_msg = {'method': 'get_console_output',
                   'namespace': None,
                   'args': {'instance': fake_instance,
                            'tail_length': fake_tail_length},
                   'version': compute_rpcapi.ComputeAPI.BASE_RPC_API_VERSION}
        rpc.call(self.context, 'compute.%s' % fake_instance['host'],
                rpc_msg, None).AndReturn(fake_console_output)

        self.mox.ReplayAll()

        output = self.compute_api.get_console_output(self.context,
                fake_instance, tail_length=fake_tail_length)
        self.assertEqual(output, fake_console_output)

    def test_console_output_no_host(self):
        instance = self._create_fake_instance(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_console_output,
                          self.context, instance)

        db.instance_destroy(self.context, instance['uuid'])

    def test_attach_interface(self):
        instance = {
            'image_ref': 'foo',
            }
        self.mox.StubOutWithMock(compute_manager, '_get_image_meta')
        self.mox.StubOutWithMock(self.compute.network_api,
                                 'allocate_port_for_instance')
        nwinfo = network_model.NetworkInfo()
        nwinfo.append(fake_network_cache_model.new_vif())
        network_id = nwinfo[0]['network']['id']
        port_id = nwinfo[0]['id']
        req_ip = '1.2.3.4'
        self.compute.network_api.allocate_port_for_instance(
            self.context, instance, port_id, network_id, req_ip,
            self.compute.conductor_api).AndReturn(nwinfo)
        compute_manager._get_image_meta(self.context, instance['image_ref'])
        self.mox.ReplayAll()
        network, mapping = self.compute.attach_interface(self.context,
                                                         instance,
                                                         network_id,
                                                         port_id,
                                                         req_ip)
        self.assertEqual(network['id'], network_id)
        return nwinfo, port_id

    def test_detach_interface(self):
        nwinfo, port_id = self.test_attach_interface()
        self.stubs.Set(self.compute.network_api, 'get_instance_nw_info',
                       lambda *a, **k: nwinfo)
        self.stubs.Set(self.compute.network_api,
                       'deallocate_port_for_instance',
                       lambda a, b, c, d: [])
        self.compute.detach_interface(self.context, {}, port_id)
        self.assertEqual(self.compute.driver._interfaces, {})

    def test_attach_volume(self):
        # Ensure instance can be soft rebooted.

        called = {}

        def fake_check_attach(*args, **kwargs):
            called['fake_check_attach'] = True

        def fake_reserve_volume(*args, **kwargs):
            called['fake_reserve_volume'] = True

        def fake_volume_get(self, context, volume_id):
            called['fake_volume_get'] = True
            return {'id': volume_id}

        def fake_rpc_attach_volume(self, context, **kwargs):
            called['fake_rpc_attach_volume'] = True

        self.stubs.Set(cinder.API, 'get', fake_volume_get)
        self.stubs.Set(cinder.API, 'check_attach', fake_check_attach)
        self.stubs.Set(cinder.API, 'reserve_volume',
                       fake_reserve_volume)
        self.stubs.Set(compute_rpcapi.ComputeAPI, 'attach_volume',
                       fake_rpc_attach_volume)

        instance = self._create_fake_instance()
        self.compute_api.attach_volume(self.context, instance, 1, '/dev/vdb')
        self.assertTrue(called.get('fake_check_attach'))
        self.assertTrue(called.get('fake_reserve_volume'))
        self.assertTrue(called.get('fake_reserve_volume'))
        self.assertTrue(called.get('fake_rpc_attach_volume'))

    def test_attach_volume_no_device(self):

        called = {}

        def fake_check_attach(*args, **kwargs):
            called['fake_check_attach'] = True

        def fake_reserve_volume(*args, **kwargs):
            called['fake_reserve_volume'] = True

        def fake_volume_get(self, context, volume_id):
            called['fake_volume_get'] = True
            return {'id': volume_id}

        def fake_rpc_attach_volume(self, context, **kwargs):
            called['fake_rpc_attach_volume'] = True

        self.stubs.Set(cinder.API, 'get', fake_volume_get)
        self.stubs.Set(cinder.API, 'check_attach', fake_check_attach)
        self.stubs.Set(cinder.API, 'reserve_volume',
                       fake_reserve_volume)
        self.stubs.Set(compute_rpcapi.ComputeAPI, 'attach_volume',
                       fake_rpc_attach_volume)

    def test_detach_volume(self):
        # Ensure volume can be detached from instance
        called = {}
        instance = self._create_fake_instance()
        volume = {'id': 1, 'attach_status': 'in-use',
                  'instance_uuid': instance['uuid']}

        def fake_check_detach(*args, **kwargs):
            called['fake_check_detach'] = True

        def fake_begin_detaching(*args, **kwargs):
            called['fake_begin_detaching'] = True

        def fake_rpc_detach_volume(self, context, **kwargs):
            called['fake_rpc_detach_volume'] = True

        self.stubs.Set(cinder.API, 'check_detach', fake_check_detach)
        self.stubs.Set(cinder.API, 'begin_detaching', fake_begin_detaching)
        self.stubs.Set(compute_rpcapi.ComputeAPI, 'detach_volume',
                       fake_rpc_detach_volume)

        self.compute_api.detach_volume(self.context,
                instance, volume)
        self.assertTrue(called.get('fake_check_detach'))
        self.assertTrue(called.get('fake_begin_detaching'))
        self.assertTrue(called.get('fake_rpc_detach_volume'))

    def test_detach_invalid_volume(self):
        # Ensure exception is raised while detaching an un-attached volume
        instance = {'uuid': 'uuid1',
                    'locked': False,
                    'vm_state': vm_states.ACTIVE}
        volume = {'id': 1, 'attach_status': 'detached'}

        self.assertRaises(exception.InvalidVolume,
                          self.compute_api.detach_volume, self.context,
                          instance, volume)

    def test_detach_unattached_volume(self):
        # Ensure exception is raised when volume's idea of attached
        # instance doesn't match.
        instance = {'uuid': 'uuid1',
                    'locked': False,
                    'vm_state': vm_states.ACTIVE}
        volume = {'id': 1, 'attach_status': 'in-use',
                  'instance_uuid': 'uuid2'}

        self.assertRaises(exception.VolumeUnattached,
                          self.compute_api.detach_volume, self.context,
                          instance, volume)

    def test_detach_volume_libvirt_is_down(self):
        # Ensure rollback during detach if libvirt goes down

        called = {}
        instance = self._create_fake_instance()

        def fake_get_instance_volume_bdm(*args, **kwargs):
            return {'device_name': '/dev/vdb', 'volume_id': 1,
                    'connection_info': '{"test": "test"}'}

        def fake_libvirt_driver_instance_exists(*args, **kwargs):
            called['fake_libvirt_driver_instance_exists'] = True
            return False

        def fake_libvirt_driver_detach_volume_fails(*args, **kwargs):
            called['fake_libvirt_driver_detach_volume_fails'] = True
            raise AttributeError()

        def fake_roll_detaching(*args, **kwargs):
            called['fake_roll_detaching'] = True

        self.stubs.Set(cinder.API, 'roll_detaching', fake_roll_detaching)
        self.stubs.Set(self.compute, "_get_instance_volume_bdm",
                       fake_get_instance_volume_bdm)
        self.stubs.Set(self.compute.driver, "instance_exists",
                       fake_libvirt_driver_instance_exists)
        self.stubs.Set(self.compute.driver, "detach_volume",
                       fake_libvirt_driver_detach_volume_fails)

        self.assertRaises(AttributeError, self.compute.detach_volume,
                          self.context, 1, instance)
        self.assertTrue(called.get('fake_libvirt_driver_instance_exists'))
        self.assertTrue(called.get('fake_roll_detaching'))

    def test_terminate_with_volumes(self):
        # Make sure that volumes get detached during instance termination.
        admin = context.get_admin_context()
        instance = self._create_fake_instance()

        volume_id = 'fake'
        values = {'instance_uuid': instance['uuid'],
                  'device_name': '/dev/vdc',
                  'delete_on_termination': False,
                  'volume_id': volume_id,
                  }
        db.block_device_mapping_create(admin, values)

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}
        self.stubs.Set(cinder.API, "get", fake_volume_get)

        # Stub out and record whether it gets detached
        result = {"detached": False}

        def fake_detach(self, context, volume_id_param):
            result["detached"] = volume_id_param == volume_id
        self.stubs.Set(cinder.API, "detach", fake_detach)

        def fake_terminate_connection(self, context, volume_id, connector):
            return {}
        self.stubs.Set(cinder.API, "terminate_connection",
                       fake_terminate_connection)

        # Kill the instance and check that it was detached
        self.compute.terminate_instance(admin, instance=instance)
        self.assertTrue(result["detached"])

    def test_inject_network_info(self):
        instance = self._create_fake_instance(params={'host': CONF.host})
        self.compute.run_instance(self.context,
                instance=jsonutils.to_primitive(instance))
        instance = self.compute_api.get(self.context, instance['uuid'])
        self.compute_api.inject_network_info(self.context, instance)
        self.compute_api.delete(self.context, instance)

    def test_reset_network(self):
        instance = self._create_fake_instance()
        self.compute.run_instance(self.context,
                instance=jsonutils.to_primitive(instance))
        instance = self.compute_api.get(self.context, instance['uuid'])
        self.compute_api.reset_network(self.context, instance)

    def test_lock(self):
        instance = self._create_fake_instance()
        self.compute_api.lock(self.context, instance)
        self.compute_api.delete(self.context, instance)

    def test_unlock(self):
        instance = self._create_fake_instance()
        self.compute_api.unlock(self.context, instance)
        self.compute_api.delete(self.context, instance)

    def test_get_lock(self):
        instance = self._create_fake_instance()
        self.assertFalse(self.compute_api.get_lock(self.context, instance))
        db.instance_update(self.context, instance['uuid'], {'locked': True})
        self.assertTrue(self.compute_api.get_lock(self.context, instance))

    def test_add_remove_security_group(self):
        instance = self._create_fake_instance()

        self.compute.run_instance(self.context,
                instance=jsonutils.to_primitive(instance))
        instance = self.compute_api.get(self.context, instance['uuid'])
        security_group_name = self._create_group()['name']

        self.security_group_api.add_to_instance(self.context,
                                                instance,
                                                security_group_name)
        self.security_group_api.remove_from_instance(self.context,
                                                     instance,
                                                     security_group_name)

    def test_get_diagnostics(self):
        instance = self._create_fake_instance()
        self.compute_api.get_diagnostics(self.context, instance)
        self.compute_api.delete(self.context, instance)

    def test_inject_file(self):
        # Ensure we can write a file to an instance.
        instance = self._create_fake_instance()
        self.compute_api.inject_file(self.context, instance,
                                     "/tmp/test", "File Contents")
        db.instance_destroy(self.context, instance['uuid'])

    def test_secgroup_refresh(self):
        instance = self._create_fake_instance()

        def rule_get(*args, **kwargs):
            mock_rule = db_fakes.FakeModel({'parent_group_id': 1})
            return [mock_rule]

        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': [instance]})
            return mock_group

        self.stubs.Set(
                   self.compute_api.db,
                   'security_group_rule_get_by_security_group_grantee',
                   rule_get)
        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        topic = rpc.queue_get_for(self.context, CONF.compute_topic,
                                  instance['host'])
        rpc.cast(self.context, topic,
                {"method": "refresh_instance_security_rules",
                 "namespace": None,
                 "args": {'instance': jsonutils.to_primitive(instance)},
                 "version":
                    compute_rpcapi.SecurityGroupAPI.BASE_RPC_API_VERSION})
        self.mox.ReplayAll()

        self.security_group_api.trigger_members_refresh(self.context, [1])

    def test_secgroup_refresh_once(self):
        instance = self._create_fake_instance()

        def rule_get(*args, **kwargs):
            mock_rule = db_fakes.FakeModel({'parent_group_id': 1})
            return [mock_rule]

        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': [instance]})
            return mock_group

        self.stubs.Set(
                   self.compute_api.db,
                   'security_group_rule_get_by_security_group_grantee',
                   rule_get)
        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        topic = rpc.queue_get_for(self.context, CONF.compute_topic,
                                  instance['host'])
        rpc.cast(self.context, topic,
                {"method": "refresh_instance_security_rules",
                 "namespace": None,
                 "args": {'instance': jsonutils.to_primitive(instance)},
                 "version":
                   compute_rpcapi.SecurityGroupAPI.BASE_RPC_API_VERSION})
        self.mox.ReplayAll()

        self.security_group_api.trigger_members_refresh(self.context, [1, 2])

    def test_secgroup_refresh_none(self):
        def rule_get(*args, **kwargs):
            mock_rule = db_fakes.FakeModel({'parent_group_id': 1})
            return [mock_rule]

        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': []})
            return mock_group

        self.stubs.Set(
                   self.compute_api.db,
                   'security_group_rule_get_by_security_group_grantee',
                   rule_get)
        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        self.mox.ReplayAll()

        self.security_group_api.trigger_members_refresh(self.context, [1])

    def test_secrule_refresh(self):
        instance = self._create_fake_instance()

        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': [instance]})
            return mock_group

        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        topic = rpc.queue_get_for(self.context, CONF.compute_topic,
                                  instance['host'])
        rpc.cast(self.context, topic,
                {"method": "refresh_instance_security_rules",
                 "namespace": None,
                 "args": {'instance': jsonutils.to_primitive(instance)},
                 "version":
                   compute_rpcapi.SecurityGroupAPI.BASE_RPC_API_VERSION})
        self.mox.ReplayAll()

        self.security_group_api.trigger_rules_refresh(self.context, [1])

    def test_secrule_refresh_once(self):
        instance = self._create_fake_instance()

        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': [instance]})
            return mock_group

        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        topic = rpc.queue_get_for(self.context, CONF.compute_topic,
                                  instance['host'])
        rpc.cast(self.context, topic,
                {"method": "refresh_instance_security_rules",
                 "namespace": None,
                 "args": {'instance': jsonutils.to_primitive(instance)},
                 "version":
                   compute_rpcapi.SecurityGroupAPI.BASE_RPC_API_VERSION})
        self.mox.ReplayAll()

        self.security_group_api.trigger_rules_refresh(self.context, [1, 2])

    def test_secrule_refresh_none(self):
        def group_get(*args, **kwargs):
            mock_group = db_fakes.FakeModel({'instances': []})
            return mock_group

        self.stubs.Set(self.compute_api.db, 'security_group_get', group_get)

        self.mox.StubOutWithMock(rpc, 'cast')
        self.mox.ReplayAll()

        self.security_group_api.trigger_rules_refresh(self.context, [1, 2])

    def test_live_migrate(self):
        instance, instance_uuid = self._run_instance()

        self.compute_api.live_migrate(self.context, instance,
                                      block_migration=True,
                                      disk_over_commit=True,
                                      host_name='fake_dest_host')

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.MIGRATING)

        db.instance_destroy(self.context, instance['uuid'])

    def test_evacuate(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        def fake_service_is_up(*args, **kwargs):
            return False

        def fake_rebuild_instance(*args, **kwargs):
            db.instance_update(self.context, instance_uuid,
                               {'host': kwargs['host']})

        self.stubs.Set(self.compute_api.servicegroup_api, 'service_is_up',
                fake_service_is_up)
        self.stubs.Set(self.compute_api.compute_rpcapi, 'rebuild_instance',
                fake_rebuild_instance)
        self.compute_api.evacuate(self.context.elevated(),
                                  instance,
                                  host='fake_dest_host',
                                  on_shared_storage=True,
                                  admin_password=None)

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.REBUILDING)
        self.assertEqual(instance['host'], 'fake_dest_host')

        db.instance_destroy(self.context, instance['uuid'])

    def test_fail_evacuate_from_non_existing_host(self):
        inst = {}
        inst['vm_state'] = vm_states.ACTIVE
        inst['image_ref'] = FAKE_IMAGE_REF
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = self.user_id
        inst['project_id'] = self.project_id
        inst['host'] = 'fake_host'
        inst['node'] = NODENAME
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['memory_mb'] = 0
        inst['vcpus'] = 0
        inst['root_gb'] = 0
        inst['ephemeral_gb'] = 0
        inst['architecture'] = 'x86_64'
        inst['os_type'] = 'Linux'

        instance = jsonutils.to_primitive(db.instance_create(self.context,
                                                             inst))
        instance_uuid = instance['uuid']
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        self.assertRaises(exception.ComputeHostNotFound,
                self.compute_api.evacuate, self.context.elevated(), instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_fail_evacuate_from_running_host(self):
        instance = jsonutils.to_primitive(self._create_fake_instance())
        instance_uuid = instance['uuid']
        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], None)

        def fake_service_is_up(*args, **kwargs):
            return True

        self.stubs.Set(self.compute_api.servicegroup_api, 'service_is_up',
                fake_service_is_up)

        self.assertRaises(exception.ComputeServiceUnavailable,
                self.compute_api.evacuate, self.context.elevated(), instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)

        db.instance_destroy(self.context, instance['uuid'])

    def test_fail_evacuate_instance_in_wrong_state(self):
        instances = [
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.BUILDING})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.PAUSED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.SUSPENDED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.RESCUED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.RESIZED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.SOFT_DELETED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.DELETED})),
            jsonutils.to_primitive(self._create_fake_instance(
                                    {'vm_state': vm_states.ERROR}))
        ]

        for instance in instances:
            self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.evacuate, self.context, instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)
            db.instance_destroy(self.context, instance['uuid'])


def fake_rpc_method(context, topic, msg, do_cast=True):
    pass


def _create_service_entries(context, values={'avail_zone1': ['fake_host1',
                                                             'fake_host2'],
                                             'avail_zone2': ['fake_host3'], }):
    for avail_zone, hosts in values.iteritems():
        for host in hosts:
            db.service_create(context,
                              {'host': host,
                               'binary': 'nova-compute',
                               'topic': 'compute',
                               'report_count': 0})
    return values


class ComputeAPIAggrTestCase(BaseTestCase):
    """This is for unit coverage of aggregate-related methods
    defined in nova.compute.api.
    """

    def setUp(self):
        super(ComputeAPIAggrTestCase, self).setUp()
        self.api = compute_api.AggregateAPI()
        self.context = context.get_admin_context()
        self.stubs.Set(rpc, 'call', fake_rpc_method)
        self.stubs.Set(rpc, 'cast', fake_rpc_method)

    def test_aggregate_no_zone(self):
        # Ensure we can create an aggregate without an availability  zone
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         None)
        self.api.delete_aggregate(self.context, aggr['id'])
        db.aggregate_get(self.context.elevated(read_deleted='yes'),
                         aggr['id'])
        self.assertRaises(exception.AggregateNotFound,
                          self.api.delete_aggregate, self.context, aggr['id'])

    def test_update_aggregate_metadata(self):
        # Ensure metadata can be updated.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        metadata = {'foo_key1': 'foo_value1',
                    'foo_key2': 'foo_value2', }
        aggr = self.api.update_aggregate_metadata(self.context, aggr['id'],
                                                  metadata)
        metadata['foo_key1'] = None
        expected = self.api.update_aggregate_metadata(self.context,
                                             aggr['id'], metadata)
        self.assertThat(expected['metadata'],
                        matchers.DictMatches({'availability_zone': 'fake_zone',
                        'foo_key2': 'foo_value2'}))

    def test_delete_aggregate(self):
        # Ensure we can delete an aggregate.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        self.api.delete_aggregate(self.context, aggr['id'])
        db.aggregate_get(self.context.elevated(read_deleted='yes'),
                         aggr['id'])
        self.assertRaises(exception.AggregateNotFound,
                          self.api.delete_aggregate, self.context, aggr['id'])

    def test_delete_non_empty_aggregate(self):
        # Ensure InvalidAggregateAction is raised when non empty aggregate.
        _create_service_entries(self.context,
                                {'fake_availability_zone': ['fake_host']})
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_availability_zone')
        self.api.add_host_to_aggregate(self.context, aggr['id'], 'fake_host')
        self.assertRaises(exception.InvalidAggregateAction,
                          self.api.delete_aggregate, self.context, aggr['id'])

    def test_add_host_to_aggregate(self):
        # Ensure we can add a host to an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values.keys()[0]
        fake_host = values[fake_zone][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        aggr = self.api.add_host_to_aggregate(self.context,
                                              aggr['id'], fake_host)
        self.assertEqual(len(aggr['hosts']), 1)

    def test_add_host_to_aggregate_multiple(self):
        # Ensure we can add multiple hosts to an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values.keys()[0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        for host in values[fake_zone]:
            aggr = self.api.add_host_to_aggregate(self.context,
                                                  aggr['id'], host)
        self.assertEqual(len(aggr['hosts']), len(values[fake_zone]))

    def test_add_host_to_aggregate_raise_not_found(self):
        # Ensure ComputeHostNotFound is raised when adding invalid host.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        self.assertRaises(exception.ComputeHostNotFound,
                          self.api.add_host_to_aggregate,
                          self.context, aggr['id'], 'invalid_host')

    def test_remove_host_from_aggregate_active(self):
        # Ensure we can remove a host from an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values.keys()[0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        for host in values[fake_zone]:
            aggr = self.api.add_host_to_aggregate(self.context,
                                                  aggr['id'], host)
        expected = self.api.remove_host_from_aggregate(self.context,
                                                       aggr['id'],
                                                       values[fake_zone][0])
        self.assertEqual(len(aggr['hosts']) - 1, len(expected['hosts']))

    def test_remove_host_from_aggregate_raise_not_found(self):
        # Ensure ComputeHostNotFound is raised when removing invalid host.
        _create_service_entries(self.context, {'fake_zone': ['fake_host']})
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        self.assertRaises(exception.ComputeHostNotFound,
                          self.api.remove_host_from_aggregate,
                          self.context, aggr['id'], 'invalid_host')


class ComputeAggrTestCase(BaseTestCase):
    """This is for unit coverage of aggregate-related methods
    defined in nova.compute.manager.
    """

    def setUp(self):
        super(ComputeAggrTestCase, self).setUp()
        self.context = context.get_admin_context()
        values = {'name': 'test_aggr'}
        az = {'availability_zone': 'test_zone'}
        self.aggr = db.aggregate_create(self.context, values, metadata=az)

    def test_add_aggregate_host(self):
        def fake_driver_add_to_aggregate(context, aggregate, host, **_ignore):
            fake_driver_add_to_aggregate.called = True
            return {"foo": "bar"}
        self.stubs.Set(self.compute.driver, "add_to_aggregate",
                       fake_driver_add_to_aggregate)

        self.compute.add_aggregate_host(self.context, "host",
                aggregate=jsonutils.to_primitive(self.aggr))
        self.assertTrue(fake_driver_add_to_aggregate.called)

    def test_remove_aggregate_host(self):
        def fake_driver_remove_from_aggregate(context, aggregate, host,
                                              **_ignore):
            fake_driver_remove_from_aggregate.called = True
            self.assertEqual("host", host, "host")
            return {"foo": "bar"}
        self.stubs.Set(self.compute.driver, "remove_from_aggregate",
                       fake_driver_remove_from_aggregate)

        self.compute.remove_aggregate_host(self.context,
                aggregate=jsonutils.to_primitive(self.aggr), host="host")
        self.assertTrue(fake_driver_remove_from_aggregate.called)

    def test_add_aggregate_host_passes_slave_info_to_driver(self):
        def driver_add_to_aggregate(context, aggregate, host, **kwargs):
            self.assertEquals(self.context, context)
            self.assertEquals(aggregate['id'], self.aggr['id'])
            self.assertEquals(host, "the_host")
            self.assertEquals("SLAVE_INFO", kwargs.get("slave_info"))

        self.stubs.Set(self.compute.driver, "add_to_aggregate",
                       driver_add_to_aggregate)

        self.compute.add_aggregate_host(self.context, "the_host",
                slave_info="SLAVE_INFO",
                aggregate=jsonutils.to_primitive(self.aggr))

    def test_remove_from_aggregate_passes_slave_info_to_driver(self):
        def driver_remove_from_aggregate(context, aggregate, host, **kwargs):
            self.assertEquals(self.context, context)
            self.assertEquals(aggregate['id'], self.aggr['id'])
            self.assertEquals(host, "the_host")
            self.assertEquals("SLAVE_INFO", kwargs.get("slave_info"))

        self.stubs.Set(self.compute.driver, "remove_from_aggregate",
                       driver_remove_from_aggregate)

        self.compute.remove_aggregate_host(self.context,
                aggregate=jsonutils.to_primitive(self.aggr), host="the_host",
                slave_info="SLAVE_INFO")


class ComputePolicyTestCase(BaseTestCase):

    def setUp(self):
        super(ComputePolicyTestCase, self).setUp()

        self.compute_api = compute.API()

    def test_actions_are_prefixed(self):
        self.mox.StubOutWithMock(policy, 'enforce')
        nova.policy.enforce(self.context, 'compute:reboot', {})
        self.mox.ReplayAll()
        compute_api.check_policy(self.context, 'reboot', {})

    def test_wrapped_method(self):
        instance = self._create_fake_instance(params={'host': None})

        # force delete to fail
        rules = {"compute:delete": [["false:false"]]}
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.delete, self.context, instance)

        # reset rules to allow deletion
        rules = {"compute:delete": []}
        self.policy.set_rules(rules)

        self.compute_api.delete(self.context, instance)

    def test_create_fail(self):
        rules = {"compute:create": [["false:false"]]}
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.create, self.context, '1', '1')

    def test_create_attach_volume_fail(self):
        rules = {
            "compute:create": [],
            "compute:create:attach_network": [["false:false"]],
            "compute:create:attach_volume": [],
        }
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.create, self.context, '1', '1',
                          requested_networks='blah',
                          block_device_mapping='blah')

    def test_create_attach_network_fail(self):
        rules = {
            "compute:create": [],
            "compute:create:attach_network": [],
            "compute:create:attach_volume": [["false:false"]],
        }
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.create, self.context, '1', '1',
                          requested_networks='blah',
                          block_device_mapping='blah')

    def test_get_fail(self):
        instance = self._create_fake_instance()

        rules = {
            "compute:get": [["false:false"]],
        }
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.get, self.context, instance['uuid'])

    def test_get_all_fail(self):
        rules = {
            "compute:get_all": [["false:false"]],
        }
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.get_all, self.context)

    def test_get_instance_faults(self):
        instance1 = self._create_fake_instance()
        instance2 = self._create_fake_instance()
        instances = [instance1, instance2]

        rules = {
            "compute:get_instance_faults": [["false:false"]],
        }
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.get_instance_faults,
                          context.get_admin_context(), instances)

    def test_force_host_fail(self):
        rules = {"compute:create": [],
                 "compute:create:forced_host": [["role:fake"]]}
        self.policy.set_rules(rules)

        self.assertRaises(exception.PolicyNotAuthorized,
                          self.compute_api.create, self.context, None, '1',
                          availability_zone='1:1')

    def test_force_host_pass(self):
        rules = {"compute:create": [],
                 "compute:create:forced_host": []}
        self.policy.set_rules(rules)

        self.compute_api.create(self.context, None, '1',
                                availability_zone='1:1')


class DisabledInstanceTypesTestCase(BaseTestCase):
    """
    Some instance-types are marked 'disabled' which means that they will not
    show up in customer-facing listings. We do, however, want those
    instance-types to be available for emergency migrations and for rebuilding
    of existing instances.

    One legitimate use of the 'disabled' field would be when phasing out a
    particular instance-type. We still want customers to be able to use an
    instance that of the old type, and we want Ops to be able perform
    migrations against it, but we *don't* want customers building new slices
    with ths phased-out instance-type.
    """
    def setUp(self):
        super(DisabledInstanceTypesTestCase, self).setUp()
        self.compute_api = compute.API()
        self.inst_type = flavors.get_default_flavor()

    def test_can_build_instance_from_visible_instance_type(self):
        self.inst_type['disabled'] = False
        # Assert that exception.InstanceTypeNotFound is not raised
        self.compute_api.create(self.context, self.inst_type, None)

    def test_cannot_build_instance_from_disabled_instance_type(self):
        self.inst_type['disabled'] = True
        self.assertRaises(exception.InstanceTypeNotFound,
            self.compute_api.create, self.context, self.inst_type, None)

    def test_can_resize_to_visible_instance_type(self):
        instance = self._create_fake_instance()
        orig_get_flavor_by_flavor_id =\
                flavors.get_flavor_by_flavor_id

        def fake_get_flavor_by_flavor_id(flavor_id, ctxt=None,
                                                read_deleted="yes"):
            instance_type = orig_get_flavor_by_flavor_id(flavor_id,
                                                                ctxt,
                                                                read_deleted)
            instance_type['disabled'] = False
            return instance_type

        self.stubs.Set(flavors, 'get_flavor_by_flavor_id',
                       fake_get_flavor_by_flavor_id)

        # FIXME(sirp): for legacy this raises FlavorNotFound instead of
        # InstanceTypeNotFound; we should eventually make it raise
        # InstanceTypeNotFound for consistency.
        self.compute_api.resize(self.context, instance, '4')

    def test_cannot_resize_to_disabled_instance_type(self):
        instance = self._create_fake_instance()
        orig_get_flavor_by_flavor_id = \
                flavors.get_flavor_by_flavor_id

        def fake_get_flavor_by_flavor_id(flavor_id, ctxt=None,
                                                read_deleted="yes"):
            instance_type = orig_get_flavor_by_flavor_id(flavor_id,
                                                                ctxt,
                                                                read_deleted)
            instance_type['disabled'] = True
            return instance_type

        self.stubs.Set(flavors, 'get_flavor_by_flavor_id',
                       fake_get_flavor_by_flavor_id)

        # FIXME(sirp): for legacy this raises FlavorNotFound instead of
        # InstanceTypeNot; we should eventually make it raise
        # InstanceTypeNotFound for consistency.
        self.assertRaises(exception.FlavorNotFound,
            self.compute_api.resize, self.context, instance, '4')


class ComputeReschedulingTestCase(BaseTestCase):
    """Tests re-scheduling logic for new build requests."""

    def setUp(self):
        super(ComputeReschedulingTestCase, self).setUp()

        self.expected_task_state = task_states.SCHEDULING

        def fake_update(*args, **kwargs):
            self.updated_task_state = kwargs.get('task_state')
        self.stubs.Set(self.compute, '_instance_update', fake_update)

    def _reschedule(self, request_spec=None, filter_properties=None,
                    exc_info=None):
        if not filter_properties:
            filter_properties = {}

        instance_uuid = "12-34-56-78-90"

        admin_password = None
        injected_files = None
        requested_networks = None
        is_first_time = False

        scheduler_method = self.compute.scheduler_rpcapi.run_instance
        method_args = (request_spec, admin_password, injected_files,
                       requested_networks, is_first_time, filter_properties)
        return self.compute._reschedule(self.context, request_spec,
                filter_properties, instance_uuid, scheduler_method,
                method_args, self.expected_task_state, exc_info=exc_info)

    def test_reschedule_no_filter_properties(self):
        # no filter_properties will disable re-scheduling.
        self.assertFalse(self._reschedule())

    def test_reschedule_no_retry_info(self):
        # no retry info will also disable re-scheduling.
        filter_properties = {}
        self.assertFalse(self._reschedule(filter_properties=filter_properties))

    def test_reschedule_no_request_spec(self):
        # no request spec will also disable re-scheduling.
        retry = dict(num_attempts=1)
        filter_properties = dict(retry=retry)
        self.assertFalse(self._reschedule(filter_properties=filter_properties))

    def test_reschedule_success(self):
        retry = dict(num_attempts=1)
        filter_properties = dict(retry=retry)
        request_spec = {'instance_uuids': ['foo', 'bar']}
        try:
            raise test.TestingException("just need an exception")
        except test.TestingException:
            exc_info = sys.exc_info()
            exc_str = traceback.format_exception(*exc_info)

        self.assertTrue(self._reschedule(filter_properties=filter_properties,
            request_spec=request_spec, exc_info=exc_info))
        self.assertEqual(1, len(request_spec['instance_uuids']))
        self.assertEqual(self.updated_task_state, self.expected_task_state)
        self.assertEqual(exc_str, filter_properties['retry']['exc'])


class ComputeReschedulingResizeTestCase(ComputeReschedulingTestCase):
    """Test re-scheduling logic for prep_resize requests."""

    def setUp(self):
        super(ComputeReschedulingResizeTestCase, self).setUp()
        self.expected_task_state = task_states.RESIZE_PREP

    def _reschedule(self, request_spec=None, filter_properties=None,
                    exc_info=None):
        if not filter_properties:
            filter_properties = {}

        instance_uuid = "12-34-56-78-90"

        instance = {'uuid': instance_uuid}
        instance_type = {}
        image = None
        reservations = None

        scheduler_method = self.compute.scheduler_rpcapi.prep_resize
        method_args = (instance, instance_type, image, request_spec,
                filter_properties, reservations)

        return self.compute._reschedule(self.context, request_spec,
                filter_properties, instance_uuid, scheduler_method,
                method_args, self.expected_task_state, exc_info=exc_info)


class InnerTestingException(Exception):
    pass


class ComputeRescheduleOrErrorTestCase(BaseTestCase):
    """Test logic and exception handling around rescheduling or re-raising
    original exceptions when builds fail.
    """

    def setUp(self):
        super(ComputeRescheduleOrErrorTestCase, self).setUp()
        self.instance = self._create_fake_instance()

    def test_reschedule_or_error_called(self):
        """Basic sanity check to make sure _reschedule_or_error is called
        when a build fails.
        """
        self.mox.StubOutWithMock(self.compute, '_spawn')
        self.mox.StubOutWithMock(self.compute, '_reschedule_or_error')

        self.compute._spawn(mox.IgnoreArg(), self.instance, mox.IgnoreArg(),
                [], mox.IgnoreArg(), [], None, set_access_ip=False).AndRaise(
                        test.TestingException("BuildError"))
        self.compute._reschedule_or_error(mox.IgnoreArg(), self.instance,
                mox.IgnoreArg(), None, None, None, False, None, {}, []).\
                        AndReturn(True)

        self.mox.ReplayAll()
        self.compute._run_instance(self.context, None, {}, None, None, None,
                False, None, self.instance)

    def test_shutdown_instance_fail(self):
        """Test shutdown instance failing before re-scheduling logic can even
        run.
        """
        instance_uuid = self.instance['uuid']
        self.mox.StubOutWithMock(self.compute, '_shutdown_instance')

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()

            compute_utils.add_instance_fault_from_exc(self.context,
                    self.compute.conductor_api,
                    self.instance, exc_info[0], exc_info=exc_info)
            self.compute._shutdown_instance(self.context, self.instance,
                    mox.IgnoreArg()).AndRaise(InnerTestingException("Error"))
            self.compute._log_original_error(exc_info, instance_uuid)

            self.mox.ReplayAll()

            # should raise the deallocation exception, not the original build
            # error:
            self.assertRaises(InnerTestingException,
                    self.compute._reschedule_or_error, self.context,
                    self.instance, exc_info, None, None, None, False, None, {})

    def test_reschedule_fail(self):
        # Test handling of exception from _reschedule.
        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()

        instance_uuid = self.instance['uuid']
        method_args = (None, None, None, None, False, {})
        self.mox.StubOutWithMock(self.compute, '_shutdown_instance')
        self.mox.StubOutWithMock(self.compute, '_cleanup_volumes')
        self.mox.StubOutWithMock(self.compute, '_reschedule')

        self.compute._shutdown_instance(self.context, self.instance,
                                        mox.IgnoreArg())
        self.compute._cleanup_volumes(self.context, instance_uuid,
                                        mox.IgnoreArg())
        self.compute._reschedule(self.context, None, instance_uuid,
                {}, self.compute.scheduler_rpcapi.run_instance,
                method_args, task_states.SCHEDULING, exc_info).AndRaise(
                        InnerTestingException("Inner"))

        self.mox.ReplayAll()

        self.assertFalse(self.compute._reschedule_or_error(self.context,
            self.instance, exc_info, None, None, None, False, None, {}))

    def test_reschedule_false(self):
        # Test not-rescheduling, but no nested exception.
        instance_uuid = self.instance['uuid']
        method_args = (None, None, None, None, False, {})
        self.mox.StubOutWithMock(self.compute, '_shutdown_instance')
        self.mox.StubOutWithMock(self.compute, '_cleanup_volumes')
        self.mox.StubOutWithMock(self.compute, '_reschedule')

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            compute_utils.add_instance_fault_from_exc(self.context,
                    self.compute.conductor_api,
                    self.instance, exc_info[0], exc_info=exc_info)

            self.compute._shutdown_instance(self.context, self.instance,
                                            mox.IgnoreArg())
            self.compute._cleanup_volumes(self.context, instance_uuid,
                                            mox.IgnoreArg())
            self.compute._reschedule(self.context, None, {}, instance_uuid,
                    self.compute.scheduler_rpcapi.run_instance, method_args,
                    task_states.SCHEDULING, exc_info).AndReturn(False)

            self.mox.ReplayAll()

            # re-scheduling is False, the original build error should be
            # raised here:
            self.assertFalse(self.compute._reschedule_or_error(self.context,
                self.instance, exc_info, None, None, None, False, None, {}))

    def test_reschedule_true(self):
        # Test behavior when re-scheduling happens.
        instance_uuid = self.instance['uuid']
        method_args = (None, None, None, None, False, {})
        self.mox.StubOutWithMock(self.compute, '_shutdown_instance')
        self.mox.StubOutWithMock(self.compute, '_cleanup_volumes')
        self.mox.StubOutWithMock(self.compute, '_reschedule')

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()

            compute_utils.add_instance_fault_from_exc(self.context,
                    self.compute.conductor_api,
                    self.instance, exc_info[0], exc_info=exc_info)
            self.compute._shutdown_instance(self.context, self.instance,
                                            mox.IgnoreArg())
            self.compute._cleanup_volumes(self.context, instance_uuid,
                                          mox.IgnoreArg())
            self.compute._reschedule(self.context, None, {}, instance_uuid,
                    self.compute.scheduler_rpcapi.run_instance,
                    method_args, task_states.SCHEDULING, exc_info).AndReturn(
                            True)
            self.compute._log_original_error(exc_info, instance_uuid)

            self.mox.ReplayAll()

            # re-scheduling is True, original error is logged, but nothing
            # is raised:
            self.compute._reschedule_or_error(self.context, self.instance,
                    exc_info, None, None, None, False, None, {})

    def test_no_reschedule_on_delete_during_spawn(self):
        # instance should not be rescheduled if instance is deleted
        # during the build
        self.mox.StubOutWithMock(self.compute, '_spawn')
        self.mox.StubOutWithMock(self.compute, '_reschedule_or_error')

        exc = exception.UnexpectedTaskStateError(expected=task_states.SPAWNING,
                actual=task_states.DELETING)
        self.compute._spawn(mox.IgnoreArg(), self.instance, mox.IgnoreArg(),
                mox.IgnoreArg(), mox.IgnoreArg(), mox.IgnoreArg(),
                mox.IgnoreArg(), set_access_ip=False).AndRaise(exc)

        self.mox.ReplayAll()
        # test succeeds if mocked method '_reschedule_or_error' is not
        # called.
        self.compute._run_instance(self.context, None, {}, None, None, None,
                False, None, self.instance)

    def test_no_reschedule_on_unexpected_task_state(self):
        # instance shouldn't be rescheduled if unexpected task state arises.
        # the exception should get reraised.
        self.mox.StubOutWithMock(self.compute, '_spawn')
        self.mox.StubOutWithMock(self.compute, '_reschedule_or_error')

        exc = exception.UnexpectedTaskStateError(expected=task_states.SPAWNING,
                actual=task_states.SCHEDULING)
        self.compute._spawn(mox.IgnoreArg(), self.instance, mox.IgnoreArg(),
                mox.IgnoreArg(), mox.IgnoreArg(), mox.IgnoreArg(),
                mox.IgnoreArg(), set_access_ip=False).AndRaise(exc)

        self.mox.ReplayAll()
        self.assertRaises(exception.UnexpectedTaskStateError,
                self.compute._run_instance, self.context, None, {}, None, None,
                None, False, None, self.instance)


class ComputeRescheduleResizeOrReraiseTestCase(BaseTestCase):
    """Test logic and exception handling around rescheduling prep resize
    requests
    """
    def setUp(self):
        super(ComputeRescheduleResizeOrReraiseTestCase, self).setUp()
        self.instance = self._create_fake_instance()
        self.instance_uuid = self.instance['uuid']
        self.instance_type = flavors.get_flavor_by_name(
                "m1.tiny")

    def test_reschedule_resize_or_reraise_called(self):
        """Verify the rescheduling logic gets called when there is an error
        during prep_resize.
        """
        self.mox.StubOutWithMock(self.compute.db, 'migration_create')
        self.mox.StubOutWithMock(self.compute, '_reschedule_resize_or_reraise')

        self.compute.db.migration_create(mox.IgnoreArg(),
                mox.IgnoreArg()).AndRaise(test.TestingException("Original"))

        self.compute._reschedule_resize_or_reraise(mox.IgnoreArg(), None,
                self.instance, mox.IgnoreArg(), self.instance_type, None, None,
                None)

        self.mox.ReplayAll()

        self.compute.prep_resize(self.context, None, self.instance,
                self.instance_type)

    def test_reschedule_fails_with_exception(self):
        """Original exception should be raised if the _reschedule method
        raises another exception
        """
        method_args = (None, self.instance, self.instance_type, None, None,
                None)
        self.mox.StubOutWithMock(self.compute, "_reschedule")

        self.compute._reschedule(self.context, None, None, self.instance_uuid,
                self.compute.scheduler_rpcapi.prep_resize, method_args,
                task_states.RESIZE_PREP).AndRaise(
                        InnerTestingException("Inner"))
        self.mox.ReplayAll()

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            self.assertRaises(test.TestingException,
                    self.compute._reschedule_resize_or_reraise, self.context,
                    None, self.instance, exc_info, self.instance_type, None,
                    {}, {})

    def test_reschedule_false(self):
        """Original exception should be raised if the resize is not
        rescheduled.
        """
        method_args = (None, self.instance, self.instance_type, None, None,
                None)
        self.mox.StubOutWithMock(self.compute, "_reschedule")

        self.compute._reschedule(self.context, None, None, self.instance_uuid,
                self.compute.scheduler_rpcapi.prep_resize, method_args,
                task_states.RESIZE_PREP).AndReturn(False)
        self.mox.ReplayAll()

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            self.assertRaises(test.TestingException,
                    self.compute._reschedule_resize_or_reraise, self.context,
                    None, self.instance, exc_info, self.instance_type, None,
                    {}, {})

    def test_reschedule_true(self):
        # If rescheduled, the original resize exception should be logged.
        method_args = (self.instance, self.instance_type, None, {}, {}, None)
        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()

            self.mox.StubOutWithMock(self.compute, "_reschedule")
            self.mox.StubOutWithMock(self.compute, "_log_original_error")
            self.compute._reschedule(self.context, {}, {},
                    self.instance_uuid,
                    self.compute.scheduler_rpcapi.prep_resize, method_args,
                    task_states.RESIZE_PREP, exc_info).AndReturn(True)

            self.compute._log_original_error(exc_info, self.instance_uuid)
            self.mox.ReplayAll()

            self.compute._reschedule_resize_or_reraise(self.context, None,
                    self.instance, exc_info, self.instance_type, None, {}, {})


class ComputeInactiveImageTestCase(BaseTestCase):
    def setUp(self):
        super(ComputeInactiveImageTestCase, self).setUp()

        def fake_show(meh, context, id):
            return {'id': id, 'min_disk': None, 'min_ram': None,
                    'name': 'fake_name',
                    'status': 'deleted',
                    'properties': {'kernel_id': 'fake_kernel_id',
                                   'ramdisk_id': 'fake_ramdisk_id',
                                   'something_else': 'meow'}}

        fake_image.stub_out_image_service(self.stubs)
        self.stubs.Set(fake_image._FakeImageService, 'show', fake_show)
        self.compute_api = compute.API()

    def test_create_instance_with_deleted_image(self):
        # Make sure we can't start an instance with a deleted image.
        inst_type = flavors.get_flavor_by_name('m1.tiny')
        self.assertRaises(exception.ImageNotActive,
                          self.compute_api.create,
                          self.context, inst_type, 'fake-image-uuid')


class EvacuateHostTestCase(BaseTestCase):
    def setUp(self):
        super(EvacuateHostTestCase, self).setUp()
        self.inst_ref = jsonutils.to_primitive(self._create_fake_instance
                                          ({'host': 'fake_host_2'}))
        db.instance_update(self.context, self.inst_ref['uuid'],
                           {"task_state": task_states.REBUILDING})

    def tearDown(self):
        db.instance_destroy(self.context, self.inst_ref['uuid'])
        super(EvacuateHostTestCase, self).tearDown()

    def _rebuild(self, on_shared_storage=True):
        orig_image_ref = None
        image_ref = None
        injected_files = None
        self.compute.rebuild_instance(
                self.context, self.inst_ref, orig_image_ref, image_ref,
                injected_files, 'newpass', recreate=True,
                on_shared_storage=on_shared_storage)

    def test_rebuild_on_host_updated_target(self):
        """Confirm evacuate scenario updates host."""
        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.mox.ReplayAll()

        self._rebuild()

        # Should be on destination host
        instance = db.instance_get(self.context, self.inst_ref['id'])
        self.assertEqual(instance['host'], self.compute.host)

    def test_rebuild_with_instance_in_stopped_state(self):
        """Confirm evacuate scenario updates vm_state to stopped
        if instance is in stopped state
        """
        #Initialize the VM to stopped state
        db.instance_update(self.context, self.inst_ref['uuid'],
                           {"vm_state": vm_states.STOPPED})
        self.inst_ref['vm_state'] = vm_states.STOPPED

        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.mox.ReplayAll()

        self._rebuild()

        #Check the vm state is reset to stopped
        instance = db.instance_get(self.context, self.inst_ref['id'])
        self.assertEqual(instance['vm_state'], vm_states.STOPPED)

    def test_rebuild_with_wrong_shared_storage(self):
        """Confirm evacuate scenario does not update host."""
        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.mox.ReplayAll()

        self.assertRaises(exception.InvalidSharedStorage,
                          lambda: self._rebuild(on_shared_storage=False))

        # Should remain on original host
        instance = db.instance_get(self.context, self.inst_ref['id'])
        self.assertEqual(instance['host'], 'fake_host_2')

    def test_rebuild_on_host_with_volumes(self):
        """Confirm evacuate scenario reconnects volumes."""
        values = {'instance_uuid': self.inst_ref['uuid'],
                  'device_name': '/dev/vdc',
                  'delete_on_termination': False,
                  'volume_id': 'fake_volume_id'}

        db.block_device_mapping_create(self.context, values)

        def fake_volume_get(self, context, volume):
            return {'id': 'fake_volume_id'}
        self.stubs.Set(cinder.API, "get", fake_volume_get)

        # Stub out and record whether it gets detached
        result = {"detached": False}

        def fake_detach(self, context, volume):
            result["detached"] = volume["id"] == 'fake_volume_id'
        self.stubs.Set(cinder.API, "detach", fake_detach)

        def fake_terminate_connection(self, context, volume, connector):
            return {}
        self.stubs.Set(cinder.API, "terminate_connection",
                       fake_terminate_connection)

        # make sure volumes attach, detach are called
        self.mox.StubOutWithMock(self.compute.volume_api, 'detach')
        self.compute.volume_api.detach(mox.IsA(self.context), mox.IgnoreArg())

        self.mox.StubOutWithMock(self.compute, '_setup_block_device_mapping')
        self.compute._setup_block_device_mapping(mox.IsA(self.context),
                                                 mox.IsA(self.inst_ref),
                                                 mox.IgnoreArg())

        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.mox.ReplayAll()

        self._rebuild()

        # cleanup
        for bdms in db.block_device_mapping_get_all_by_instance(
            self.context, self.inst_ref['uuid']):
            db.block_device_mapping_destroy(self.context, bdms['id'])

    def test_rebuild_on_host_with_shared_storage(self):
        """Confirm evacuate scenario on shared storage."""
        self.mox.StubOutWithMock(self.compute.driver, 'spawn')
        self.compute.driver.spawn(mox.IsA(self.context),
                mox.IsA(self.inst_ref), {}, mox.IgnoreArg(), 'newpass',
                network_info=mox.IgnoreArg(),
                block_device_info=mox.IgnoreArg())

        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.mox.ReplayAll()

        self._rebuild()

    def test_rebuild_on_host_without_shared_storage(self):
        """Confirm evacuate scenario without shared storage
        (rebuild from image)
        """
        fake_image = {'id': 1,
                      'name': 'fake_name',
                      'properties': {'kernel_id': 'fake_kernel_id',
                                     'ramdisk_id': 'fake_ramdisk_id'}}

        self.mox.StubOutWithMock(self.compute.driver, 'spawn')
        self.compute.driver.spawn(mox.IsA(self.context),
                mox.IsA(self.inst_ref), mox.IsA(fake_image), mox.IgnoreArg(),
                mox.IsA('newpass'), network_info=mox.IgnoreArg(),
                block_device_info=mox.IgnoreArg())

        self.stubs.Set(self.compute.driver, 'instance_on_disk',
                       lambda x: False)
        self.mox.ReplayAll()

        self._rebuild(on_shared_storage=False)

    def test_rebuild_on_host_instance_exists(self):
        """Rebuild if instance exists raises an exception."""
        db.instance_update(self.context, self.inst_ref['uuid'],
                           {"task_state": task_states.SCHEDULING})
        self.compute.run_instance(self.context, instance=self.inst_ref)

        self.stubs.Set(self.compute.driver, 'instance_on_disk', lambda x: True)
        self.assertRaises(exception.InstanceExists,
                          lambda: self._rebuild(on_shared_storage=True))

    def test_driver_doesnt_support_recreate(self):
        with utils.temporary_mutation(self.compute.driver.capabilities,
                                      supports_recreate=False):
            self.stubs.Set(self.compute.driver, 'instance_on_disk',
                           lambda x: True)
            self.assertRaises(exception.InstanceRecreateNotSupported,
                              lambda: self._rebuild(on_shared_storage=True))


class ComputeInjectedFilesTestCase(BaseTestCase):
    # Test that running instances with injected_files decodes files correctly

    def setUp(self):
        super(ComputeInjectedFilesTestCase, self).setUp()
        self.instance = self._create_fake_instance()
        self.stubs.Set(self.compute.driver, 'spawn', self._spawn)

    def _spawn(self, context, instance, image_meta, injected_files,
            admin_password, nw_info, block_device_info):
        self.assertEqual(self.expected, injected_files)

    def _test(self, injected_files, decoded_files):
        self.expected = decoded_files
        self.compute.run_instance(self.context, self.instance,
                                  injected_files=injected_files)

    def test_injected_none(self):
        # test an input of None for injected_files
        self._test(None, [])

    def test_injected_empty(self):
        # test an input of [] for injected_files
        self._test([], [])

    def test_injected_success(self):
        # test with valid b64 encoded content.
        injected_files = [
            ('/a/b/c', base64.b64encode('foobarbaz')),
            ('/d/e/f', base64.b64encode('seespotrun')),
        ]

        decoded_files = [
            ('/a/b/c', 'foobarbaz'),
            ('/d/e/f', 'seespotrun'),
        ]
        self._test(injected_files, decoded_files)

    def test_injected_invalid(self):
        # test with invalid b64 encoded content
        injected_files = [
            ('/a/b/c', base64.b64encode('foobarbaz')),
            ('/d/e/f', 'seespotrun'),
        ]

        self.assertRaises(exception.Base64Exception, self.compute.run_instance,
                self.context, self.instance, injected_files=injected_files)

    def test_reschedule(self):
        # test that rescheduling is done with original encoded files
        expected = [
            ('/a/b/c', base64.b64encode('foobarbaz')),
            ('/d/e/f', base64.b64encode('seespotrun')),
        ]

        def _roe(context, instance, exc_info, requested_networks,
                 admin_password, injected_files, is_first_time, request_spec,
                 filter_properties, bdms=None):
            self.assertEqual(expected, injected_files)
            return True

        def spawn_explode(context, instance, image_meta, injected_files,
                admin_password, nw_info, block_device_info):
            # force reschedule logic to execute
            raise test.TestingException(_("spawn error"))

        self.stubs.Set(self.compute.driver, 'spawn', spawn_explode)
        self.stubs.Set(self.compute, '_reschedule_or_error', _roe)

        self.compute.run_instance(self.context, self.instance,
                                  injected_files=expected)


class CheckConfigDriveTestCase(test.TestCase):
    # NOTE(sirp): `TestCase` is far too heavyweight for this test, this should
    # probably derive from a `test.FastTestCase` that omits DB and env
    # handling
    def setUp(self):
        super(CheckConfigDriveTestCase, self).setUp()
        self.compute_api = compute.API()
        self.context = context.RequestContext(
                'fake_user_id', 'fake_project_id')

        self.called = called = {'show': False}

        def fake_get_remote_image_service(context, image_id):
            class FakeGlance(object):
                def show(self, context, image_id):
                    called['show'] = True

            return FakeGlance(), image_id

        self.stubs.Set(glance, 'get_remote_image_service',
                       fake_get_remote_image_service)

    def tearDown(self):
        self.stubs.UnsetAll()
        super(CheckConfigDriveTestCase, self).tearDown()

    def assertCheck(self, expected, config_drive):
        self.assertEqual(expected,
                         self.compute_api._check_config_drive(
                             self.context, config_drive))

    def test_value_is_none(self):
        self.assertFalse(self.called['show'])
        self.assertCheck((None, None), None)
        self.assertFalse(self.called['show'])

    def test_bool_string_or_id(self):
        self.assertCheck((None, True), "true")
        self.assertCheck((None, True), 1)
        self.assertCheck((None, True), 't')

    def test_value_is_image_id(self):
        self.assertCheck(("fake-uuid", None), "fake-uuid")


class CheckRequestedImageTestCase(test.TestCase):
    def setUp(self):
        super(CheckRequestedImageTestCase, self).setUp()
        self.compute_api = compute.API()
        self.context = context.RequestContext(
                'fake_user_id', 'fake_project_id')

        self.instance_type = flavors.get_default_flavor()
        self.instance_type['memory_mb'] = 64
        self.instance_type['root_gb'] = 1

    def test_no_image_specified(self):
        self.compute_api._check_requested_image(self.context, None, {},
                self.instance_type)

    def test_image_status_must_be_active(self):
        image = dict(id='123', status='foo')

        self.assertRaises(exception.ImageNotActive,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type)

        image['status'] = 'active'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)

    def test_image_min_ram_check(self):
        image = dict(id='123', status='active', min_ram='65')

        self.assertRaises(exception.InstanceTypeMemoryTooSmall,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type)

        image['min_ram'] = '64'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)

    def test_image_min_disk_check(self):
        image = dict(id='123', status='active', min_disk='2')

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type)

        image['min_disk'] = '1'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)

    def test_image_too_large(self):
        image = dict(id='123', status='active', size='1073741825')

        self.assertRaises(exception.InstanceTypeDiskTooSmall,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type)

        image['size'] = '1073741824'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)

    def test_root_gb_zero_disables_size_check(self):
        self.instance_type['root_gb'] = 0
        image = dict(id='123', status='active', size='1073741825')

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)

    def test_root_gb_zero_disables_min_disk(self):
        self.instance_type['root_gb'] = 0
        image = dict(id='123', status='active', min_disk='2')

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type)


class ComputeAPIClassNameTestCase(test.TestCase):
    def setUp(self):
        super(ComputeAPIClassNameTestCase, self).setUp()

    def test_default_compute_api_class_name(self):
        result = compute.get_compute_api_class_name()
        self.assertEqual('nova.compute.api.API', result)

    def test_cell_compute_api_class_name(self):
        self.flags(enable=True, group='cells')
        self.flags(cell_type='api', group='cells')
        result = compute.get_compute_api_class_name()
        self.assertEqual('nova.compute.cells_api.ComputeCellsAPI', result)
        self.flags(cell_type='compute', group='cells')
        result = compute.get_compute_api_class_name()
        self.assertEqual('nova.compute.api.API', result)

    def test_illegal_cell_compute_api_class_name(self):
        self.flags(enable=True, group='cells')
        self.flags(cell_type='fake_cell_type', group='cells')
        self.assertRaises(exception.InvalidInput,
                          compute.get_compute_api_class_name)
