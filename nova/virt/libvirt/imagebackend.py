# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Grid Dynamics
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

import abc
import contextlib
import os

from oslo.config import cfg

from nova import exception
from nova.openstack.common import excutils
from nova.openstack.common import fileutils
from nova.openstack.common import log as logging
from nova import utils
from nova.virt.disk import api as disk
from nova.virt import images
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import utils as libvirt_utils

__imagebackend_opts = [
    cfg.StrOpt('libvirt_images_type',
            default='default',
            help='VM Images format. Acceptable values are: raw, qcow2, lvm,'
                 ' default. If default is specified,'
                 ' then use_cow_images flag is used instead of this one.'),
    cfg.StrOpt('libvirt_images_volume_group',
            default=None,
            help='LVM Volume Group that is used for VM images, when you'
                 ' specify libvirt_images_type=lvm.'),
    cfg.BoolOpt('libvirt_sparse_logical_volumes',
            default=False,
            help='Create sparse logical volumes (with virtualsize)'
                 ' if this flag is set to True.'),
    cfg.IntOpt('libvirt_lvm_snapshot_size',
               default=1000,
               help='The amount of storage (in megabytes) to allocate for LVM'
                    ' snapshot copy-on-write blocks.'),
        ]

CONF = cfg.CONF
CONF.register_opts(__imagebackend_opts)
CONF.import_opt('base_dir_name', 'nova.virt.libvirt.imagecache')
CONF.import_opt('preallocate_images', 'nova.virt.driver')

LOG = logging.getLogger(__name__)


def create_live_container(live_file, disk_file, memory_file):
    disk_path, disk_file = os.path.split(disk_file)
    utils.execute('chmod', '666', memory_file, run_as_root=True)
    memory_path, memory_file = os.path.split(memory_file)
    utils.execute('tar', 'zcf', live_file, '-C', disk_path, disk_file,
                  '-C', memory_path, memory_file)


def extract_live_container(live_file, disk_file, memory_file, tmpdir=None):
    with utils.tempdir(dir=tmpdir) as tmp:
        utils.execute('tar', 'zxf', live_file, '-C', tmp)
        tmp_disk = os.path.join(tmp, 'disk')
        tmp_memory = os.path.join(tmp, 'memory')
        utils.execute('mv', '-f', tmp_disk, disk_file)
        utils.execute('mv', '-f', tmp_memory, memory_file)


