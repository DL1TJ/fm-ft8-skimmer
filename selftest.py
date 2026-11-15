#!/usr/bin/env python3
"""selftest.py - proves the skimmer without a radio, without a broker and without sending anything.

1. settings: defaults, a full file, and the mistakes that must be refused,
2. message parsing and QSO grouping on a scripted exchange,
3. the PSK Reporter datagram layout, against a recording socket,
4. the web page server: what it serves and what it refuses,
5. the MQTT publisher, against a stand-in broker on localhost,
6. the whole signal path: FT8 audio -> FM-modulated IQ as rtl_sdr would deliver it (narrow and wide
   deviation, with noise), other signals (carrier, tone, voice, packet, SSB, CW) -> demodulate ->
   jt9 -> station. Part 6 needs jt9 (WSJT-X); it is skipped with a note when jt9 is not installed.

Needs numpy and, for part 6, jt9. Run it after every change:  python3 selftest.py
The callsigns are the example calls of the WSJT-X documentation.
"""
import json
import os
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

import ft8msg
import mqttc
import pskreporter
import skimmer
import web

FAIL = []
HERE = Path(__file__).resolve().parent

# FT8 channel symbols (79 tones each) for the test messages, as printed by WSJT-X's ft8code
SYNC = "3140652"
SYMBOLS = {
    "CQ K1ABC FN42": ("00000000100547670460602153343", "73601104751700733474545513354"),
    "K1ABC W9XYZ EN37": ("03224752350406114700513432537", "46455756156477030037617546223"),
    "W9XYZ K1ABC -07": ("02035572500547670461746203506", "62523105424515151027711723341"),
    "CQ G0XYZ IO91": ("00000000100527607260740543526", "15267214124165136541752350423"),
}


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAIL.append(name)


# ---------- 1. settings ----------
def config_from(text, tmp):
    p = Path(tmp) / "config.ini"
    p.write_text(text)
    os.chmod(p, 0o600)
    return skimmer.Config(str(p), must_exist=True)


def refused(text, tmp):
    try:
        config_from(text, tmp)
    except skimmer.ConfigError:
        return True
    return False


def test_config():
    tmp = tempfile.mkdtemp(prefix="fmft8-test-")
    c = skimmer.Config(os.path.join(tmp, "none.ini"))
    check("defaults: 144.575 MHz, page on localhost, nothing sent",
          c.freq == 144575000 and c.web and c.web_bind == "127.0.0.1" and not c.psk and not c.mqtt and c.keep_audio == 0 and not c.notes,
          str(vars(c)))
    c = config_from("[station]\ncallsign = k1abc\nlocator = fn42AB\nantenna = vertical\n[receiver]\nfrequency = 432500000\n"
                    "device = 00000001\ngain = 38.6\n[pskreporter]\nenabled = yes\n[mqtt]\nenabled = yes\nhost = broker.example\n"
                    "username = u\npassword = p\ntopic = /radio/fmft8/\n[web]\nbind = 0.0.0.0\n", tmp)
    check("full file: callsign and locator normalised, Hz accepted, topic trimmed",
          (c.callsign, c.locator, c.freq, c.gain, c.mqtt_topic, c.psk) == ("K1ABC", "FN42ab", 432500000, "38.6", "radio/fmft8", True),
          str(vars(c)))
    check("note when the page is open to the network", any("0.0.0.0" in n for n in c.notes), str(c.notes))
    check("public station details carry no settings beyond them",
          c.public() == {"call": "K1ABC", "loc": "FN42ab", "antenna": "vertical", "sdr": "RTL-SDR", "psk": True})
    os.chmod(Path(tmp) / "config.ini", 0o644)
    c2 = skimmer.Config(str(Path(tmp) / "config.ini"))
    check("note when a file with a password is readable by others", any("chmod 600" in n for n in c2.notes), str(c2.notes))
    c3 = config_from("[receiver]\npause_when = systemctl --user is-active --quiet 'my unit.service'\n", tmp)
    check("pause_when is split into arguments, no shell", c3.pause_when == ["systemctl", "--user", "is-active", "--quiet", "my unit.service"], str(c3.pause_when))
    check("pause: succeeds -> pause, fails / missing / empty -> listen",
          skimmer.must_pause(["true"]) and not skimmer.must_pause(["false"]) and not skimmer.must_pause(["/no/such/program"]) and not skimmer.must_pause([]))
    check("refused: PSK Reporter without a callsign", refused("[pskreporter]\nenabled = yes\n", tmp))
    check("refused: bad callsign", refused("[station]\ncallsign = NOT A CALL\n", tmp) and refused("[station]\ncallsign = <b>\n", tmp)
          and refused("[station]\ncallsign = 12345\n", tmp))
    c4 = config_from("[station]\ncallsign = onl-12345\nlocator = jo22ab12\n", tmp)
    check("listener identifier and 8-character locator accepted", (c4.callsign, c4.locator) == ("ONL-12345", "JO22ab12"), f"{c4.callsign} {c4.locator}")
    check("refused: bad locator", refused("[station]\nlocator = ZZ99\n", tmp))
    check("refused: unknown setting (a typo must not pass silently)", refused("[receiver]\nfrequenzy = 144.575\n", tmp))
    check("refused: unknown section", refused("[reciever]\ngain = 10\n", tmp))
    check("refused: device with shell characters", refused("[receiver]\ndevice = 0; rm -rf /\n", tmp))
    check("refused: MQTT without a host", refused("[mqtt]\nenabled = yes\n", tmp))
    check("refused: MQTT topic with a wildcard", refused("[mqtt]\nenabled = yes\nhost = h\ntopic = a/#\n", tmp))
    check("refused: port out of range", refused("[web]\nport = 70000\n", tmp))


