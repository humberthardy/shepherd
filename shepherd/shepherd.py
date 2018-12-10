import docker
import yaml
import json
import base64
import os
import time
import traceback

from redis import StrictRedis

from shepherd.schema import AllFlockSchema, InvalidParam
from shepherd.network_pool import NetworkPool

import gevent


# ============================================================================
class Shepherd(object):
    DEFAULT_FLOCKS = 'flocks.yaml'

    USER_PARAMS_KEY = 'up:{0}'

    SHEP_REQID_LABEL = 'owt.shepherd.reqid'

    DEFAULT_REQ_TTL = 120

    DEFAULT_SHM_SIZE = '1g'

    def __init__(self, redis, network_templ=None):
        self.flocks = {}
        self.docker = docker.from_env()
        self.redis = redis

        self.network_pool = NetworkPool(self.docker, network_templ=network_templ)

    def load_flocks(self, flocks_file):
        with open(flocks_file) as fh:
            data = yaml.load(fh.read())
            flocks = AllFlockSchema().load(data)
            for flock in flocks['flocks']:
                self.flocks[flock['name']] = flock

    def request_flock(self, flock_name, req_opts=None, ttl=None):
        req_opts = req_opts or {}
        try:
            flock = self.flocks[flock_name]
        except:
            return {'error': 'invalid_flock',
                    'flock': flock_name}

        flock_req = FlockRequest().init_new(flock_name, req_opts)

        overrides = flock_req.get_overrides()

        try:
            image_list = self.resolve_image_list(flock['containers'], overrides)
        except InvalidParam as ip:
            return ip.msg

        flock_req.data['image_list'] = image_list
        ttl = ttl or self.DEFAULT_REQ_TTL
        flock_req.save(self.redis, expire=ttl)

        return {'reqid': flock_req.reqid}

    def is_valid_flock(self, reqid, ensure_state=None):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return False

        if ensure_state and ensure_state != flock_req.get_state():
            return False

        return True

    def start_flock(self, reqid, labels=None, environ=None, pausable=False,
                    network_pool=None):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        response = flock_req.get_cached_response()
        if response:
            return response

        flock_req.update_env(environ)

        try:
            flock_name = flock_req.data['flock']
            image_list = flock_req.data['image_list']
            flock = self.flocks[flock_name]
        except:
            return {'error': 'invalid_flock',
                    'flock': flock_name}

        network = None
        containers = {}

        try:
            network_pool = network_pool or self.network_pool
            network = network_pool.create_network()

            flock_req.set_network(network.name)

            links = flock.get('links', [])
            for link in links:
                self.link_external_container(network, link)

            # auto remove if not pausable and flock auto_remove is true
            auto_remove = not pausable and flock.get('auto_remove', True)

            for image, spec in zip(image_list, flock['containers']):
                container, info = self.run_container(image, spec, flock_req, network,
                                                     labels=labels,
                                                     auto_remove=auto_remove)
                containers[spec['name']] = info

        except:
            traceback.print_exc()

            try:
                self.stop_flock(reqid)
            except:
                pass

            return {'error': 'start_error',
                    'details': traceback.format_exc()
                   }

        response = {'containers': containers,
                    'network': network.name
                   }

        flock_req.cache_response(response, self.redis)
        return response

    def link_external_container(self, network, link):
        if ':' in link:
            name, alias = link.split(':', 1)
        else:
            name = link
            alias = link

        res = network.connect(name, aliases=[alias])

    def short_id(self, container):
        return container.id[:12]

    def get_ip(self, container, network):
        return container.attrs['NetworkSettings']['Networks'][network.name]['IPAddress']

    def get_ports(self, container, port_map):
        ports = {}
        if not port_map:
            return ports

        for port_name in port_map:
            try:
                port = port_map[port_name]
                pinfo = container.attrs['NetworkSettings']['Ports'][str(port) + '/tcp']
                pinfo = pinfo[0]
                ports[port_name] = int(pinfo['HostPort'])

            except:
                ports[port_name] = -1

        return ports

    def run_container(self, image, spec, flock_req, network, labels=None,
                      auto_remove=False):

        api = self.docker.api

        net_config = api.create_networking_config({
            network.name: api.create_endpoint_config(
                aliases=[spec['name']],
            )
        })

        ports = spec.get('ports')
        if ports:
            port_values = list(ports.values())
            port_bindings = {int(port): None for port in port_values}
        else:
            port_values = None
            port_bindings = None

        host_config = api.create_host_config(auto_remove=auto_remove,
                                             cap_add=['ALL'],
                                             shm_size=spec.get('shm_size', self.DEFAULT_SHM_SIZE),
                                             security_opt=['apparmor=unconfined'],
                                             port_bindings=port_bindings)

        name = spec['name'] + '-' + flock_req.reqid

        environ = spec.get('environment') or {}
        environ.update(flock_req.data['environ'])

        labels = labels or {}
        labels[self.SHEP_REQID_LABEL] = flock_req.reqid

        cdata = api.create_container(
            image,
            networking_config=net_config,
            ports=port_values,
            name=name,
            host_config=host_config,
            detach=True,
            hostname=spec['name'],
            environment=environ,
            labels=labels
        )

        container = self.docker.containers.get(cdata['Id'])

        external_network = spec.get('external_network')
        if external_network:
            external_network = self.docker.networks.get(external_network)
            external_network.connect(container)

        container.start()

        # reload to get updated data
        container.reload()

        info = {}
        info['id'] = self.short_id(container)

        if external_network:
            info['ip'] = self.get_ip(container, external_network)
        else:
            info['ip'] = self.get_ip(container, network)

        info['ports'] = self.get_ports(container, ports)

        if info['ip'] and flock_req.data['user_params'] and spec.get('set_user_params'):
            # add reqid to userparams
            flock_req.data['user_params']['reqid'] = flock_req.reqid
            self.redis.hmset(self.USER_PARAMS_KEY.format(info['ip']), flock_req.data['user_params'])

        return container, info

    def get_network(self, flock_req):
        name = flock_req.get_network()
        if not name:
            return None

        return self.docker.networks.get(name)

    def resolve_image_list(self, specs, overrides):
        image_list = []
        for spec in specs:
            image = overrides.get(spec['name'], spec['image'])
            image_list.append(image)
            if image != spec['image']:
                if not self.is_ancestor_of(image, spec['image']):
                    raise InvalidParam({'error': 'invalid_image_param',
                                        'image_passed': image,
                                        'image_expected': spec['image']
                                       })

        return image_list

    def is_ancestor_of(self, name, ancestor):
        name = self.full_tag(name)
        ancestor = self.full_tag(ancestor)
        try:
            image = self.docker.images.get(name)
        except docker.errors.ImageNotFound:
            return False

        history = image.history()
        for entry in history:
            if entry.get('Tags') and ancestor in entry['Tags']:
                return True

        return False

    def stop_flock(self, reqid, keep_reqid=False, grace_time=None, network_pool=None):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        if not keep_reqid:
            flock_req.delete(self.redis)
        else:
            flock_req.stop(self.redis)

        try:
            network = self.get_network(flock_req)
            containers = network.containers
        except:
            network = None
            containers = self.get_flock_containers(flock_req)

        for container in containers:
            if container.labels.get(self.SHEP_REQID_LABEL) != reqid:
                try:
                    network.disconnect(container)
                except:
                    pass

                continue

            try:
                ip = self.get_ip(container, network)
                self.redis.delete(self.USER_PARAMS_KEY.format(ip))
            except:
                pass

            try:
                if grace_time:
                    self._do_graceful_stop(container, grace_time)
                else:
                    container.kill()
            except docker.errors.APIError as e:
                pass

            try:
                container.remove(v=True, link=False, force=True)

            except docker.errors.APIError:
                pass

        try:
            network_pool = network_pool or self.network_pool
            network_pool.remove_network(network)
        except:
            pass

        return {'success': True}

    def _do_graceful_stop(self, container, grace_time):
        def do_stop():
            try:
                container.stop(timeout=grace_time)
            except docker.errors.APIError as e:
                pass

        gevent.spawn(do_stop)

    def get_flock_containers(self, flock_req):
        return self.docker.containers.list(all=True, filters={'label': self.SHEP_REQID_LABEL + '=' + flock_req.reqid})

    def pause_flock(self, reqid, grace_time=1):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        state = flock_req.get_state()
        if state != 'running':
            return {'error': 'not_running', 'state': state}

        try:
            containers = self.get_flock_containers(flock_req)

            for container in containers:
                self._do_graceful_stop(container, grace_time)

            flock_req.set_state('paused', self.redis)

        except:
            traceback.print_exc()

            return {'error': 'pause_failed',
                    'details': traceback.format_exc()
                   }

        return {'success': True}

    def resume_flock(self, reqid):
        flock_req = FlockRequest(reqid)
        if not flock_req.load(self.redis):
            return {'error': 'invalid_reqid'}

        state = flock_req.get_state()
        if state != 'paused':
            return {'error': 'not_paused', 'state': state}

        try:
            containers = self.get_flock_containers(flock_req)

            network = self.get_network(flock_req)

            for container in containers:
                container.start()

            flock_req.set_state('running', self.redis)

        except:
            traceback.print_exc()

            return {'error': 'resume_failed',
                    'details': traceback.format_exc()
                   }

        return {'success': True}

    @classmethod
    def full_tag(cls, tag):
        return tag + ':latest' if ':' not in tag else tag


