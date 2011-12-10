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

from nova import test
from nova import policy
from nova import exception


class PolicyCheckTestCase(test.TestCase):
    def setUp(self):
        super(PolicyCheckTestCase, self).setUp()
        self.context = {'tenant_id' : 'bob'}
        self.target = {}

    def test_enforce_nonexistent_action_throws(self):
        action = "example:noexist"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, self.context, action, self.target)

    def test_enforce_bad_action_throws(self):
        action = "example:denied"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, self.context, action, self.target)    

    def test_enforce_good_action(self):
        action = "example:allowed"
        policy.enforce(self.context, action, self.target)
    
    def test_enforce_http_check(self):
        action = "example:get_google"
        policy.enforce(self.context, action, self.target)

    def test_enforce_http_check(self):
        action = "example:get_google"
        context = {}
        target = {}
        result = policy.enforce(context, action, target)
        self.assertEqual(result, None)

    def test_templatized_enforcement(self):
        target_mine = {'tenant_id' : 'bob'}
        target_not_mine = {'tenant_id' : 'fred'}
        action = "example:my_file"
        policy.enforce(self.context, action, target_mine)
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, self.context, action, target_not_mine)
    
    def test_early_AND_enforcement(self):
        action = "example:early_and_fail"
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, self.context, action, self.target)
    
    def test_early_OR_enforcement(self):
        action = "example:early_or_success"
        policy.enforce(self.context, action, self.target)