def test_state_file():
    """A state file that is damaged, or of the wrong shape, means a fresh start - not a service that cannot start."""
    for n, bad in enumerate(["{not json", "[1, 2, 3]", '{"hours": {"x": 1}}', '{"decodes": 7, "qsos": "no"}', '{"hours": {"3600": "abc"}}', ""]):
        tmp = tempfile.mkdtemp(prefix="fmft8-test-")
        cfg = config_from(f"[storage]\ndirectory = {tmp}/data\n", tmp)
        cfg.data.mkdir()
        (cfg.data / "state.json").write_text(bad)
        try:
            st = skimmer.Station(cfg, skimmer.Outputs())
            st.announce()
            st.book.slot(1_800_000_000)
            st.close()
            ok = (cfg.data / "state.json.bad").exists() and json.loads((cfg.data / "state.json").read_text())["hours"]
        except Exception as e:
            ok = False
            bad += f" -> {type(e).__name__}: {e}"
        check(f"unusable state file {n + 1}: set aside, fresh start", bool(ok), bad)
    tmp = tempfile.mkdtemp(prefix="fmft8-test-")
    cfg = config_from(f"[storage]\ndirectory = {tmp}/data\n", tmp)
    st = skimmer.Station(cfg, skimmer.Outputs())
    st.data = Path(tmp) / "gone" / "deeper"                       # as if the disk went away
    try:
        st.save()
        st.record({"msg": "CQ K1ABC FN42"})
        ok = True
    except Exception:
        ok = False
    st.close()
    check("a disk that cannot be written does not stop the skimmer", ok)