# ===========================================================================
class FlockRequest(object):
    REQ_KEY = 'req:{0}'

    def __init__(self, reqid=None):
        if not reqid:
            reqid = self._make_reqid()
        self.reqid = reqid
        self.key = self.REQ_KEY.format(self.reqid)

    def _make_reqid(self):
        return base64.b32encode(os.urandom(15)).decode('utf-8')

    def init_new(self, flock_name, req_opts):
        self.data = {'id': self.reqid,
                     'flock': flock_name,
                     'overrides': req_opts.get('overrides', {}),
                     'user_params': req_opts.get('user_params', {}),
                     'environ': req_opts.get('environ', {}),
                     'state': 'new',
                    }
        return self

    def update_env(self, environ):
        if not environ:
            return

        self.data['environ'].update(environ)

    def get_overrides(self):
        return self.data.get('overrides') or {}

    def get_state(self):
        return self.data.get('state', 'new')

    def set_state(self, state, redis):
        self.data['state'] = state
        self.save(redis)

    def set_network(self, network_name):
        self.data['net'] = network_name

    def get_network(self):
        return self.data.get('net')

    def load(self, redis):
        data = redis.get(self.key)
        self.data = json.loads(data) if data else {}
        return self.data != {}

    def save(self, redis, expire=None):
        redis.set(self.key, json.dumps(self.data), ex=expire)
        if expire is None:
            redis.persist(self.key)

    def get_cached_response(self):
        return self.data.get('resp')

    def cache_response(self, resp, redis):
        self.data['state'] = 'running'
        self.data['resp'] = resp
        self.save(redis)

    def stop(self, redis):
        self.data.pop('resp', '')
        self.data['state'] = 'stopped'
        self.save(redis)

    def delete(self, redis):
        redis.delete(self.key)


# ===========================================================================
if __name__ == '__main__':
    pass
    #shep = Shepherd(StrictRedis('redis://redis/3'))
    #res = shep.request_flock('test', {'foo': 'bar'})

    #print(res['reqid'])



