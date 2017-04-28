# -*- encoding: utf8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2017 Marek Marczykowski-Górecki
#                               <marmarek@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation; either version 2.1 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with this program; if not, see <http://www.gnu.org/licenses/>.


'''
Main Qubes() class and related classes.
'''
import shlex
import socket
import subprocess

import logging

import qubesmgmt.base
import qubesmgmt.exc
import qubesmgmt.label
import qubesmgmt.storage
import qubesmgmt.utils
import qubesmgmt.vm
import qubesmgmt.config

BUF_SIZE = 4096
VM_ENTRY_POINT = 'qubesmgmt.vm'

class VMCollection(object):
    '''Collection of VMs objects'''
    def __init__(self, app):
        self.app = app
        self._vm_list = None
        self._vm_objects = {}

    def clear_cache(self):
        '''Clear cached list of VMs'''
        self._vm_list = None

    def refresh_cache(self, force=False):
        '''Refresh cached list of VMs'''
        if not force and self._vm_list is not None:
            return
        vm_list_data = self.app.qubesd_call(
            'dom0',
            'mgmt.vm.List'
        )
        new_vm_list = {}
        # FIXME: this will probably change
        for vm_data in vm_list_data.splitlines():
            vm_name, props = vm_data.decode('ascii').split(' ', 1)
            vm_name = str(vm_name)
            props = props.split(' ')
            new_vm_list[vm_name] = dict(
                [vm_prop.split('=', 1) for vm_prop in props])

        self._vm_list = new_vm_list
        for name, vm in list(self._vm_objects.items()):
            if vm.name not in self._vm_list:
                # VM no longer exists
                del self._vm_objects[name]
            elif vm.__class__.__name__ != self._vm_list[vm.name]['class']:
                # VM class have changed
                del self._vm_objects[name]
            # TODO: some generation ID, to detect VM re-creation
            elif name != vm.name:
                # renamed
                self._vm_objects[vm.name] = vm
                del self._vm_objects[name]

    def __getitem__(self, item):
        if item not in self:
            raise KeyError(item)
        if item not in self._vm_objects:
            cls = qubesmgmt.utils.get_entry_point_one(VM_ENTRY_POINT,
                self._vm_list[item]['class'])
            self._vm_objects[item] = cls(self.app, item)
        return self._vm_objects[item]

    def __contains__(self, item):
        self.refresh_cache()
        return item in self._vm_list

    def __delitem__(self, key):
        self.app.qubesd_call(key, 'mgmt.vm.Remove')
        self.clear_cache()

    def __iter__(self):
        self.refresh_cache()
        for vm in self._vm_list:
            yield self[vm]

    def keys(self):
        '''Get list of VM names.'''
        self.refresh_cache()
        return self._vm_list.keys()


