# Copyright 2011 Eldar Nugaev
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

import datetime
import json

from nova.api.openstack import v2
from nova.api.openstack.v2 import extensions
from nova.api.openstack import wsgi
import nova.compute
from nova import test
from nova.tests.api.openstack import fakes
import nova.utils


dt = datetime.datetime.utcnow()


def fake_get_actions(self, _context, instance_uuid):
    return [
        {'action': 'rebuild', 'error': None, 'created_at': dt},
        {'action': 'reboot', 'error': 'Failed!', 'created_at': dt},
    ]


def fake_instance_get(self, _context, instance_uuid):
    return {'uuid': instance_uuid}


class ServerDiagnosticsTest(test.TestCase):

    def setUp(self):
        super(ServerDiagnosticsTest, self).setUp()
        self.flags(allow_admin_api=True)
        self.flags(verbose=True)
        self.stubs.Set(nova.compute.API, 'get_actions', fake_get_actions)
        self.stubs.Set(nova.compute.API, 'get', fake_instance_get)
        self.compute_api = nova.compute.API()

        self.router = v2.APIRouter()
        ext_middleware = extensions.ExtensionMiddleware(self.router)
        self.app = wsgi.LazySerializationMiddleware(ext_middleware)

    def test_get_actions(self):
        uuid = nova.utils.gen_uuid()
        req = fakes.HTTPRequest.blank('/fake/servers/%s/actions' % uuid)
        res = req.get_response(self.app)
        output = json.loads(res.body)
        expected = {'actions': [
            {'action': 'rebuild', 'error': None, 'created_at': str(dt)},
            {'action': 'reboot', 'error': 'Failed!', 'created_at': str(dt)},
        ]}
        self.assertEqual(output, expected)
