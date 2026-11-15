"""Reception reports to PSK Reporter (https://pskreporter.info/pskdev.html), standard library only.

IPFIX-style UDP datagrams to report.pskreporter.info:4739. The rules of that page as applied here:
  * one datagram at most every five minutes, the timer started by the first waiting spot and stretched by
    a random part so stations do not report in step,
  * a callsign is reported at most once in five minutes,
  * the record descriptors go out with the first three datagrams and then once an hour,
  * one socket (one source port) and one random identifier for the life of the process; the sequence
    number counts spots, not datagrams,
  * informationSource 1 (automatically extracted).
Sender record: callsign, frequency, SNR (1 byte), mode, locator (empty when unknown), informationSource,
time - the field set WSJT-X sends. Receiver record: callsign, locator, software, antenna.
"""
import os
import random
import socket
import struct
import time

ENTERPRISE = b"\x00\x00\x76\x8f"
RX_TEMPLATE = (b"\x00\x03\x00\x2c\x99\x92\x00\x04\x00\x01"
               + b"".join(f + b"\xff\xff" + ENTERPRISE for f in (b"\x80\x02", b"\x80\x04", b"\x80\x08", b"\x80\x09")) + b"\x00\x00")
TX_TEMPLATE = (b"\x00\x02\x00\x3c\x99\x93\x00\x07"
               + b"\x80\x01\xff\xff" + ENTERPRISE      # senderCallsign
               + b"\x80\x05\x00\x04" + ENTERPRISE      # frequency, Hz
               + b"\x80\x06\x00\x01" + ENTERPRISE      # sNR
               + b"\x80\x0a\xff\xff" + ENTERPRISE      # mode
               + b"\x80\x03\xff\xff" + ENTERPRISE      # senderLocator
               + b"\x80\x0b\x00\x01" + ENTERPRISE      # informationSource
               + b"\x00\x96\x00\x04")                  # flowStartSeconds
MAX_SPOTS = 60        # per datagram, far below the UDP size limit


def _str(s):
    b = s.encode("utf-8")[:254]
    return bytes([len(b)]) + b


def _pad(b):
    return b + b"\x00" * (-len(b) % 4)


class Reporter:
    def __init__(self, call, locator, software, antenna, host="report.pskreporter.info", port=4739,
                 log=print, sock=None, clock=time.time):
        self.rx = (call, locator, software, antenna)
        self.addr, self.log, self.clock = (host, port), log, clock
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ident = os.urandom(4)
        self.seq, self.sent_packets, self.templates_at = 0, 0, 0.0
        self.pending, self.last = {}, {}       # call -> spot ; call -> time last reported
        self.due = None

    def add(self, call, locator, snr, freq, t, mode="FT8"):
        """Queue a spot; the strongest report of a callsign within the waiting period wins."""
        now = self.clock()
        if now - self.last.get(call, 0) < 300:
            return False
        old = self.pending.get(call)
        if old is None or snr > old["snr"]:
            self.pending[call] = {"call": call, "loc": locator or (old or {}).get("loc") or "", "snr": snr, "freq": freq,
                                  "t": int(t), "mode": mode}
        elif locator and not old["loc"]:
            old["loc"] = locator
        if self.due is None:
            self.due = now + 300 + random.uniform(0, 30)
        return True

    def packet(self, spots, templates):
        rx = _pad(b"".join(_str(x) for x in self.rx))
        body = b"\x99\x92" + struct.pack(">H", len(rx) + 4) + rx
        tx = _pad(b"".join(_str(s["call"]) + struct.pack(">Ib", s["freq"], max(-128, min(127, s["snr"]))) + _str(s["mode"])
                           + _str(s["loc"]) + b"\x01" + struct.pack(">I", s["t"]) for s in spots))
        body += b"\x99\x93" + struct.pack(">H", len(tx) + 4) + tx
        if templates:
            body = RX_TEMPLATE + TX_TEMPLATE + body
        return b"\x00\x0a" + struct.pack(">HII", len(body) + 16, int(self.clock()), self.seq) + self.ident + body

    def tick(self, force=False):
        """Send what is waiting once its five minutes are over. Returns the number of spots sent."""
        now = self.clock()
        if not self.pending or not (force or (self.due is not None and now >= self.due)):
            return 0
        spots = list(self.pending.values())[:MAX_SPOTS]
        templates = self.sent_packets < 3 or now - self.templates_at >= 3600
        try:
            self.sock.sendto(self.packet(spots, templates), self.addr)
        except OSError as e:                    # no network, no name: keep the spots, try again in a minute
            self.log(f"[pskreporter] not sent: {e}")
            self.due = now + 60
            return 0
        if templates:
            self.templates_at = now
        self.sent_packets += 1
        self.seq = (self.seq + len(spots)) & 0xFFFFFFFF
        for s in spots:
            self.last[s["call"]] = now
            del self.pending[s["call"]]
        self.last = {c: t for c, t in self.last.items() if now - t < 3600}
        self.due = now + 300 + random.uniform(0, 30) if self.pending else None
        self.log(f"[pskreporter] {len(spots)} spot{'s' if len(spots) != 1 else ''} sent: " + " ".join(s["call"] for s in spots))
        return len(spots)