class QubesBase(qubesmgmt.base.PropertyHolder):
    '''Main Qubes application'''

    #: domains (VMs) collection
    domains = None
    #: labels collection
    labels = None
    #: storage pools
    pools = None
    #: type of qubesd connection: either 'socket' or 'qrexec'
    qubesd_connection_type = None
    #: logger
    log = None

    def __init__(self):
        super(QubesBase, self).__init__(self, 'mgmt.property.', 'dom0')
        self.domains = VMCollection(self)
        self.labels = qubesmgmt.base.WrapperObjectsCollection(
            self, 'mgmt.label.List', qubesmgmt.label.Label)
        self.pools = qubesmgmt.base.WrapperObjectsCollection(
            self, 'mgmt.pool.List', qubesmgmt.storage.Pool)
        #: cache for available storage pool drivers and options to create them
        self._pool_drivers = None
        self.log = logging.getLogger('app')

    def _refresh_pool_drivers(self):
        '''
        Refresh cached storage pool drivers and their parameters.

        :return: None
        '''
        if self._pool_drivers is None:
            pool_drivers_data = self.qubesd_call(
                'dom0', 'mgmt.pool.ListDrivers', None, None)
            assert pool_drivers_data.endswith(b'\n')
            pool_drivers = {}
            for driver_line in pool_drivers_data.decode('ascii').splitlines():
                if not driver_line:
                    continue
                driver_name, driver_options = driver_line.split(' ', 1)
                pool_drivers[driver_name] = driver_options.split(' ')
            self._pool_drivers = pool_drivers

    @property
    def pool_drivers(self):
        ''' Available storage pool drivers '''
        self._refresh_pool_drivers()
        return self._pool_drivers.keys()

    def pool_driver_parameters(self, driver):
        ''' Parameters to initialize storage pool using given driver '''
        self._refresh_pool_drivers()
        return self._pool_drivers[driver]

    def add_pool(self, name, driver, **kwargs):
        ''' Add a storage pool to config

        :param name: name of storage pool to create
        :param driver: driver to use, see :py:meth:`pool_drivers` for
        available drivers
        :param kwargs: configuration parameters for storage pool,
        see :py:meth:`pool_driver_parameters` for a list
        '''
        # sort parameters only to ease testing, not required by API
        payload = 'name={}\n'.format(name) + \
                  ''.join('{}={}\n'.format(key, value)
            for key, value in sorted(kwargs.items()))
        self.qubesd_call('dom0', 'mgmt.pool.Add', driver,
            payload.encode('utf-8'))

    def remove_pool(self, name):
        ''' Remove a storage pool '''
        self.qubesd_call('dom0', 'mgmt.pool.Remove', name, None)

    def get_label(self, label):
        '''Get label as identified by index or name

        :throws KeyError: when label is not found
        '''

        # first search for name, verbatim
        try:
            return self.labels[label]
        except KeyError:
            pass

        # then search for index
        if label.isdigit():
            for i in self.labels:
                if i.index == int(label):
                    return i

        raise KeyError(label)

    @staticmethod
    def get_vm_class(clsname):
        '''Find the class for a domain.

        Classes are registered as setuptools' entry points in ``qubes.vm``
        group. Any package may supply their own classes.

        :param str clsname: name of the class
        :return type: class
        '''

        try:
            return qubesmgmt.utils.get_entry_point_one(
                VM_ENTRY_POINT, clsname)
        except KeyError:
            raise qubesmgmt.exc.QubesException(
                'no such VM class: {!r}'.format(clsname))
            # don't catch TypeError

    def add_new_vm(self, cls, name, label, template=None, pool=None,
            pools=None):
        '''Create new Virtual Machine

        Example usage with custom storage pools:

        >>> app = qubesmgmt.Qubes()
        >>> pools = {'private': 'external'}
        >>> vm = app.add_new_vm('AppVM', 'my-new-vm', 'red',
        >>>    'my-template', pools=pools)
        >>> vm.netvm = app.domains['sys-whonix']

        :param str cls: name of VM class (`AppVM`, `TemplateVM` etc)
        :param str name: name of VM
        :param str label: label color for new VM
        :param str template: template to use (if apply for given VM class),
        can be also VM object; use None for default value
        :param str pool: storage pool to use instead of default one
        :param dict pools: storage pool for specific volumes
        :return new VM object
        '''

        if not isinstance(cls, str):
            cls = cls.__name__

        if template is not None:
            template = str(template)

        if pool and pools:
            raise ValueError('only one of pool= and pools= can be used')

        method_prefix = 'mgmt.vm.Create.'
        payload = 'name={} label={}'.format(name, label)
        if pool:
            payload += ' pool={}'.format(str(pool))
            method_prefix = 'mgmt.vm.CreateInPool.'
        if pools:
            payload += ''.join(' pool:{}={}'.format(vol, str(pool))
                for vol, pool in sorted(pools.items()))
            method_prefix = 'mgmt.vm.CreateInPool.'

        self.qubesd_call('dom0', method_prefix + cls, template,
            payload.encode('utf-8'))

        return self.domains[name]

    def run_service(self, dest, service, filter_esc=False, user=None,
            localcmd=None, **kwargs):
        '''Run qrexec service in a given destination

        *kwargs* are passed verbatim to :py:meth:`subprocess.Popen`.

        :param str dest: Destination - may be a VM name or empty
        string for default (for a given service)
        :param str service: service name
        :param bool filter_esc: filter escape sequences to protect terminal \
            emulator
        :param str user: username to run service as
        :param str localcmd: Command to connect stdin/stdout to
        :rtype: subprocess.Popen
        '''
        raise NotImplementedError


