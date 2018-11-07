import gevent


# ============================================================================
class LaunchAllPool(object):
    POOL_NAME_LABEL = 'owt.shepherd.pool'
    POOL_KEY = 'p:{id}:i'

    POOL_FLOCKS = 'p:{id}:f'

    POOL_REQ = 'p:{id}:rq:'

    DEFAULT_DURATION = 3600

    EXPIRE_CHECK = 30

    def __init__(self, name, shepherd, redis, duration=None, expire_check=None, **kwargs):
        self.name = name
        self.shepherd = shepherd
        self.redis = redis

        self.duration = int(duration or self.DEFAULT_DURATION)
        self.expire_check = expire_check or self.EXPIRE_CHECK

        self.labels = {self.POOL_NAME_LABEL: self.name}

        self.pool_key = self.POOL_KEY.format(id=self.name)
        self.flocks_key = self.POOL_FLOCKS.format(id=self.name)

        self.req_key = self.POOL_REQ.format(id=self.name)

        self.api = shepherd.docker.api

        self.running = True

        gevent.spawn(self.event_loop)

        gevent.spawn(self.expire_loop)

    def request(self, flock_name, req_opts):
        return self.shepherd.request_flock(flock_name, req_opts)

    def start(self, reqid, environ=None, value=1):
        res = self.shepherd.start_flock(reqid, labels=self.labels,
                                        environ=environ)
        if 'error' not in res:
            self.redis.sadd(self.flocks_key, reqid)

            self.redis.set(self.req_key + reqid, value, ex=self.duration)

        return res

    def stop(self, reqid, expired=False, keep_reqid=False, grace_time=None):
        res = self.shepherd.stop_flock(reqid, keep_reqid=keep_reqid,
                                              grace_time=grace_time)

        if 'error' not in res:
            self.redis.srem(self.flocks_key, reqid)

            self.redis.delete(self.req_key + reqid)

        return res

    def is_active(self, reqid):
        return self.redis.sismember(self.flocks_key, reqid)

    def curr_size(self):
        return self.redis.scard(self.flocks_key)

    def event_loop(self):
        filters = {
                   'label': self.POOL_NAME_LABEL + '=' + self.name,
                   'event': ['die', 'start'],
                   'type': 'container'
                  }

        print('Event Loop Started')

        for event in self.api.events(decode=True,
                                     filters=filters):
            try:
                if not self.running:
                    break

                reqid = event['Actor']['Attributes'][self.shepherd.SHEP_REQID_LABEL]
                if event['status'] == 'die':
                    self.handle_die_event(reqid, event)

                elif event['status'] == 'start':
                    self.handle_start_event(reqid, event)

            except Exception as e:
                print(e)

    def handle_die_event(self, reqid, event):
        key = self.req_key + reqid
        self.redis.delete(key)

    def handle_start_event(self, reqid, event):
        pass

    def expire_loop(self):
        print('Expire Loop Started')
        while self.running:
            for reqid in self.redis.smembers(self.flocks_key):
                key = self.req_key + reqid
                if not self.redis.exists(key):
                    print('Expired: ' + reqid)
                    self.stop(reqid, expired=True)

            gevent.sleep(self.expire_check)

    def shutdown(self):
        self.running = False

        for reqid in self.redis.smembers(self.flocks_key):
            self.stop(reqid)


