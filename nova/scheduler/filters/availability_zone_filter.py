# Copyright (c) 2011-2012 Openstack, LLC.
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


import abstract_filter

from nova import flags

# NOTE(vish): move the flag here once we kill simple scheduler
flags.DECLARE('default_schedule_zone', 'nova.scheduler.simple')
FLAGS = flags.FLAGS

class AllHostsFilter(abstract_filter.AbstractHostFilter):
    """Filters Hosts by availabilty zone."""

    def host_passes(self, host_state, filter_properties):
        context = filter_properties['context']
        spec = filter_properties['request_spec']
        instance_properties = spec.get('instance_properties')
        availability_zone = instance_properties.get('availability_zone')
        zone, host = FLAGS.default_schedule_zone, None
        if availability_zone:
            zone, _x, host = availability_zone.partition(':')

        if host and context.is_admin:
            return host == host_state.host
        if zone:
            return zone == host_state.service['availability_zone']
        return True