class QubesLocal(QubesBase):
    '''Application object communicating through local socket.

    Used when running in dom0.
    '''

    qubesd_connection_type = 'socket'

    def qubesd_call(self, dest, method, arg=None, payload=None):
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(qubesmgmt.config.QUBESD_SOCKET)
        except IOError:
            # TODO:
            raise

        # src, method, dest, arg
        for call_arg in ('dom0', method, dest, arg):
            if call_arg is not None:
                client_socket.sendall(call_arg.encode('ascii'))
            client_socket.sendall(b'\0')
        if payload is not None:
            client_socket.sendall(payload)

        client_socket.shutdown(socket.SHUT_WR)

        return_data = client_socket.makefile('rb').read()
        client_socket.close()
        return self._parse_qubesd_response(return_data)

    def run_service(self, dest, service, filter_esc=False, user=None,
            localcmd=None, **kwargs):
        '''Run qrexec service in a given destination

        :param str dest: Destination - may be a VM name or empty
        string for default (for a given service)
        :param str service: service name
        :param bool filter_esc: filter escape sequences to protect terminal \
            emulator
        :param str user: username to run service as
        :param str localcmd: Command to connect stdin/stdout to
        :rtype: subprocess.Popen
        '''

        if not dest:
            raise ValueError('Empty destination name allowed only from a VM')
        try:
            self.qubesd_call(dest, 'mgmt.vm.Start')
        except qubesmgmt.exc.QubesVMNotHaltedError:
            pass
        qrexec_opts = ['-d', dest]
        if filter_esc:
            qrexec_opts.extend(['-t', '-T'])
        if localcmd:
            qrexec_opts.extend(['-l', localcmd])
        if user is None:
            user = 'DEFAULT'
        kwargs.setdefault('stdin', subprocess.PIPE)
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        proc = subprocess.Popen([qubesmgmt.config.QREXEC_CLIENT] +
            qrexec_opts + ['{}:QUBESRPC {} dom0'.format(user, service)],
            **kwargs)
        return proc


class QubesRemote(QubesBase):
    '''Application object communicating through qrexec services.

    Used when running in VM.
    '''

    qubesd_connection_type = 'qrexec'

    def qubesd_call(self, dest, method, arg=None, payload=None):
        service_name = method
        if arg is not None:
            service_name += '+' + arg
        p = subprocess.Popen([qubesmgmt.config.QREXEC_CLIENT_VM,
            dest, service_name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        (stdout, stderr) = p.communicate(payload)
        if p.returncode != 0:
            # TODO: use dedicated exception
            raise qubesmgmt.exc.QubesException('Service call error: %s',
                stderr.decode())

        return self._parse_qubesd_response(stdout)

    def run_service(self, dest, service, filter_esc=False, user=None,
            localcmd=None, **kwargs):
        '''Run qrexec service in a given destination

        :param str dest: Destination - may be a VM name or empty
        string for default (for a given service)
        :param str service: service name
        :param bool filter_esc: filter escape sequences to protect terminal \
            emulator
        :param str user: username to run service as
        :param str localcmd: Command to connect stdin/stdout to
        :rtype: subprocess.Popen
        '''
        if filter_esc:
            raise NotImplementedError(
                'filter_esc not implemented for calls from VM')
        if user:
            raise ValueError(
                'non-default user not possible for calls from VM')
        kwargs.setdefault('stdin', subprocess.PIPE)
        kwargs.setdefault('stdout', subprocess.PIPE)
        kwargs.setdefault('stderr', subprocess.PIPE)
        proc = subprocess.Popen([qubesmgmt.config.QREXEC_CLIENT_VM,
            dest or '', service] + (shlex.split(localcmd) if localcmd else []),
            **kwargs)
        return proc
