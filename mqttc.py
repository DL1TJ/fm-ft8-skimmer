"""Small MQTT 3.1.1 publisher, standard library only (no paho needed). TLS with certificate
verification unless switched off; username and password are optional.

send() never blocks the receiver for long and never raises: while the broker is away, messages are
dropped except retained ones, whose latest value per topic is sent again after the reconnect. A last
will marks the station offline when the connection dies.
"""
import json
import select
import socket
import ssl
import struct
import threading
import time


def _varlen(n):
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (128 if n else 0))
        if not n:
            return bytes(out)


def _s(x):
    b = x.encode() if isinstance(x, str) else bytes(x)
    return struct.pack(">H", len(b)) + b


class Mqtt:
    def __init__(self, host, port, user, password, client_id, will=None, tls=True, log=print):
        self.host, self.port, self.user, self.password, self.cid = host, port, user, password, client_id
        self.tls = tls
        self.will, self.log = will, log           # will = (topic, payload dict), sent retained
        self.sock, self.lock = None, threading.Lock()
        self.retained, self.last_tx, self.stop = {}, 0.0, False
        threading.Thread(target=self._run, daemon=True).start()

    def _connect(self):
        flags, body = 0x02, _s(self.cid)       # clean session
        if self.will:
            flags |= 0x24                     # will flag + will retain
            body += _s(self.will[0]) + _s(json.dumps(self.will[1]))
        if self.user:
            flags |= 0x80
            body += _s(self.user)
            if self.password:
                flags |= 0x40
                body += _s(self.password)
        s = socket.create_connection((self.host, self.port), timeout=15)
        try:
            if self.tls:
                s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
            var = _s("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
            s.sendall(b"\x10" + _varlen(len(var) + len(body)) + var + body)
            ack = b""
            while len(ack) < 4:
                part = s.recv(4 - len(ack))
                if not part:
                    break
                ack += part
            if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                why = {4: "bad username or password", 5: "not authorised"}.get(ack[3] if len(ack) == 4 else -1, ack.hex() or "no answer")
                raise OSError(f"broker refused the connection: {why}")
        except BaseException:
            s.close()
            raise
        return s

    def _tx(self, topic, data, retain):
        var = _s(topic) + data
        self.sock.sendall(bytes([0x30 | (1 if retain else 0)]) + _varlen(len(var)) + var)
        self.last_tx = time.time()

    def _drop(self, why):
        if self.sock is not None:
            self.log(f"[mqtt] connection lost: {why}")
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _run(self):
        while not self.stop:
            with self.lock:
                try:
                    if self.sock is None:
                        self.sock = self._connect()
                        self.log(f"[mqtt] connected to {self.host}")
                        for topic, data in list(self.retained.items()):
                            self._tx(topic, data, True)
                    else:
                        while select.select([self.sock], [], [], 0)[0]:    # PINGRESP; EOF = broker gone
                            if not self.sock.recv(256):
                                raise OSError("closed by the broker")
                        if time.time() - self.last_tx > 25:
                            self.sock.sendall(b"\xc0\x00")
                            self.last_tx = time.time()
                except (OSError, ssl.SSLError) as e:
                    if self.sock is None:
                        self.log(f"[mqtt] connect failed: {e}")
                    self._drop(e)
            time.sleep(5)

    def send(self, topic, payload, retain=False):
        data = json.dumps(payload, separators=(",", ":")).encode()
        with self.lock:
            if retain:
                self.retained[topic] = data
            if self.sock is None:
                return False
            try:
                self._tx(topic, data, retain)
                return True
            except (OSError, ssl.SSLError) as e:
                self._drop(e)
                return False

    def close(self):
        self.stop = True
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.sendall(b"\xe0\x00")
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


class Dry:
    """Stands in for Mqtt in the self-test: records, sends nothing."""

    def __init__(self, log=print):
        self.log, self.sent = log, []

    def send(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))
        return True

    def close(self):
        pass