class Image(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, source_type, driver_format, is_block_dev=False):
        """Image initialization.

        :source_type: block or file
        :driver_format: raw or qcow2
        :is_block_dev:
        """
        self.source_type = source_type
        self.driver_format = driver_format
        self.is_block_dev = is_block_dev
        self.preallocate = False

        # NOTE(mikal): We need a lock directory which is shared along with
        # instance files, to cover the scenario where multiple compute nodes
        # are trying to create a base file at the same time
        self.lock_path = os.path.join(CONF.instances_path, 'locks')

    @abc.abstractmethod
    def create_image(self, prepare_template, base, size, *args, **kwargs):
        """Create image from template.

        Contains specific behavior for each image type.

        :prepare_template: function, that creates template.
        Should accept `target` argument.
        :base: Template name
        :size: Size of created image in bytes
        """
        pass

    def libvirt_info(self, disk_bus, disk_dev, device_type, cache_mode,
            extra_specs):
        """Get `LibvirtConfigGuestDisk` filled for this image.

        :disk_dev: Disk bus device name
        :disk_bus: Disk bus type
        :device_type: Device type for this image.
        :cache_mode: Caching mode for this image
        :extra_specs: Instance type extra specs dict.
        """
        info = vconfig.LibvirtConfigGuestDisk()
        info.source_type = self.source_type
        info.source_device = device_type
        info.target_bus = disk_bus
        info.target_dev = disk_dev
        info.driver_cache = cache_mode
        info.driver_format = self.driver_format
        driver_name = libvirt_utils.pick_disk_driver_name(self.is_block_dev)
        info.driver_name = driver_name
        info.source_path = self.path

        tune_items = ['disk_read_bytes_sec', 'disk_read_iops_sec',
            'disk_write_bytes_sec', 'disk_write_iops_sec',
            'disk_total_bytes_sec', 'disk_total_iops_sec']
        # Note(yaguang): Currently, the only tuning available is Block I/O
        # throttling for qemu.
        if self.source_type in ['file', 'block']:
            for key, value in extra_specs.iteritems():
                scope = key.split(':')
                if len(scope) > 1 and scope[0] == 'quota':
                    if scope[1] in tune_items:
                        setattr(info, scope[1], value)
        return info


    def cache(self, fetch_func, filename, size=None, live=False, tmpdir=None,
              *args, **kwargs):
        """Creates image from template.

        Ensures that template and image not already exists.
        Ensures that base directory exists.
        Synchronizes on template fetching.

        :fetch_func: Function that creates the base image
                     Should accept `target` argument.
        :filename: Name of the file in the image directory
        :size: Size of created image in bytes (optional)
        :live: Whether image is a live container (optional)
        :tmpdir: Where to create tmpdir for live extraction (optional)
        """
        base_dir = os.path.join(CONF.instances_path, CONF.base_dir_name)
        if not os.path.exists(base_dir):
            fileutils.ensure_tree(base_dir)
        base = os.path.join(base_dir, filename)
        memory_file = '%s.memory' % base

        @utils.synchronized(filename, external=True, lock_path=self.lock_path)
        def call_if_not_exists(target, *args, **kwargs):
            if live and (not os.path.exists(target) or
                         not os.path.exists(memory_file)):
                live_file = '%s.live' % base
                if (not os.path.exists(live_file)):
                    fetch_func(target=live_file, *args, **kwargs)
                extract_live_container(live_file, target, memory_file,
                                       tmpdir=tmpdir)
            elif (not os.path.exists(target) or
                  (CONF.libvirt_images_type == "lvm" and
                   'ephemeral_size' in kwargs)):
                fetch_func(target=target, *args, **kwargs)

        if (not os.path.exists(self.path) or not os.path.exists(base)
            or (live and not os.path.exists(memory_file))):
            self.create_image(call_if_not_exists, base, size,
                              *args, **kwargs)

        if size and self.preallocate and self._can_fallocate():
            utils.execute('fallocate', '-n', '-l', size, self.path)
        return memory_file

    def _can_fallocate(self):
        """Check once per class, whether fallocate(1) is available,
           and that the instances directory supports fallocate(2).
        """
        can_fallocate = getattr(self.__class__, 'can_fallocate', None)
        if can_fallocate is None:
            _out, err = utils.trycmd('fallocate', '-n', '-l', '1',
                                     self.path + '.fallocate_test')
            fileutils.delete_if_exists(self.path + '.fallocate_test')
            can_fallocate = not err
            self.__class__.can_fallocate = can_fallocate
            if not can_fallocate:
                LOG.error('Unable to preallocate_images=%s at path: %s' %
                          (CONF.preallocate_images, self.path))
        return can_fallocate

    def snapshot_create(self):
        raise NotImplementedError()

    def snapshot_extract(self, target, out_format):
        raise NotImplementedError()

    def snapshot_delete(self):
        raise NotImplementedError()


