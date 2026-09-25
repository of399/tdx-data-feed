import time

from pytdx.hq import TdxHq_API


class TdxClient:
    def __init__(self):
        self.api = TdxHq_API()
        from pytdx.config.hosts import hq_hosts
        self.servers = [(h[1], h[2]) for h in hq_hosts][:5]
        self.current_server = 0
        self.last_request = 0
        self.connected = False
        self._hb_running = False

    def connect(self):
        while True:
            ip, port = self.servers[self.current_server]
            try:
                if self.api.connect(ip, port):
                    self.connected = True
                    return True
            except Exception:
                pass
            self.current_server = (self.current_server + 1) % len(self.servers)
            time.sleep(1)

    def reconnect(self):
        self.connected = False
        return self.connect()

    def start_heartbeat(self):
        import threading

        self._hb_running = True

        def _loop():
            while self._hb_running:
                try:
                    self.api.get_security_count()
                except Exception:
                    self.reconnect()
                time.sleep(30)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    def fetch_with_retry(self, func, *args, **kwargs):
        retry = 3
        while retry > 0:
            now = time.time()
            if now - self.last_request < 0.08:
                time.sleep(0.08 - (now - self.last_request))
            try:
                result = func(*args, **kwargs)
                self.last_request = time.time()
                return result
            except Exception:
                retry -= 1
                if retry == 0:
                    raise
                self.reconnect()
        return None
