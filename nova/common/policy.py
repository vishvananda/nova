# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 OpenStack, LLC.
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

"""Common Policy Engine Implementation"""

import json
import urllib
import urllib2


class NotAllowed(Exception):
        pass


def enforce(match_list, target_dict, credentials_dict):
    """Check the authz of some rules against credentials.

    Match lists look like:

        ('rule:compute:get_volume',)

    or

        (('role:compute_admin',),
         ('tenant_id:%(tenant_id)s', 'role:compute_sysadmin'))


    Target dicts contain as much information as we can about the object being
    operated on.

    Credentials dicts contain as much information as we can about the user
    performing the action.

    """
    b = HttpBrain()
    if not b.check(match_list, target_dict, credentials_dict):
        raise NotAllowed()


class Brain(object):
    # class level on purpose, the brain is global
    rules = {}

    def __init__(self, rules=None):
        if rules is not None:
            self.__class__.rules = rules

    def add_rule(self, key, match):
        self.rules[key] = match

    def check(self, match_list, target_dict, cred_dict):
        if not match_list:
            return True
        for and_list in match_list:
            matched = False
            if isinstance(and_list, basestring):
                and_list = (and_list,)
            for match in and_list:
                match_kind, match_value = match.split(':', 1)
                if hasattr(self, '_check_%s' % match_kind):
                    f = getattr(self, '_check_%s' % match_kind)
                    rv = f(match_value, target_dict, cred_dict)
                    if not rv:
                        matched = False
                        break
                else:
                    rv = self._check_generic(match, target_dict, cred_dict)
                    if not rv:
                        matched = False
                        break
                matched = True

            # all AND matches passed
            if matched:
                return True

        # no OR rules matched
        return False

    def _check_rule(self, match, target_dict, cred_dict):
        new_match_list = self.rules.get(match)
        if new_match_list is None:
            return False
        return self.check(new_match_list, target_dict, cred_dict)

    def _check_role(self, match, target_dict, cred_dict):
        if cred_dict['is_admin']:
            return True
        return match in cred_dict['roles']

    def _check_generic(self, match, target_dict, cred_dict):
        """Check an individual match.

        Matches look like:

            tenant:%(tenant_id)s
            role:compute:admin

        """

        # TODO(termie): do dict inspection via dot syntax
        match = match % target_dict
        key, value = match.split(':', 1)
        if key in cred_dict:
            return value == cred_dict[key]
        return False


class HttpBrain(Brain):
    """A brain that can check external urls a

    Posts json blobs for target and credentials.

    """

    def _check_http(self, match, target_dict, cred_dict):
        url = match % target_dict
        data = {'target': json.dumps(target_dict),
                'credentials': json.dumps(cred_dict)}
        post_data = urllib.urlencode(data)
        f = urllib2.urlopen(url, post_data)
        # NOTE(vish): This is to show how we could do remote requests,
        #             but some fancier method for response codes should
        #             probably be defined
        if f.read() == "True":
            return True
        return False


def load_json(path):
    rules_dict = json.load(open(path))
    b = HttpBrain(rules=rules_dict)
