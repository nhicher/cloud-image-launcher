#!/usr/bin/env python3
import argparse
import libvirt
import logging
import os
import subprocess
import sys

from jinja2 import FileSystemLoader
from jinja2.environment import Environment
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ManageKvm:
    def __init__(self):
        self.conn = libvirt.open('qemu:///system')
        self.home = str(Path.home())

    def usage(self, args=sys.argv[1:]):
        parser = argparse.ArgumentParser()
        parser.add_argument('-n', '--hostname', type=str,
                            help='define hostname')
        parser.add_argument('-a', '--action', type=str,
                            help='action to execute',
                            choices=['create', 'destroy'])
        parser.add_argument('-d', '--distribution', type=str, default='fedora',
                            help='distribution to use',
                            choices=['centos', 'fedora'])
        parser.add_argument('-p', '--pub_key_path', type=str,
                            help='path to the pub key path')
        parser.add_argument('-m', '--memory', type=str, default='4096',
                            help='size in mega for memory')
        parser.add_argument('--verbose', action='store_true',
                            help='verbose output')
        self.args = parser.parse_args()

    def get_key(self):
        return open(os.path.expanduser(self.args.pub_key_path)).read()

    def manage_log(self):
        self.logger = logging.getLogger('cloud-image-launcher')
        self.logger.setLevel(logging.DEBUG)

        # create file handler with logs even debug messages
        file_handler = RotatingFileHandler('/tmp/cloud-image-launcher.log',
                                           'a', 300)
        file_handler.setLevel(logging.DEBUG)

        # create console handler with a higher log level
        console_handler = logging.StreamHandler()
        if self.args.verbose:
            console_handler.setLevel(logging.DEBUG)
        else:
            console_handler.setLevel(logging.ERROR)

        # create formatter and add it to the handlers
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        formatter = logging.Formatter(log_format)
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # add the handlers to the logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def execute(self, argv, output=None):
        self.logger.debug(f"Running {argv}")
        stderr_output = subprocess.DEVNULL if output == 'devnull' else None

        try:
            subprocess.check_output(argv, stderr=stderr_output)
            return True
        except subprocess.CalledProcessError as e:
            if output == 'devnull':
                return False
            else:
                self.logger.debug(e.output)

    def _check_if_instance(self):
        self.logger.info(f'check if instance {self.args.hostname} exist')
        try:
            self.conn.lookupByName(self.args.hostname)
            self.is_instance = True
        except libvirt.libvirtError:
            self.is_instance = False
        finally:
            return self.is_instance

    def _create_image(self):
        image = {
                 'centos': 'CentOS-7-x86_64-GenericCloud.qcow2',
                 'fedora': 'Fedora-Cloud-Base-31-1.9.x86_64.qcow2',
                }
        image_path = "/var/lib/libvirt/images"
        base_image = f'{image_path}/{image[self.args.distribution]}'
        self.disk = f'{image_path}/{self.args.hostname}.qcow2'
        self.logger.info(f'create disk {self.disk}')

        command = ['sudo', 'qemu-img', 'create', '-q', '-f', 'qcow2', '-b',
                   base_image, self.disk]
        print(command)
        self.execute(command, output='devnull')
        self.logger.info(f'{self.disk} created')

    def _create_cloud_init_config(self):
        self.logger.info('create cloud-init config file')

        loader = FileSystemLoader(os.path.dirname(os.path.realpath(__file__)))
        env = Environment(trim_blocks=True, loader=loader)

        domain = 'localdomain'
        dhcp = True
        for config_file in ['user-data', 'meta-data']:
            j2 = f"{config_file}.j2"
            dest = f"/tmp/{config_file}"
            template = env.get_template(j2)
            result = template.render({'dhcp': dhcp,
                                      'distribution': self.args.distribution,
                                      'pub_key': self.get_key(),
                                      'hostname': self.args.hostname,
                                      'domain': domain})
            with open(dest, "w") as fh:
                fh.write(result)

        self.iso = f"/var/lib/libvirt/images/{self.args.hostname}.iso"
        command = ['sudo', 'genisoimage', '-output', self.iso, '-volid',
                   'cidata', '-joliet', '-rock', '/tmp/user-data',
                   '/tmp/meta-data']
        self.execute(command, output='devnull')
        self.logger.info(f'cloud-init {self.iso} created')

    def _create_instance(self):
        command = ['sudo', 'virt-install',
                   '--connect=qemu:///system',
                   '--accelerate',
                   '--boot', 'hd',
                   '--noautoconsole',
                   '--graphics', 'vnc',
                   '--disk', self.disk,
                   '--disk', f'path={self.iso},device=cdrom',
                   '--network', 'bridge=virbr0,model=virtio',
                   '--cpu', 'host',
                   '--vcpus=2',
                   '--ram', self.args.memory,
                   '--name', self.args.hostname]
        self.execute(command)

    def _destroy_instance(self):
        hostname = self.args.hostname
        if self.is_instance:
            self.logger.info(f'destroy instance {hostname}')

            dom = self.conn.lookupByName(hostname)
            if dom.isActive():
                dom.destroy()
            command = ['sudo', 'virsh', '--quiet', 'undefine',
                       hostname, '--snapshots-metadata',
                       '--remove-all-storage']
            self.execute(command)
            self.logger.info(f'instance {hostname} destroyed')

    def create(self):
        if self.is_instance:
            self.logger.info(f'instance {self.args.hostname} exists')
        else:
            self.logger.info(f'creating instance {self.args.hostname}')
            self._create_image()
            self._create_cloud_init_config()
            self._create_instance()

    def destroy(self):
        self._destroy_instance()

    def main(self):
        self.usage()
        self.manage_log()
        self._check_if_instance()
        # dynamically call functions based on action
        getattr(self, '%s' % self.args.action)()
        self.conn.close()


if __name__ == '__main__':
    ManageKvm().main()