class Raw(Image):
    def __init__(self, instance=None, disk_name=None, path=None,
                 snapshot_name=None):
        super(Raw, self).__init__("file", "raw", is_block_dev=False)

        self.path = (path or
                     os.path.join(libvirt_utils.get_instance_path(instance),
                                  disk_name))
        self.snapshot_name = snapshot_name
        self.preallocate = CONF.preallocate_images != 'none'
        self.correct_format()

    def correct_format(self):
        if os.path.exists(self.path):
            data = images.qemu_img_info(self.path)
            self.driver_format = data.file_format or 'raw'

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def copy_raw_image(base, target, size):
            libvirt_utils.copy_image(base, target)
            if size:
                disk.extend(target, size)

        generating = 'image_id' not in kwargs
        if generating:
            #Generating image in place
            prepare_template(target=self.path, *args, **kwargs)
        else:
            prepare_template(target=base, *args, **kwargs)
            if not os.path.exists(self.path):
                with fileutils.remove_path_on_error(self.path):
                    copy_raw_image(base, self.path, size)
        self.correct_format()

    def snapshot_create(self):
        pass

    def snapshot_extract(self, target, out_format):
        images.convert_image(self.path, target, out_format)

    def snapshot_delete(self):
        pass


class Qcow2(Image):
    def __init__(self, instance=None, disk_name=None, path=None,
                 snapshot_name=None):
        super(Qcow2, self).__init__("file", "qcow2", is_block_dev=False)

        self.path = (path or
                     os.path.join(libvirt_utils.get_instance_path(instance),
                                  disk_name))
        self.snapshot_name = snapshot_name
        self.preallocate = CONF.preallocate_images != 'none'

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def copy_qcow2_image(base, target, size):
            # TODO(pbrady): Consider copying the cow image here
            # with preallocation=metadata set for performance reasons.
            # This would be keyed on a 'preallocate_images' setting.
            libvirt_utils.create_cow_image(base, target)
            if size:
                disk.extend(target, size)

        # Download the unmodified base image (existence is checked in prepare)
        prepare_template(target=base, *args, **kwargs)

        legacy_backing_size = None
        legacy_base = base

        # Determine whether an existing qcow2 disk uses a legacy backing by
        # actually looking at the image itself and parsing the output of the
        # backing file it expects to be using.
        if os.path.exists(self.path):
            backing_path = libvirt_utils.get_disk_backing_file(self.path)
            backing_file = os.path.basename(backing_path)
            backing_parts = backing_file.rpartition('_')
            if backing_file != backing_parts[-1] and \
                    backing_parts[-1].isdigit():
                legacy_backing_size = int(backing_parts[-1])
                legacy_base += '_%d' % legacy_backing_size
                legacy_backing_size *= 1024 * 1024 * 1024

        # Create the legacy backing file if necessary.
        if legacy_backing_size:
            if not os.path.exists(legacy_base):
                with fileutils.remove_path_on_error(legacy_base):
                    libvirt_utils.copy_image(base, legacy_base)
                    disk.extend(legacy_base, legacy_backing_size)

        # NOTE(cfb): Having a flavor that sets the root size to 0 and having
        #            nova effectively ignore that size and use the size of the
        #            image is considered a feature at this time, not a bug.
        if size and size < disk.get_disk_size(base):
            LOG.error('%s virtual size larger than flavor root disk size %s' %
                      (base, size))
            raise exception.InstanceTypeDiskTooSmall()
        if not os.path.exists(self.path):
            with fileutils.remove_path_on_error(self.path):
                copy_qcow2_image(base, self.path, size)

    def snapshot_create(self):
        libvirt_utils.create_snapshot(self.path, self.snapshot_name)

    def snapshot_extract(self, target, out_format):
        libvirt_utils.extract_snapshot(self.path, 'qcow2',
                                       self.snapshot_name, target,
                                       out_format)

    def snapshot_delete(self):
        libvirt_utils.delete_snapshot(self.path, self.snapshot_name)


