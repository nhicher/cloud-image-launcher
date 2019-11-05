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


class CloudImgLauncher:

    def __init__(self):
        self.conn = libvirt.open('qemu:///system')
        self.images = {
            'centos7': {
                'url': 'https://cloud.centos.org/centos/7/images/CentOS-7-x86_64-GenericCloud.qcow2',
                'image': 'CentOS-7-x86_64-GenericCloud.qcow2',
                'distro': 'fedora',
                'os': 'centos7.0'
            },
            'fedora31': {
                'url': 'https://download.fedoraproject.org/pub/fedora/linux/releases/31/Cloud/x86_64/images/Fedora-Cloud-Base-31-1.9.x86_64.raw.xz',
                'image': 'Fedora-Cloud-Base-31-1.9.x86_64.qcow2',
                'distro': 'centos',
                'os': 'fedora30'
            },
        }
        self.images_path = "/var/lib/libvirt/images"

    def parse_arguments(self, args=sys.argv[1:]):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(
            title='subcommands',
            description='valid subcommands',
            help='additional help',
            dest="command")
        parser_create = subparsers.add_parser(
            'create', help='Create VM')
        parser_destroy = subparsers.add_parser(
            'destroy', help='Destroy VM')
        parser_fetch = subparsers.add_parser(
            'fetch', help='Fetch distro image from Internet')

        parser_fetch.add_argument(
            '-d', '--distribution', type=str, default='fedora31',
            help='distribution image to fetch')

        parser_destroy.add_argument(
            '-n', '--hostname', type=str,
            help='define hostname')

        parser_create.add_argument(
            '-p', '--pub_key_path', type=str,
            help='path to the pub key path')
        parser_create.add_argument(
            '-n', '--hostname', type=str,
            help='define hostname')
        parser_create.add_argument(
            '-d', '--distribution', type=str, default='fedora31',
            help='distribution to use')
        parser_create.add_argument(
            '-m', '--memory', type=str, default='4096',
            help='size in mega for memory')
        parser.add_argument(
            '--verbose', action='store_true',
            help='verbose output')
        self.args = parser.parse_args()
        if not self.args.command:
            parser.print_help()
            sys.exit(0)

    def get_key(self):
        return open(os.path.expanduser(self.args.pub_key_path)).read()

    def manage_log(self):
        self.logger = logging.getLogger()
        self.logger.setLevel(logging.DEBUG)

        # create file handler logs even debug messages
        file_handler = RotatingFileHandler(
            '/var/log/cloudimg-launcher.log', 'a', 300)
        file_handler.setLevel(logging.DEBUG)

        # create console handler with a higher log level
        console_handler = logging.StreamHandler()
        if self.args.verbose:
            console_handler.setLevel(logging.DEBUG)
        else:
            console_handler.setLevel(logging.INFO)

        # create formatter and add it to the handlers
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        formatter = logging.Formatter(log_format)
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        # add the handlers to the logger
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def execute(self, argv, output=None):
        self.logger.debug("Running %s" % argv)
        stderr_output = subprocess.DEVNULL if output == 'devnull' else None

        try:
            subprocess.check_output(argv, stderr=stderr_output)
            return True
        except subprocess.CalledProcessError as e:
            if output == 'devnull':
                return False
            else:
                self.logger.debug(e.output)

    def is_instance(self):
        self.logger.info('check if instance %s exist' % self.args.hostname)
        try:
            self.conn.lookupByName(self.args.hostname)
            is_instance = True
        except libvirt.libvirtError:
            is_instance = False
        except Exception:
            self.logger.exception("Unexpected error: ")
            raise
        finally:
            return is_instance

    def _create_image(self):
        if not self.args.distribution:
            print('you must specify a distribution')
            sys.exit(0)

        base_image = os.path.join(
            self.images_path, self.images[self.args.distribution]['image'])
        disk = os.path.join(self.images_path, self.args.hostname)

        command = ['sudo', 'qemu-img', 'create', '-q', '-f', 'qcow2', '-b',
                   base_image, disk]
        self.execute(command, output='devnull')
        self.logger.info('%s created' % disk)

    def _create_cloud_init_config(self):
        self.logger.info('create cloud-init config file')

        loader = FileSystemLoader(os.path.dirname(os.path.realpath(__file__)))
        env = Environment(trim_blocks=True, loader=loader)

        domain = 'localdomain'
        distro = self.images[self.args.distribution]['distro']

        for config_file in ['user-data', 'meta-data']:
            j2 = os.path.join("distros", distro, config_file + ".j2")
            dest = "/tmp/%s" % config_file
            template = env.get_template(j2)
            result = template.render({'hostname': self.args.hostname,
                                      'domain': domain,
                                      'pub_key': self.get_key()})
            with open(dest, "w") as fh:
                fh.write(result)

        iso = "/var/lib/libvirt/images/%s.iso" % self.args.hostname
        command = ['sudo', 'genisoimage', '-output', iso, '-volid',
                   'cidata', '-joliet', '-rock', '/tmp/user-data',
                   '/tmp/meta-data']
        self.execute(command, output='devnull')

        self.logger.info('cloud-init %s created' % iso)

    def _create_instance(self):
        os_variant = self.images[self.args.distribution]['os']
        iso = "/var/lib/libvirt/images/%s.iso" % self.args.hostname
        disk = os.path.join(self.images_path, self.args.hostname)
        command = ['sudo', 'virt-install',
                   '--connect=qemu:///system',
                   '--accelerate',
                   '--boot', 'hd',
                   '--noautoconsole',
                   '--graphics', 'vnc',
                   '--disk', disk,
                   '--disk', 'path=%s,device=cdrom' % iso,
                   '--network', 'bridge=virbr0,model=virtio',
                   '--os-variant', os_variant,
                   '--vcpus=4',
                   '--cpu', 'host',
                   '--ram', self.args.memory,
                   '--name', self.args.hostname]
        self.execute(command)
        self.logger.info('To get the IP address check the DHCP leases')
        self.logger.info(
            'sudo virsh --connect=qemu:///system net-dhcp-leases default')

    def create(self):
        if self.is_instance():
            self.logger.info(
                'instance %s already exists' % self.args.hostname)
            return

        self.logger.info('creating instance %s' % self.args.hostname)
        self._create_image()
        self._create_cloud_init_config()
        self._create_instance()

    def destroy(self):
        if not self.is_instance():
            self.logger.info(
                'instance %s does not exists' % self.args.hostname)
            return
        self.logger.info('destroy instance %s' % self.args.hostname)

        dom = self.conn.lookupByName(self.args.hostname)
        if dom.isActive():
            dom.destroy()
        command = ['sudo', 'virsh', '--quiet', 'undefine',
                   self.args.hostname, '--snapshots-metadata',
                   '--remove-all-storage']
        self.execute(command)
        self.logger.info('instance %s destroyed' % self.args.hostname)

    def fetch(self):
        if not self.args.distribution:
            self.logger.info(
                "Please provide the distribution to download")
            sys.exit(1)
        base_image = os.path.join(
            self.images_path, self.images[self.args.distribution]['image'])
        url = self.images[self.args.distribution]['url']
        command = ['curl', '-o', base_image, '-L', url]
        self.execute(command)

    def main(self):
        self.parse_arguments()
        self.manage_log()
        getattr(self, self.args.command)()
        self.conn.close()


if __name__ == '__main__':
    CloudImgLauncher().main()
