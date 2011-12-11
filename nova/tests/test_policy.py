# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 Piston Cloud Computing, Inc.
# All Rights Reserved.

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

"""Test of Policy Engine For Nova"""

import urllib2
import StringIO

from nova import context
from nova import exception
from nova import flags
from nova import policy
from nova import test
from nova import utils
from nova.common import policy as common_policy

FLAGS = flags.FLAGS


class PolicyFileTestCase(test.TestCase):
    def setUp(self):
        self.flags(policy_file='nova/tests/policy.json')



class PolicyTestCase(test.TestCase):
    def setUp(self):
        super(PolicyTestCase, self).setUp()
        # NOTE(vish): preload rules to circumvent reloading from file
        policy._load_if_modified(utils.find_config(FLAGS.policy_file))
        common_policy.Brain.rules = None
        rules = {
            "true" : [],
            "example:allowed" : [],
            "example:denied" : [["false:false"]],
            "example:get_http": [["http:http://www.example.com"]],
            "example:my_file": [["role:compute_admin"],
                                ["project_id:%(project_id)s"]],
            "example:early_and_fail" : [["false:false", "rule:true"]],
            "example:early_or_success" : [["rule:true"], ["false:false"]],
            "example:sysadmin_allowed" : [["role:sysadmin"]],
        }
        common_policy.HttpBrain(rules)
        self.context = context.RequestContext('fake', 'fake')
        self.admin_context = context.RequestContext('admin', 'fake', is_admin=True)
        self.target = {}

    def test_admin_has_all_roles(self):
        action = "example:sysadmin_allowed"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, self.target)
        policy.enforce(self.admin_context, action, self.target)

    def test_enforce_nonexistent_action_throws(self):
        action = "example:noexist"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, self.target)

    def test_enforce_bad_action_throws(self):
        action = "example:denied"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, self.target)

    def test_enforce_good_action(self):
        action = "example:allowed"
        policy.enforce(self.context, action, self.target)

    def test_enforce_http_true(self):

        def fakeurlopen(url, post_data):
            return StringIO.StringIO("True")
        self.stubs.Set(urllib2, 'urlopen', fakeurlopen)
        action = "example:get_http"
        target = {}
        result = policy.enforce(self.context, action, target)
        self.assertEqual(result, None)

    def test_enforce_http_false(self):

        def fakeurlopen(url, post_data):
            return StringIO.StringIO("False")
        self.stubs.Set(urllib2, 'urlopen', fakeurlopen)
        action = "example:get_http"
        target = {}
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, target)

    def test_templatized_enforcement(self):
        target_mine = {'project_id' : 'fake'}
        target_not_mine = {'project_id' : 'another'}
        action = "example:my_file"
        policy.enforce(self.context, action, target_mine)
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, target_not_mine)

    def test_early_AND_enforcement(self):
        action = "example:early_and_fail"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce,
                          self.context, action, self.target)

    def test_early_OR_enforcement(self):
        action = "example:early_or_success"
        policy.enforce(self.context, action, self.target)
