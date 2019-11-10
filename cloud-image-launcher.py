#!/usr/bin/env python3

import argparse
import libvirt
import logging
import os
import subprocess
import sys
import yaml
import time
import io
from xml.dom import minidom

from jinja2 import FileSystemLoader
from jinja2.environment import Environment
from logging.handlers import RotatingFileHandler
from pathlib import Path


class CloudImgLauncher:

    def __init__(self):
        self.conn = libvirt.open('qemu:///system')
        self.images = yaml.safe_load(open('distros/distros.yaml'))
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
            '-d', '--distribution', type=str, required=True,
            help='distribution image to fetch')

        parser_destroy.add_argument(
            '-n', '--hostname', type=str,
            help='define hostname')

        parser_create.add_argument(
            '-p', '--pub_key_path', type=str,
            default='~/.ssh/id_rsa.pub',
            help='path to the pub key path')
        parser_create.add_argument(
            '-n', '--hostname', type=str,
            help='define hostname')
        parser_create.add_argument(
            '-d', '--distribution', type=str, required=True,
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
            '/tmp/cloud-image-launcher.log', 'a', 300)
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

    def get_instance_macs(self):
        macs = []
        vmdom = self.conn.lookupByName(self.args.hostname)
        doc = minidom.parse(io.StringIO(vmdom.XMLDesc()))
        for node in doc.getElementsByTagName('devices'):
            i_nodes = node.getElementsByTagName('interface')
            for i_node in i_nodes:
                for v_node in i_node.getElementsByTagName('mac'):
                    macs.append(v_node.getAttribute('address'))
        return macs

    def _create_image(self):
        base_image = os.path.join(
            self.images_path, self.images[self.args.distribution]['image'])
        disk = os.path.join(self.images_path, self.args.hostname)

        command = ['sudo', 'qemu-img', 'create', '-q', '-f', 'qcow2', '-b',
                   base_image, disk]
        self.execute(command, output='devnull')
        self.logger.info('%s created' % disk)

    def get_ip_address_from_dhcp_leases(self, macs):
        if not macs:
            self.logger.info("No MAC addr found for that dom")
            return
        # Only use the first mac we found on default network
        mac = macs[0]
        self.logger.info(
            "Getting IP address - waiting for DHCP lease (%s) ..." % mac)
        max_attempt = 45
        net = [n for n in self.conn.listAllNetworks()
               if n.name() == 'default'][0]
        ipaddr = None
        attempt = 0
        while True:
            _ipaddr = [lease['ipaddr'] for lease in net.DHCPLeases()
                       if lease['mac'] == mac]
            if not _ipaddr:
                if attempt == max_attempt:
                    self.logger.info(
                        "Timeout trying to discover VM ip address")
                    break
                time.sleep(1)
                attempt = attempt + 1
            else:
                ipaddr = _ipaddr[0]
                break
        return ipaddr

    def get_dom_ip(self):
        macs = self.get_instance_macs()
        return self.get_ip_address_from_dhcp_leases(macs)

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
        ipaddr = self.get_dom_ip()
        if not ipaddr:
            self.logger.info(
                'sudo virsh --connect=qemu:///system net-dhcp-leases default')
        else:
            self.logger.info("Your VM net iface is up at %s" % ipaddr)

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
        distribution = self.args.distribution
        distributions = [image for image in self.images.keys()]
        if distribution not in self.images:
            self.logger.info(
                f"Available distributions: {', '.join(distributions)}")
            sys.exit(1)
        base_image = os.path.join(
            self.images_path, self.images[distribution]['image'])
        url = self.images[self.args.distribution]['url']
        if not Path(base_image).is_file():
            self.logger.info(f'Fetching {url}')
            command = ['sudo', 'curl', '-o', base_image, '-L', url]
            self.execute(command)
        self.logger.info(f'Image {distribution} availables')

    def main(self):
        self.parse_arguments()
        self.manage_log()
        getattr(self, self.args.command)()
        self.conn.close()


if __name__ == '__main__':
    CloudImgLauncher().main()