# ---------- 2. messages ----------
def test_messages():
    p = ft8msg.parse
    check("cq with grid", p("CQ K1ABC FN42") == {"kind": "cq", "de": "K1ABC", "to": None, "grid": "FN42", "report": None})
    check("cq dx", p("CQ DX K1ABC FN42")["de"] == "K1ABC")
    check("grid reply", p("K1ABC W9XYZ EN37")["kind"] == "grid" and p("K1ABC W9XYZ EN37")["de"] == "W9XYZ")
    check("report", p("W9XYZ K1ABC -07")["report"] == "-07")
    check("r-report", p("K1ABC W9XYZ R+12")["kind"] == "r-report")
    check("rr73 is no grid", p("W9XYZ K1ABC RR73")["kind"] == "rr73")
    check("73", p("K1ABC W9XYZ 73")["kind"] == "73")
    check("hashed call", p("<K1ABC> W9XYZ/P R-01")["de"] == "W9XYZ/P")
    check("free text dropped", p("TNX FOR QSO 73") is None and p("5XQ0 ZZ9ABC1X AA00") is None)
    check("markup is no callsign", p("CQ <script> FN42") is None and p("K1ABC <img/src=x> 73") is None)
    b = ft8msg.Book()
    t = 1_800_000_000 // 15 * 15
    seq = ["CQ K1ABC FN42", "K1ABC W9XYZ EN37", "W9XYZ K1ABC -07", "K1ABC W9XYZ R+12", "W9XYZ K1ABC RR73", "K1ABC W9XYZ 73"]
    for i, m in enumerate(seq):
        b.add({"t": t + 15 * i, "snr": 5 + i, "dt": 0.1, "f": 1500, "msg": m})
        b.slot(t + 15 * i, None, 1)
    b.slot(t + 90, {"peak_db": 30.0, "dur_s": 4.0, "kind": "voice", "label": "FM voice (probable)"})
    b.slot(t + 105)
    q = b.qsos
    check("one qso from six messages", len(q) == 1 and q[0]["n"] == 5, str(q))
    check("qso confirmed, both heard", q[0]["stage"] == "confirmed" and sorted(q[0]["heard"]) == ["K1ABC", "W9XYZ"])
    check("grid and reports kept", q[0]["grid"] == {"W9XYZ": "EN37"} and q[0]["rpt"] == {"K1ABC": "-07", "W9XYZ": "+12"})
    b.add({"t": t + 3600, "snr": 1, "dt": 0, "f": 1200, "msg": "K1ABC W9XYZ EN37"})
    check("same pair an hour later is a new qso", len(b.qsos) == 2)
    b.add({"t": t + 3615, "snr": 1, "dt": 0, "f": 1200, "msg": "G0XYZ PA9XYZ R-15"})
    check("one-sided qso followed", b.qsos[-1]["stage"] == "r-report" and b.qsos[-1]["heard"] == ["PA9XYZ"])
    hours = b.stats()["hours"]
    check("hour buckets: 8 slots, 7 with a signal, 6 messages, 1 qso, 6 FT8 slots, 1 other",
          hours[0][1:] == [8, 7, 6, 1, 6, 1] and hours[1][3:5] == [2, 2], str(hours))
    b2 = ft8msg.Book(json.loads(json.dumps(b.state())))
    check("state round trip", b2.stats()["hours"] == hours and b2.qsos == b.qsos)


# ---------- 3. PSK Reporter ----------
def test_pskreporter():
    """Datagram layout against the published format; a recording socket, nothing is sent."""

    class Sock:
        def __init__(self):
            self.out = []

        def sendto(self, data, addr):
            self.out.append((data, addr))

    now = [1_800_000_000.0]
    so = Sock()
    r = pskreporter.Reporter("PA9XYZ", "JO22ab", "fm-ft8-skimmer test", "vertical", sock=so, clock=lambda: now[0], log=lambda *a: None)
    r.add("K1ABC", "FN42", 12, 144575000, now[0] - 30)
    r.add("K1ABC", None, 15, 144575000, now[0] - 15)
    r.add("W9XYZ", None, -3, 144575000, now[0])
    check("psk: nothing before five minutes", r.tick() == 0 and not so.out)
    now[0] += 331
    check("psk: two spots after five minutes", r.tick() == 2 and len(so.out) == 1 and so.out[0][1] == ("report.pskreporter.info", 4739))
    p = so.out[0][0]
    check("psk: header (version, length, time, sequence 0)", p[:2] == b"\x00\x0a" and struct.unpack(">H", p[2:4])[0] == len(p)
          and struct.unpack(">II", p[4:12]) == (int(now[0]), 0) and len(p) % 4 == 0)
    check("psk: both descriptors in the first datagram", p[16:16 + 44] == pskreporter.RX_TEMPLATE and p[60:60 + 60] == pskreporter.TX_TEMPLATE
          and struct.unpack(">H", pskreporter.RX_TEMPLATE[2:4])[0] == 44 and struct.unpack(">H", pskreporter.TX_TEMPLATE[2:4])[0] == 60)
    d = p[120:]
    n = struct.unpack(">H", d[2:4])[0]
    check("psk: receiver record", d[:2] == b"\x99\x92" and d[4:4 + 7] == b"\x06PA9XYZ" and b"\x06JO22ab" in d[:n] and n % 4 == 0)
    s = d[n:]
    want = (b"\x05K1ABC" + struct.pack(">Ib", 144575000, 15) + b"\x03FT8" + b"\x04FN42" + b"\x01" + struct.pack(">I", int(now[0] - 331 - 15)))
    check("psk: sender record (strongest report, locator kept)", s[:2] == b"\x99\x93" and struct.unpack(">H", s[2:4])[0] == len(s) and s[4:4 + len(want)] == want, s.hex())
    check("psk: second record with empty locator and negative SNR", b"\x05W9XYZ" + struct.pack(">Ib", 144575000, -3) + b"\x03FT8\x00\x01" in s)
    check("psk: same call not again within five minutes", r.add("K1ABC", "FN42", 20, 144575000, now[0]) is False)
    now[0] += 301
    r.add("K1ABC", "FN42", 20, 144575000, now[0])
    now[0] += 331
    r.tick()
    check("psk: sequence counts spots", struct.unpack(">I", so.out[1][0][8:12])[0] == 2 and so.out[1][0][12:16] == p[12:16])
    for _ in range(3):
        now[0] += 400
        r.add("K1ABC", "FN42", 20, 144575000, now[0])
        now[0] += 331
        r.tick()
    check("psk: descriptors only in the first three datagrams", [pskreporter.TX_TEMPLATE in x[0] for x in so.out] == [True, True, True, False, False])


