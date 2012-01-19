# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
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
#    under the License

import webob.exc

from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova.api.openstack import extensions
from nova import flags
from nova import log as logging
from nova import network
from nova import rpc


LOG = logging.getLogger('nova.api.openstack.compute.contrib.certificates')
FLAGS = flags.FLAGS


def _translate_certificate_view(certificate, private_key=None):
    return {
        'data': certificate,
        'private_key': private_key,
    }


class CertificatesController(object):
    """The Floating IP Pool API controller for the OpenStack API."""

    def __init__(self):
        self.network_api = network.API()
        super(CertificatesController, self).__init__()

    def show(self, req, id):
        """Return a list of certificates."""
        context = req.environ['nova.context']
        if id != 'root':
            msg = _("Only root certificate can be retrieved.")
            raise webob.exc.HTTPNotImplemented(explanation=msg)
        cert = rpc.call(context, FLAGS.cert_topic,
                        {"method": "fetch_ca",
                         "args": {"project_id": context.project_id}})
        return {'certificate': _translate_certificate_view(cert)}

    def create(self, req, body=None):
        """Return a list of certificates."""
        context = req.environ['nova.context']
        pk, cert = rpc.call(context, FLAGS.cert_topic,
                            {"method": "generate_x509_cert",
                             "args": {"user_id": context.user_id,
                                      "project_id": context.project_id}})
        context = req.environ['nova.context']
        return {'certificate': _translate_certificate_view(cert, pk)}


def make_certificate(elem):
    elem.set('data')
    elem.set('private_key')


class CertificateTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('certificate',
                                       selector='certificate')
        make_certificate(root)
        return xmlutil.MasterTemplate(root, 1)


class CertificatesTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('certificates')
        elem = xmlutil.SubTemplateElement(root, 'certificate',
                                          selector='certificates')
        make_certificate(elem)
        return xmlutil.MasterTemplate(root, 1)


class CertificatesSerializer(xmlutil.XMLTemplateSerializer):
    def index(self):
        return CertificatesTemplate()


class Certificates(extensions.ExtensionDescriptor):
    """Certificates support"""

    name = "Certificates"
    alias = "os-certificates"
    namespace = \
        "http://docs.openstack.org/compute/ext/certificates/api/v1.1"
    updated = "2012-01-19T00:00:00+00:00"

    def get_resources(self):
        resources = []

        body_serializers = {
            'application/xml': CertificatesSerializer(),
            }

        serializer = wsgi.ResponseSerializer(body_serializers)

        res = extensions.ResourceExtension('os-certificates',
                         CertificatesController(),
                         serializer=serializer,
                         member_actions={})
        resources.append(res)

        return resources
