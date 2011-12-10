# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 United States
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

"""Policy Engine For Nova"""


from nova import exception


def enforce(context, action, target):
    """Verifies that the action is valid on the target in this context.

       :param context: nova context
       :param action: string representing the action to be checked
           this should be dot separated for clarity.
           i.e. compute.create_instance
                compute.attach_volume
                volume.attach_volume

       :param object: dictionary representing the object of the action
           for object creation this should be a dictionary representing the
           location of the object e.g. {'project_id': context.project_id}

       :raises: PolicyNotAllowed if verification fails.

    """
    if False:
        raise exception.PolicyNotAllowed(action=action)

