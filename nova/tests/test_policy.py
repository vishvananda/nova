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
    
    def test_enforce_bad_action_throws(self):
        context = {}
        action = "example:denied"
        target = {}
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, context, action, target)    
        
    def test_enforce_good_action(self):
        context = {}
        action = "example:allowed"
        target = {}
        result = policy.enforce(context, action, target)
        self.assertEqual(result, None)
    
    def test_enforce_http_check(self):
        action = "example:get_google"
        context = {}
        target = {}
        result = policy.enforce(context, action, target)
        self.assertEqual(result, None)
    
    def test_templatized_enforcement(self):
        context = {'tenant_id' : 'bob'}
        target_mine = {'tenant_id' : 'bob'}
        target_not_mine = {'tenant_id' : 'fred'}
        action = "example:my_file"
        result = policy.enforce(context, action, target_mine)
        self.assertEqual(result, None)
        self.assertRaises(exception.PolicyNotAllowed, policy.enforce, context, action, target_not_mine)