# ---------- 4. the page server ----------
def fetch(port, path, method="GET", headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_web():
    page = web.Page("127.0.0.1", 0, HERE / "web", log=lambda *a: None)
    try:
        code, hdr, body = fetch(page.port, "/")
        check("web: the page is served", code == 200 and b"<!doctype html>" in body[:40].lower() and hdr["Content-Type"].startswith("text/html"))
        check("web: security headers", "default-src 'none'" in hdr.get("Content-Security-Policy", "") and hdr.get("X-Content-Type-Options") == "nosniff"
              and hdr.get("X-Frame-Options") == "DENY")
        for f in ("app.js", "style.css", "theme.js", "mqtt-lite.js", "favicon.svg"):
            check(f"web: {f}", fetch(page.port, "/" + f)[0] == 200)
        index = (HERE / "web" / "index.html").read_text()
        check("web: the page has no inline script or style (the content policy would block it)",
              "<style" not in index and " style=" not in index and "onclick" not in index
              and all('src="' in s for s in index.split("<script")[1:]))
        check("web: data starts empty", fetch(page.port, "/data/status.json")[2] == b"null" and fetch(page.port, "/data/config.json")[2] == b'{"mqtt":null}')
        page.put("status", {"state": "listening"})
        code, hdr, body = fetch(page.port, "/data/status.json?x=1")
        check("web: data is replaced", json.loads(body) == {"state": "listening"} and hdr["Content-Type"] == "application/json")
        check("web: unchanged data answers 304", fetch(page.port, "/data/status.json", headers={"If-None-Match": hdr["ETag"]})[0] == 304)
        page.put("decode", {"x": 1})
        check("web: only status, recent and stats exist", fetch(page.port, "/data/decode.json")[0] == 404)
        for bad in ("/../skimmer.py", "/..%2fskimmer.py", "/web/../config.example.ini", "//etc/passwd", "/data/", "/index.html/", "/.git/config"):
            check(f"web: {bad} is not found", fetch(page.port, bad)[0] == 404)
        check("web: POST is refused", fetch(page.port, "/data/status.json", method="POST")[0] in (405, 501))
        check("web: PUT is refused", fetch(page.port, "/index.html", method="PUT")[0] in (405, 501))
        check("web: HEAD has no body", fetch(page.port, "/", method="HEAD")[2] == b"")
    finally:
        page.close()


# ---------- 5. the MQTT publisher ----------
def test_mqtt():
    """Against a stand-in broker on localhost (no TLS): the CONNECT packet, a retained and a plain PUBLISH."""
    import socket
    import threading
    import time

    got = {}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def broker():
        c, _ = srv.accept()
        c.settimeout(5)
        buf = b""

        def packet():
            nonlocal buf
            while True:
                if len(buf) >= 2:
                    n, m, i = 0, 1, 1
                    while i < len(buf):
                        n += (buf[i] & 127) * m
                        m *= 128
                        i += 1
                        if not buf[i - 1] & 128:
                            if len(buf) >= i + n:
                                head, body, buf = buf[0], buf[i:i + n], buf[i + n:]
                                return head, body
                            break
                buf += c.recv(4096)

        try:
            got["connect"] = packet()
            c.sendall(b"\x20\x02\x00\x00")
            got["first"] = packet()
            got["second"] = packet()
        except OSError as e:
            got["error"] = e
        c.close()

    th = threading.Thread(target=broker, daemon=True)
    th.start()
    m = mqttc.Mqtt("127.0.0.1", srv.getsockname()[1], "user", "secret", "test-client", will=("t/status", {"state": "offline"}),
                   tls=False, log=lambda *a: None)
    m.send("t/status", {"state": "listening"}, retain=True)        # before the connection is up: kept, sent on connect
    for _ in range(100):
        if m.sock is not None:
            break
        time.sleep(0.05)
    m.send("t/decode", {"msg": "CQ K1ABC FN42"})
    th.join(5)
    m.close()
    srv.close()
    head, body = got.get("connect", (0, b""))
    check("mqtt: CONNECT with client id, will, username and password",
          head == 0x10 and body[:7] == b"\x00\x04MQTT\x04" and body[7] == 0xE6
          and all(x in body for x in (b"\x00\x0btest-client", b"\x00\x08t/status", b'{"state": "offline"}', b"\x00\x04user", b"\x00\x06secret")), str(got))
    check("mqtt: the retained status is sent after connecting", got.get("first") == (0x31, b"\x00\x08t/status" + b'{"state":"listening"}'), str(got.get("first")))
    check("mqtt: a decode is published unretained", got.get("second") == (0x30, b"\x00\x08t/decode" + b'{"msg":"CQ K1ABC FN42"}'), str(got.get("second")))


# ---------- 6. the signal path ----------
def ft8_audio(msg, f0):
    """The FT8 audio WSJT-X would send for msg: 12 kHz, +-1, 15 s, the signal from 0.5 s to 13.14 s."""
    a, b = SYMBOLS[msg]
    tones = np.array([int(c) for c in SYNC + a + SYNC + b + SYNC], dtype=np.float64)
    sps = int(0.16 * skimmer.AUDIO_FS)
    f = np.repeat(f0 + 6.25 * tones, sps)
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * 12.5) * skimmer.AUDIO_FS          # Gaussian smoothing, BT = 2
    k = np.arange(-int(4 * sigma), int(4 * sigma) + 1)
    g = np.exp(-0.5 * (k / sigma) ** 2)
    f = np.convolve(np.pad(f, len(k) // 2, mode="edge"), g / g.sum(), mode="valid")
    sig = np.sin(2 * np.pi * np.cumsum(f) / skimmer.AUDIO_FS)
    out = np.zeros(skimmer.SLOT * skimmer.AUDIO_FS, np.float32)
    out[skimmer.AUDIO_FS // 2: skimmer.AUDIO_FS // 2 + len(sig)] = sig
    return out


def upsample(a):
    """12 kHz -> FS by linear interpolation (plenty for a modulator input)."""
    r = skimmer.FS // skimmer.AUDIO_FS
    n = skimmer.SLOT * skimmer.FS
    x = np.arange(min(n, len(a) * r)) / r
    up = np.interp(x, np.arange(len(a)), a.real)
    if np.iscomplexobj(a):
        up = up + 1j * np.interp(x, np.arange(len(a)), a.imag)
    return np.concatenate([up, np.zeros(n - len(up), up.dtype)])


def to_u8(x):
    out = np.empty(2 * len(x), np.float32)
    out[0::2], out[1::2] = x.real, x.imag
    return np.clip(np.round(out * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()


NOISE_SD = 0.05                                                    # per I/Q component
NOISE_CH = 2 * NOISE_SD ** 2 * skimmer.CH_BW / skimmer.FS           # noise power in the channel width


def noise(rng):
    n = skimmer.SLOT * skimmer.FS
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * NOISE_SD).astype(np.complex64)


def fm_iq(audio, dev, cnr_db, rng, shift=0, key_s=13, gate=None):
    """Audio (12 kHz, +-1) as an FM signal OFFSET below the tuner, in rtl_sdr's u8 IQ at FS; cnr in the channel width."""
    n = skimmer.SLOT * skimmer.FS
    x = noise(rng)
    if audio is not None:
        up = upsample(audio)
        on = np.concatenate([np.ones(int(key_s * skimmer.FS)), np.zeros(n)])[:n]       # keyed for part of the slot
        if gate is not None:                                                            # on/off keying (12 kHz pattern)
            on = on * np.concatenate([np.repeat(gate, skimmer.FS // skimmer.AUDIO_FS), np.zeros(n)])[:n]
        phase = 2 * np.pi * np.cumsum(up * dev + shift - skimmer.OFFSET) / skimmer.FS
        x = x + (on * np.sqrt(NOISE_CH * 10 ** (cnr_db / 10)) * np.exp(1j * phase)).astype(np.complex64)
    return to_u8(x)


def ssb_iq(audio, rf_hz, cnr_db, rng):
    """Audio (12 kHz) as an upper-sideband signal whose suppressed carrier sits rf_hz from the channel centre."""
    n = skimmer.SLOT * skimmer.FS
    spec = np.fft.fft(audio)
    spec[len(spec) // 2 + 1:] = 0                                   # analytic signal: positive frequencies only
    spec[1: len(spec) // 2] *= 2
    up = upsample(np.fft.ifft(spec))
    up = up / np.abs(up).max()
    k = np.arange(n)
    x = noise(rng) + (np.sqrt(NOISE_CH * 10 ** (cnr_db / 10)) * up * np.exp(2j * np.pi * (rf_hz - skimmer.OFFSET) / skimmer.FS * k)).astype(np.complex64)
    return to_u8(x)


def band_noise(rng, n, lo, hi):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / skimmer.AUDIO_FS)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, n)


def test_signal_path(jt9):
    rng = np.random.default_rng(1)
    tmp = tempfile.mkdtemp(prefix="fmft8-test-")
    cfg = config_from(f"[station]\ncallsign = PA9XYZ\nlocator = JO22\n[storage]\ndirectory = {tmp}/data\nkeep_audio = 2\n", tmp)
    rec = mqttc.Dry()
    st = skimmer.Station(cfg, skimmer.Outputs(broker=rec, topic="test"), jt9=jt9)
    t = 1_800_000_000 // 15 * 15
    cases = [("CQ K1ABC FN42", 1500, 2500, 25), ("K1ABC W9XYZ EN37", 1100, 5000, 25), ("W9XYZ K1ABC -07", 1900, 2500, 14)]
    for i, (msg, f0, dev, cnr) in enumerate(cases):
        st.slot(t + 15 * i, fm_iq(ft8_audio(msg, f0), dev, cnr, rng))
        d = st.book.decodes[-1] if st.book.decodes else {}
        check(f"decoded {msg!r} (deviation {dev} Hz, {cnr} dB CNR)",
              d.get("msg") == msg and abs(d.get("f", 0) - f0) <= 3 and d.get("t") == t + 15 * i, f"{d} last={st.last}")
        check(f"  carrier measured near {cnr} dB", abs(st.last["peak_db"] - cnr) < 3 and 12 <= st.last["carrier_s"] <= 14 and st.last["what"] == "FT8", str(st.last))
    st.slot(t + 60, fm_iq(None, 0, 0, rng))
    check("noise only: no decode, no carrier", st.last["n"] == 0 and st.last["carrier_s"] == 0 and len(st.book.decodes) == 3, str(st.last))
    st.slot(t + 75, fm_iq(np.zeros(12000, np.float32), 2500, 20, rng))
    check("unmodulated carrier: noted as such, no decode",
          st.book.carriers[-1]["t"] == t + 75 and st.book.carriers[-1]["n"] == 0 and st.book.carriers[-1]["kind"] == "carrier"
          and len(st.book.decodes) == 3, str(st.book.carriers[-1:]))
    n = 13 * 12000
    tt = np.arange(n) / 12000
    syll = np.clip(np.sin(2 * np.pi * 2.3 * tt) + 0.3 * np.sin(2 * np.pi * 0.7 * tt), 0, None)          # syllables and pauses
    voice = band_noise(rng, n, 300, 2600) * syll
    voice = voice / np.abs(voice).max()
    afsk = np.sin(2 * np.pi * np.cumsum(np.where(rng.integers(0, 2, n // 10).repeat(10), 1200, 2200)) / 12000)
    ft8 = ft8_audio("CQ K1ABC FN42", 800)
    others = [("voice", voice, 3000, 0, "voice"), ("1750 Hz tone", np.sin(2 * np.pi * 1750 * tt), 2500, 0, "tone"),
              ("AFSK packet", afsk, 3000, 0, "packet"), ("FT8 keyed for 3 s only", ft8[: 3 * 12000], 400, 0, "ft8x"),
              ("carrier 5 kHz off", np.zeros(n), 2500, 5000, "carrier")]
    for j, (name, audio, dev, shift, kind) in enumerate(others):
        iq = fm_iq(audio.astype(np.float32), dev, 25, rng, shift=shift, key_s=min(13, len(audio) / 12000))
        st.slot(t + 90 + 15 * j, iq)
        c = st.book.carriers[-1]
        check(f"other signal identified: {name} -> {kind}", c["kind"] == kind and c["n"] == 0 and len(st.book.decodes) == 3, str(c))
    check("offset noted", "+5.0 kHz off the channel" in st.book.carriers[-1]["label"], st.book.carriers[-1]["label"])
    dits = (np.sin(2 * np.pi * 6 * tt) > 0).astype(float)                                              # a string of dits
    ssb = [("SSB voice", voice, -1500, "ssb"), ("CW", dits, 800, "cw"),
           ("FT8 sent in SSB", ft8_audio("CQ G0XYZ IO91", 1200)[: 13 * 12000 + 6000], -900, "ssb-ft8")]
    for j, (name, audio, rf_hz, kind) in enumerate(ssb):
        iq = ssb_iq(audio, rf_hz, 30, rng) if kind != "cw" else fm_iq((audio * 0).astype(np.float32), 0, 30, rng, shift=rf_hz, key_s=13, gate=audio)
        st.slot(t + 165 + 15 * j, iq)
        c = st.book.carriers[-1]
        check(f"not FM, identified: {name} -> {kind}", c["kind"] == kind and c["n"] == 0 and len(st.book.decodes) == 3, str(c))
    check("FT8 in SSB is named but not counted", "CQ G0XYZ IO91" in st.book.carriers[-1]["label"] and len(st.book.qsos) == 1
          and not any(d["msg"].startswith("CQ G0XYZ") for d in st.book.decodes), st.book.carriers[-1]["label"])
    topics = [x[0] for x in rec.sent]
    check("published decode, recent, stats, status", all(f"test/{k}" in topics for k in ("decode", "recent", "stats", "status")))
    status = [x[1] for x in rec.sent if x[0] == "test/status"][-1]
    check("status carries the station, never the settings", status["call"] == "PA9XYZ" and status["loc"] == "JO22"
          and not any(k in status for k in ("password", "username", "host", "directory")), str(status))
    check("qso K1ABC/W9XYZ at stage report", st.book.qsos and st.book.qsos[-1]["stage"] == "report", str(st.book.qsos))
    check("hour bucket: 13 slots, 12 with a signal, 3 messages, 1 qso, 3 FT8, 9 other", st.book.stats()["hours"][-1][1:] == [13, 12, 3, 1, 3, 9], str(st.book.stats()["hours"]))
    kept = sorted((cfg.data / "undecoded").glob("*.wav"))
    check("undecoded audio: only as many as asked for", len(kept) == 2, str(kept))
    check("every decode is on disk", len((cfg.data / "decodes.ndjson").read_text().splitlines()) == 3)
    st.close()
    st2 = skimmer.Station(cfg, skimmer.Outputs(), jt9=jt9)
    check("state survives a restart", len(st2.book.decodes) == 3)
    st2.close()


if __name__ == "__main__":
    test_config()
    test_state_file()
    test_messages()
    test_pskreporter()
    test_web()
    test_mqtt()
    jt9 = skimmer.find_jt9("jt9")
    if jt9:
        test_signal_path(jt9)
    else:
        print("SKIP the signal path: jt9 (WSJT-X) is not installed")
    print("\nself-test", "FAILED: " + ", ".join(FAIL) if FAIL else "passed" + ("" if jt9 else " (signal path skipped)"))
    sys.exit(1 if FAIL else 0)