class Lvm(Image):
    @staticmethod
    def escape(filename):
        return filename.replace('_', '__')

    def __init__(self, instance=None, disk_name=None, path=None,
                 snapshot_name=None):
        super(Lvm, self).__init__("block", "raw", is_block_dev=True)

        if path:
            info = libvirt_utils.logical_volume_info(path)
            self.vg = info['VG']
            self.lv = info['LV']
            self.path = path
        else:
            if not CONF.libvirt_images_volume_group:
                raise RuntimeError(_('You should specify'
                                     ' libvirt_images_volume_group'
                                     ' flag to use LVM images.'))
            self.vg = CONF.libvirt_images_volume_group
            self.lv = '%s_%s' % (self.escape(instance['name']),
                                 self.escape(disk_name))
            self.path = os.path.join('/dev', self.vg, self.lv)

        # TODO(pbrady): possibly deprecate libvirt_sparse_logical_volumes
        # for the more general preallocate_images
        self.sparse = CONF.libvirt_sparse_logical_volumes
        self.preallocate = not self.sparse

        if snapshot_name:
            self.snapshot_name = snapshot_name
            self.snapshot_path = os.path.join('/dev', self.vg,
                                              self.snapshot_name)

    def _can_fallocate(self):
        return False

    def create_image(self, prepare_template, base, size, *args, **kwargs):
        @utils.synchronized(base, external=True, lock_path=self.lock_path)
        def create_lvm_image(base, size):
            base_size = disk.get_disk_size(base)
            resize = size > base_size
            size = size if resize else base_size
            libvirt_utils.create_lvm_image(self.vg, self.lv,
                                           size, sparse=self.sparse)
            images.convert_image(base, self.path, 'raw', run_as_root=True)
            if resize:
                disk.resize2fs(self.path, run_as_root=True)

        generated = 'ephemeral_size' in kwargs

        #Generate images with specified size right on volume
        if generated and size:
            libvirt_utils.create_lvm_image(self.vg, self.lv,
                                           size, sparse=self.sparse)
            with self.remove_volume_on_error(self.path):
                prepare_template(target=self.path, *args, **kwargs)
        else:
            prepare_template(target=base, *args, **kwargs)
            with self.remove_volume_on_error(self.path):
                create_lvm_image(base, size)

    @contextlib.contextmanager
    def remove_volume_on_error(self, path):
        try:
            yield
        except Exception:
            with excutils.save_and_reraise_exception():
                libvirt_utils.remove_logical_volumes(path)

    def snapshot_create(self):
        size = CONF.libvirt_lvm_snapshot_size
        cmd = ('lvcreate', '-L', size, '-s', '--name', self.snapshot_name,
               self.path)
        libvirt_utils.execute(*cmd, run_as_root=True, attempts=3)

    def snapshot_extract(self, target, out_format):
        images.convert_image(self.snapshot_path, target, out_format,
                             run_as_root=True)

    def snapshot_delete(self):
        # NOTE (rmk): Snapshot volumes are automatically zeroed by LVM
        cmd = ('lvremove', '-f', self.snapshot_path)
        libvirt_utils.execute(*cmd, run_as_root=True, attempts=3)


class Backend(object):
    def __init__(self, use_cow):
        self.BACKEND = {
            'raw': Raw,
            'qcow2': Qcow2,
            'lvm': Lvm,
            'default': Qcow2 if use_cow else Raw
        }

    def backend(self, image_type=None):
        if not image_type:
            image_type = CONF.libvirt_images_type
        image = self.BACKEND.get(image_type)
        if not image:
            raise RuntimeError(_('Unknown image_type=%s') % image_type)
        return image

    def image(self, instance, disk_name, image_type=None):
        """Constructs image for selected backend

        :instance: Instance name.
        :name: Image name.
        :image_type: Image type.
        Optional, is CONF.libvirt_images_type by default.
        """
        backend = self.backend(image_type)
        return backend(instance=instance, disk_name=disk_name)

    def snapshot(self, disk_path, snapshot_name, image_type=None):
        """Returns snapshot for given image

        :path: path to image
        :snapshot_name: snapshot name
        :image_type: type of image
        """
        backend = self.backend(image_type)
        return backend(path=disk_path, snapshot_name=snapshot_name)