# ============================================================================
class FixedSizePool(LaunchAllPool):
    NOW_SERVING = 'now_serving'

    NUMBER_TTL = 180

    NEXT = 'next'

    REQID_TO_NUMBER = 'p:{id}:r2n:'

    NUMBER_TO_REQID = 'p:{id}:n2r:'

    def __init__(self, *args, **kwargs):
        super(FixedSizePool, self).__init__(*args, **kwargs)

        data = {
                'duration': kwargs['duration'],
                'max_size': kwargs['max_size']
               }

        self.redis.hmset(self.pool_key, data)

        #self.redis.hsetnx(self.pool_key, self.NOW_SERVING, '1')

        self.reqid_to_number = self.REQID_TO_NUMBER.format(id=self.name)
        self.number_to_reqid = self.NUMBER_TO_REQID.format(id=self.name)

        self.number_ttl = kwargs.get('number_ttl', self.NUMBER_TTL)

    def request(self, flock_name, req_opts):
        res = super(FixedSizePool, self).request(flock_name, req_opts)

        if 'reqid' in res:
            number = self.get_number(res['reqid'])

        return res

    def start(self, reqid, environ=None):
        if self.is_active(reqid):
            return super(FixedSizePool, self).start(reqid, environ=environ)

        pos = self.get_queue_pos(reqid)
        if pos >= 0:
            return {'queued': pos}

        res = super(FixedSizePool, self).start(reqid, environ=environ)

        if 'error' in res:
            self.remove_request(reqid)

        return res

    def get_queue_pos(self, reqid):
        number = self.get_number(reqid)
        now_serving = int(self.redis.hget(self.pool_key, self.NOW_SERVING) or 1)

        # if missed our number, get new one
        if number < now_serving:
            number = self.get_number(reqid, force_new=True)

        else:
            now_serving = self.incr_now_serving(now_serving, number)

        # pos in the queue
        pos = number - now_serving

        if pos < self.num_avail():
            self.remove_request(reqid, number)
            if pos == 0:
                max_number = int(self.redis.hget(self.pool_key, self.NEXT))
                self.incr_now_serving(now_serving, max_number)

            return -1

        return pos

    def num_avail(self):
        max_size = self.redis.hget(self.pool_key, 'max_size')
        return int(max_size) - self.curr_size()

    def incr_now_serving(self, now_serving, number):
        # if not serving current number, check any expired numbers
        while now_serving < number:
            # if not expired, stop there
            if self.redis.get(self.number_to_reqid + str(now_serving)) is not None:
                break

            now_serving = self.redis.hincrby(self.pool_key, self.NOW_SERVING, 1)

        return now_serving

    def get_number(self, reqid, force_new=False):
        number = None

        if not force_new:
            number = self.redis.get(self.reqid_to_number + reqid)

        if number is None:
            number = self.redis.hincrby(self.pool_key, self.NEXT, 1)
        else:
            number = int(number)

        self.redis.set(self.reqid_to_number + reqid, str(number), ex=self.number_ttl)
        self.redis.set(self.number_to_reqid + str(number), reqid, ex=self.number_ttl)
        return number

    def remove_request(self, reqid, number=None):
        if number is None:
            number = self.redis.get(self.reqid_to_number + reqid)

        self.redis.delete(self.reqid_to_number + reqid)

        if number is not None:
            self.redis.delete(self.number_to_reqid + str(number))


# ============================================================================
class PersistentPool(LaunchAllPool):
    POOL_WAIT_Q = 'p:{id}:q'

    POOL_WAIT_SET = 'p:{id}:s'

    def __init__(self, *args, **kwargs):
        super(PersistentPool, self).__init__(*args, **kwargs)

        data = {
                'duration': kwargs['duration'],
                'max_size': kwargs['max_size']
               }

        self.redis.hmset(self.pool_key, data)

        self.pool_wait_q = self.POOL_WAIT_Q.format(id=self.name)

        self.pool_wait_set = self.POOL_WAIT_SET.format(id=self.name)

        self.grace_time = kwargs.get('grace_time', None)

    def num_avail(self):
        max_size = self.redis.hget(self.pool_key, 'max_size')
        return int(max_size) - self.curr_size()

    def start(self, reqid, environ=None):
        if self.is_active(reqid):
            return super(PersistentPool, self).start(reqid, environ=environ)

        elif self.redis.sismember(self.pool_wait_set, reqid):
            return {'queued': self._find_wait_pos(reqid)}

        if self.num_avail() == 0:
            pos = self._push_wait(reqid)
            return {'queued': pos - 1}

        return super(PersistentPool, self).start(reqid, environ=environ)

    def _push_wait(self, reqid):
        self.redis.sadd(self.pool_wait_set, reqid)
        return self.redis.rpush(self.pool_wait_q, reqid)

    def _pop_wait(self):
        reqid = self.redis.lpop(self.pool_wait_q)
        if reqid:
            self.redis.srem(self.pool_wait_set, reqid)

        return reqid

    def _remove_wait(self, reqid):
        self.redis.lrem(self.pool_wait_q, 1, reqid)
        self.redis.srem(self.pool_wait_set, reqid)

    def _find_wait_pos(self, reqid):
        pool_list = self.redis.lrange(self.pool_wait_q, 0, -1)
        try:
            return pool_list.index(reqid)
        except:
            return -1

    def stop(self, reqid, expired=False, **kwargs):
        stop_res = {'success': True}

        # if currently running
        if self.is_active(reqid):
            # remove so is not considered for restart by expire loop
            #if not expired:
            #    self.redis.srem(self.flocks_key, reqid)

            next_reqid = self._pop_wait()

            # if stopping due to expiration:
            if expired:

                #if no next key, extend this for same duration
                if next_reqid is None:
                    self.redis.set(self.req_key + reqid, 1, ex=self.duration)
                    return {'success': True}
                else:
                    self._push_wait(reqid)

            stop_res = super(PersistentPool, self).stop(reqid, expired=expired,
                                                        keep_reqid=expired,
                                                        grace_time=self.grace_time)

            # if next reqid, queue it up next
            if next_reqid:
                res = super(PersistentPool, self).start(next_reqid)

                # ensure not running for good
                #if 'error' in res:
                #    self.redis.srem(self.flocks_key, reqid)

        else:
            stop_res = super(PersistentPool, self).stop(reqid, expired=expired)

            self._remove_wait(reqid)

        return stop_res

