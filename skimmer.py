#!/usr/bin/env python3
"""fm-ft8-skimmer - receive-only skimmer for FM-FT8: standard FT8 sent through ordinary FM radios.

Listens on one FM channel (144.575 MHz unless configured otherwise) with an RTL-SDR, cuts the
demodulated audio into the 15 s FT8 slots, decodes each slot with WSJT-X's jt9 and shows what it
heard on a small web page. Optionally reports the stations heard to PSK Reporter and publishes
everything to an MQTT broker.

    rtl_sdr (240 kS/s IQ, tuned 30 kHz above the channel to keep the DC spike out)
      -> channel filter -> FM discriminator -> 12 kHz audio -> jt9 -8

Working on IQ rather than on rtl_fm audio also gives the RF level of the channel, so a carrier
that did not decode is noted too, with a guess at what it was.

Usage:
    python3 skimmer.py [--config FILE]     run
    python3 skimmer.py --check             check that everything needed is there, then exit
    python3 skimmer.py --version

Without a config file it runs with the defaults: 144.575 MHz, first RTL-SDR, page on
http://127.0.0.1:8073, nothing sent anywhere. See config.example.ini.
"""
import argparse
import configparser
import json
import os
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import ft8msg
import mqttc
import pskreporter
import web

VERSION = "1.0.0"

FS = 240000            # IQ rate from rtl_sdr
OFFSET = 30000         # the tuner sits this far above the channel
SLOT = 15              # FT8 time slot, seconds
IF_FS = 24000          # channel at baseband
AUDIO_FS = 12000       # what jt9 wants
CH_BW = 16000          # nominal channel width (wide enough for 5 kHz deviation)
CH_PASS, CH_STOP = 7000, 10000     # channel filter: flat to +-7 kHz, gone at +-10 kHz
AF_PASS, AF_STOP = 3000, 3600      # audio low-pass
CARRIER_DB = 6.0       # channel power over the noise floor that counts as a signal
MAX_LOG_BYTES = 50 * 1024 * 1024   # decodes.ndjson is rotated once at this size
STALL_S = 90           # a running receiver that delivered no slot for this long is restarted

LINE_RE = re.compile(r"^\d{6}\s+(-?\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+~\s+(.*?)\s*$")
LOCATOR_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?$", re.I)
STATION_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9/-]{3,15}$")      # a callsign or a listener (SWL) identifier
JT9_PLACES = ("/usr/bin/jt9", "/usr/local/bin/jt9", "/usr/local/wsjtx/jt9", "/opt/wsjtx/bin/jt9",
              "/Applications/wsjtx.app/Contents/MacOS/jt9")

DEFAULTS = {
    "station": {"callsign": "", "locator": "", "antenna": "", "receiver": "RTL-SDR"},
    "receiver": {"frequency": "144.575", "device": "0", "gain": "auto", "ppm": "0", "pause_when": ""},
    "decoder": {"jt9": "jt9"},
    "web": {"enabled": "yes", "bind": "127.0.0.1", "port": "8073"},
    "pskreporter": {"enabled": "no"},
    "mqtt": {"enabled": "no", "host": "", "port": "8883", "tls": "yes", "username": "", "password": "",
             "topic": "fmft8"},
    "storage": {"directory": "~/.local/share/fm-ft8-skimmer", "keep_audio": "0"},
}
DEFAULT_CONFIG = "~/.config/fm-ft8-skimmer/config.ini"


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------- configuration ----------
class ConfigError(Exception):
    pass


class Config:
    """The settings, checked. Raises ConfigError with a message a person can act on."""

    def __init__(self, path=None, must_exist=False):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        self.path = Path(os.path.expanduser(path or os.environ.get("FMFT8_CONFIG") or DEFAULT_CONFIG))
        self.found = self.path.is_file()
        self.notes = []
        if self.found:
            try:
                cp.read(self.path, encoding="utf-8")
            except configparser.Error as e:
                raise ConfigError(f"{self.path}: {e}") from None
        elif must_exist:
            raise ConfigError(f"config file not found: {self.path}")
        for sec in cp.sections():
            if sec not in DEFAULTS:
                raise ConfigError(f"{self.path}: unknown section [{sec}]")
            for key in cp[sec]:
                if key not in DEFAULTS[sec]:
                    raise ConfigError(f"{self.path}: unknown setting '{key}' in [{sec}]")

        def get(sec, key):
            return cp[sec][key].strip()

        def yes(sec, key):
            try:
                return cp[sec].getboolean(key)
            except ValueError:
                raise ConfigError(f"[{sec}] {key} must be yes or no") from None

        def num(sec, key, lo, hi, kind=int):
            try:
                v = kind(get(sec, key))
            except ValueError:
                raise ConfigError(f"[{sec}] {key} must be a number") from None
            if not lo <= v <= hi:
                raise ConfigError(f"[{sec}] {key} must be between {lo} and {hi}")
            return v

        self.callsign = get("station", "callsign").upper()
        if self.callsign and not STATION_RE.match(self.callsign):
            raise ConfigError(f"[station] callsign '{self.callsign}' does not look like a callsign or listener identifier")
        loc = get("station", "locator")
        if loc and not LOCATOR_RE.match(loc):
            raise ConfigError(f"[station] locator '{loc}' is not a Maidenhead locator such as FN42 or FN42ab")
        self.locator = loc[:4].upper() + loc[4:].lower()
        self.antenna = get("station", "antenna")[:60]
        self.receiver = get("station", "receiver")[:60]

        f = num("receiver", "frequency", 0.1, 3e9, float)
        self.freq = int(round(f * 1e6 if f < 100000 else f))      # MHz or Hz
        self.device = get("receiver", "device")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", self.device):
            raise ConfigError("[receiver] device must be an RTL-SDR index or serial number")
        g = get("receiver", "gain").lower()
        self.gain = "0" if g == "auto" else str(num("receiver", "gain", 0, 60, float))
        self.ppm = str(num("receiver", "ppm", -200, 200))
        try:
            self.pause_when = shlex.split(get("receiver", "pause_when"))
        except ValueError as e:
            raise ConfigError(f"[receiver] pause_when: {e}") from None

        self.jt9 = get("decoder", "jt9") or "jt9"

        self.web = yes("web", "enabled")
        self.web_bind = get("web", "bind") or "127.0.0.1"
        self.web_port = num("web", "port", 1, 65535)

        self.psk = yes("pskreporter", "enabled")
        if self.psk and not (self.callsign and self.locator):
            raise ConfigError("[pskreporter] needs [station] callsign and locator: reports are sent under your callsign")

        self.mqtt = yes("mqtt", "enabled")
        self.mqtt_host = get("mqtt", "host")
        self.mqtt_port = num("mqtt", "port", 1, 65535)
        self.mqtt_tls = yes("mqtt", "tls")
        self.mqtt_user, self.mqtt_pass = get("mqtt", "username"), cp["mqtt"]["password"]
        self.mqtt_topic = get("mqtt", "topic").strip("/")
        if self.mqtt:
            if not self.mqtt_host:
                raise ConfigError("[mqtt] is enabled but has no host")
            if not self.mqtt_topic or re.search(r"[#+\s]", self.mqtt_topic):
                raise ConfigError("[mqtt] topic must be a plain topic without wildcards or spaces")
            if not self.mqtt_tls and self.mqtt_pass:
                self.notes.append("[mqtt] tls = no: the broker password crosses the network unencrypted")
            if self.mqtt_pass and self.found and os.name == "posix" and self.path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
                self.notes.append(f"{self.path} holds a password and is readable by other users: chmod 600 it")

        self.data = Path(os.path.expanduser(get("storage", "directory")))
        self.keep_audio = num("storage", "keep_audio", 0, 1000)
        if self.web and self.web_bind not in ("127.0.0.1", "::1", "localhost"):
            self.notes.append(f"the page listens on {self.web_bind}: anyone who can reach that address can read it")

    def public(self):
        """What the page may show about the station."""
        return {"call": self.callsign or None, "loc": self.locator or None, "antenna": self.antenna or None,
                "sdr": self.receiver or None, "psk": self.psk}


def find_jt9(name):
    p = shutil.which(name)
    if p:
        return p
    if name == "jt9":
        for cand in JT9_PLACES:
            if os.access(cand, os.X_OK):
                return cand
    return None


# ---------- signal path ----------
def _taper(f, f_pass, f_stop):
    """1 up to f_pass, raised-cosine roll-off to 0 at f_stop."""
    w = np.clip((f_stop - np.abs(f)) / (f_stop - f_pass), 0.0, 1.0)
    return (0.5 - 0.5 * np.cos(np.pi * w)).astype(np.float32)


def channelize(x):
    """IQ at FS with the channel OFFSET below the centre -> the channel at baseband, complex at IF_FS.

    Mixing, filtering and decimation in one step in the frequency domain (numpy only). The slot is
    worked through in blocks of one second that overlap by 25 ms on each side, the overlap being thrown
    away afterwards: that keeps the memory needed small (a Raspberry Pi has little) and the filter's
    wrap-around out of the result."""
    dec = FS // IF_FS
    block, pad = FS, FS // 40                             # 240000 + 2 * 6000 = 252000 = 2^5 3^2 5^3 7: a fast FFT size
    size = block + 2 * pad
    m = size // dec
    rel = np.arange(-(m // 2), m // 2)
    pick = (int(round(-OFFSET / FS * size)) + rel) % size
    shape = _taper(rel * (FS / size), CH_PASS, CH_STOP).astype(np.complex64)
    n = len(x) // dec * dec
    out = np.empty(n // dec, np.complex64)
    buf = np.zeros(size, np.complex64)
    for start in range(0, n, block):
        lo, hi = max(0, start - pad), min(n, start + block + pad)
        buf[:] = 0
        buf[lo - (start - pad): hi - (start - pad)] = x[lo:hi]
        y = np.fft.ifft(np.fft.ifftshift(np.fft.fft(buf)[pick] * shape))
        take = min(block, n - start) // dec
        out[start // dec: start // dec + take] = y[pad // dec: pad // dec + take] / dec
    return out


def discriminate(y):
    """Complex baseband at IF_FS -> FM-demodulated audio at AUDIO_FS, float, in radians per sample."""
    d = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float32)
    n = len(d) // 4 * 4
    spec = np.fft.rfft(d[:n])
    keep = n // 4 + 1                                   # 0 .. AUDIO_FS / 2
    f = np.arange(keep) * (IF_FS / n)
    return np.fft.irfft(spec[:keep] * _taper(f, AF_PASS, AF_STOP), n // 2) / 2


def demodulate(raw):
    """One slot of rtl_sdr bytes (u8 IQ at FS) -> (int16 audio at 12 kHz, rf dict, aux).

    rf: noise_db (floor in the channel width, dB re full scale), peak_db / mean_db (channel power over
    that floor), carrier_s (seconds the channel was more than CARRIER_DB above it).
    aux, for identifying what is not FT8 via FM: on (per 0.25 s, whether a signal stood on the channel),
    env_cv (per 0.25 s, how much the envelope varies: ~0 for FM, large for SSB and CW), bw (occupied RF
    bandwidth in Hz while on), y (the channel at 24 kHz, complex).
    """
    x = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    x -= 127.5
    x *= 1 / 127.5
    x = x.view(np.complex64)
    # noise floor: median bin of the averaged spectrum (signals occupy few bins), scaled to the channel
    nfft = 4096
    seg = x[: len(x) // nfft * nfft].reshape(-1, nfft)[::8]
    win = np.hanning(nfft).astype(np.float32)
    psd = (np.abs(np.fft.fft(seg * win, axis=1)) ** 2).mean(axis=0)
    psd /= (win ** 2).sum() * FS                         # power per Hz
    noise = float(np.median(psd)) * CH_BW + 1e-12
    y = channelize(x)
    blk = IF_FS // 4                                     # 0.25 s
    p = (np.abs(y[: len(y) // blk * blk]) ** 2).reshape(-1, blk).mean(axis=1) / noise
    on = p > 10 ** (CARRIER_DB / 10)
    rf = {"noise_db": round(float(10 * np.log10(noise)), 1),
          "peak_db": round(float(10 * np.log10(p.max() + 1e-9)), 1),
          "mean_db": round(float(10 * np.log10(p.mean() + 1e-9)), 1),
          "carrier_s": round(float(on.sum() * 0.25), 2)}
    env = np.abs(y[: len(p) * blk]).reshape(-1, blk)
    aux = {"on": on, "env_cv": env.std(axis=1) / (env.mean(axis=1) + 1e-12), "bw": None, "y": y}
    if on.any():
        nf = 2048
        seg = y[: len(p) * blk].reshape(-1, blk)[on][:, : blk // nf * nf].reshape(-1, nf)
        ps = np.fft.fftshift((np.abs(np.fft.fft(seg * np.hanning(nf).astype(np.float32), axis=1)) ** 2).mean(axis=0))
        ps = np.clip(ps - np.median(ps) * 1.5, 0, None)             # what stands above the noise
        c = np.cumsum(ps) / (ps.sum() + 1e-12)
        aux["bw"] = float((np.searchsorted(c, 0.95) - np.searchsorted(c, 0.05) + 1) * IF_FS / nf)
    a = np.clip(discriminate(y) * (9000 / np.pi), -32767, 32767).astype("<i2")
    want = SLOT * AUDIO_FS
    a = a[:want] if len(a) >= want else np.concatenate([a, np.zeros(want - len(a), "<i2")])
    return a, rf, aux


def ssb_views(y):
    """The channel as an SSB (USB) receiver would hear it, 12 kHz int16 audio, for three dial settings:
    yields (audio, hz) with hz = RF offset from the channel centre that lands on 1500 Hz audio."""
    n = len(y) // 4 * 4
    k = np.arange(n, dtype=np.float32)
    for c in (0, -3000, 3000):
        z = y[:n] * np.exp(2j * np.pi * (1500 - c) / IF_FS * k).astype(np.complex64)
        a = np.fft.irfft(np.fft.fft(z)[: n // 4 + 1], n // 2)         # positive audio frequencies only, 12 kHz
        a = a / (a.std() + 1e-12) * 1500
        want = SLOT * AUDIO_FS
        a = a[:want] if len(a) >= want else np.concatenate([a, np.zeros(want - len(a))])
        yield np.clip(a, -32767, 32767).astype("<i2"), c


def classify(audio, aux, snr_db):
    """What a signal that is not decodable FT8 looks like, from the demodulated audio while it was on.

    -> dict(kind, label, off): kind burst | ssb | cw | ssb-data | tone | ft8x | data | packet | weak | carrier |
    voice | unknown, off = carrier offset from the channel centre in Hz. A heuristic for a usage log, not a
    decoder. FM keeps a constant envelope; a signal whose envelope swings is SSB (a few kHz wide) or CW
    (narrow). Within FM it separates a bare carrier, steady tones, FT8-like tone sequences, AFSK packet
    and speech-like audio.
    """
    fr, on = AUDIO_FS // 4, aux["on"]
    idx = [i for i in range(1, min(len(on), len(audio) // fr) - 1) if on[i - 1] and on[i] and on[i + 1]]
    if not idx:
        return {"kind": "burst", "label": "short burst", "off": None}
    f = np.fft.rfftfreq(fr, 1 / AUDIO_FS)
    band, win = (f >= 150) & (f <= 3300), np.hanning(fr)
    afsk_bins = (f >= 900) & (f <= 2500)                      # where 1200 Bd AFSK (1200 / 2200 Hz) keeps its energy
    dc, rms, conc, peak, afsk = [], [], [], [], []
    for i in idx:
        seg = audio[i * fr:(i + 1) * fr].astype(np.float64)
        dc.append(seg.mean())
        seg = seg - seg.mean()
        sp = np.abs(np.fft.rfft(seg * win)) ** 2
        tot = sp[band].sum() + 1e-9
        k = int(np.argmax(np.where(band, sp, 0)))
        rms.append(seg.std())
        conc.append(sp[max(0, k - 6):k + 7].sum() / tot)       # share of the energy within 24 Hz of the peak
        peak.append(f[k])
        afsk.append(sp[afsk_bins].sum() / tot)
    off = float(np.median(dc)) * 4 / 3                         # audio units -> Hz (9000 per pi rad/sample at 24 kHz)
    rms, conc, peak = np.array(rms), np.array(conc), np.array(peak)
    tonal = conc > 0.6
    out = {"off": int(round(off / 100) * 100)}
    away = f", {off / 1000:+.1f} kHz off the channel" if abs(off) > 2500 else ""
    cv, bw = float(np.median(aux["env_cv"][idx])), aux.get("bw") or 0.0
    quiet = 25 * 10 ** ((40 - min(snr_db, 40)) / 20)          # audio noise left on a bare carrier at this strength
    dc = np.array(dc) * 4 / 3
    if cv > 0.35 and snr_db >= 15:                             # the envelope swings: not FM
        if bw < 500:
            out.update(kind="cw", label="CW (keyed carrier), not FM")
        elif bw <= 4500:
            out.update(kind="ssb", label="SSB voice (probable), not FM")
        else:
            out.update(kind="unknown", label="signal with varying amplitude, not FM")
        out["off"] = None
        return out
    if bw < 300 and snr_db >= 15 and np.median(rms) < quiet and len(dc) >= 8 and 10 < np.percentile(dc, 95) - np.percentile(dc, 5) <= 80:
        out.update(kind="ssb-data", label="narrow carrier stepping in frequency: a digital mode in SSB (FT8-like), not FM")
    elif tonal.sum() >= 3 and tonal.mean() >= 0.3:
        pk = peak[tonal]
        lo, hi = (np.percentile(pk, [10, 90]) if len(pk) >= 5 else (pk.min(), pk.max()))
        if hi - lo <= 12:
            out.update(kind="tone", label=f"steady tone, {int(round(np.median(pk) / 10) * 10)} Hz")
        elif hi - lo <= 80:
            out.update(kind="ft8x", label=f"FT8-like tones near {int(round(np.median(pk) / 50) * 50)} Hz, not decoded")
        else:
            out.update(kind="data", label="data tones")
    elif np.median(afsk) > 0.8 and np.median(rms) >= 40 and rms.std() / (rms.mean() + 1e-9) < 0.15 and snr_db >= 12:
        out.update(kind="packet", label="data, AFSK-like (packet)")
    elif np.median(rms) < quiet:
        out.update(kind="carrier", label="unmodulated carrier")
    elif snr_db < 12:
        out.update(kind="weak", label="weak signal")
    elif rms.std() / (rms.mean() + 1e-9) > 0.3:
        out.update(kind="voice", label="FM voice (probable)")
    else:
        out.update(kind="unknown", label="modulated signal, not identified")
    out["label"] += away
    return out


def write_wav(path, audio):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_FS)
        w.writeframes(audio.tobytes())


def decode(jt9, audio, t, workdir):
    """jt9 on one slot of audio -> list of dict(t, snr, dt, f, msg)."""
    path = os.path.join(workdir, datetime.fromtimestamp(t, timezone.utc).strftime("%y%m%d_%H%M%S") + ".wav")
    write_wav(path, audio)
    try:
        r = subprocess.run([jt9, "-8", "-d", "2", "-p", "15", "-L", "100", "-H", "3300", "-a", workdir, "-t", workdir, path],
                           capture_output=True, text=True, errors="replace", timeout=40, cwd=workdir)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"jt9 failed: {e}")
        return []
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = []
    for line in r.stdout.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        msg = "".join(c for c in m.group(4) if " " <= c <= "~")[:40].strip()   # what was sent on the air: keep it plain
        if msg.endswith("?") or re.search(r"\s[aq]\d$", msg):
            # low confidence or a-priori assisted: on a channel that is mostly noise these are the false decodes
            continue
        out.append({"t": int(t), "snr": int(m.group(1)), "dt": float(m.group(2)), "f": int(m.group(3)), "msg": msg})
    return out


# ---------- where the results go ----------
class Outputs:
    """Hands every result to the page and, when configured, to the broker.
    Names: status, recent, stats (the current picture, replaced each time) and decode (one per message)."""

    def __init__(self, page=None, broker=None, topic="fmft8"):
        self.page, self.broker, self.topic = page, broker, topic

    def send(self, name, payload, retain=False):
        if self.page is not None and retain:
            self.page.put(name, payload)
        if self.broker is not None:
            self.broker.send(f"{self.topic}/{name}", payload, retain=retain)

    def close(self):
        if self.broker is not None:
            self.broker.close()
        if self.page is not None:
            self.page.close()


# ---------- the station ----------
class Station:
    def __init__(self, cfg, out, jt9="jt9", psk=None):
        self.cfg, self.out, self.jt9, self.psk = cfg, out, jt9, psk
        self.data = cfg.data
        self.data.mkdir(parents=True, exist_ok=True)
        self.book = self.load()
        self.state, self.reason, self.since = "starting", None, int(time.time())
        self.last = None            # last slot: t, rf, n
        self.stats_at = 0
        self.work = tempfile.mkdtemp(prefix="fmft8-", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)

    def load(self):
        """The saved state; a file that is missing or cannot be used means a fresh start, never a failure."""
        path = self.data / "state.json"
        try:
            book = ft8msg.Book(json.loads(path.read_text()))
            book.recent(), book.stats()
            json.dumps(book.state())
            return book
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"{path} cannot be used ({type(e).__name__}: {e}); kept as state.json.bad, starting fresh")
            try:
                os.replace(path, self.data / "state.json.bad")
            except OSError:
                pass
        return ft8msg.Book()

    def set_state(self, state, reason=None):
        if (state, reason) != (self.state, self.reason):
            log(f"state: {state}" + (f" ({reason})" if reason else ""))
            self.state, self.reason, self.since = state, reason, int(time.time())
            self.status()

    def status(self):
        self.out.send("status", {"state": self.state, "reason": self.reason, "since": self.since,
                                 "ts": int(time.time()), "freq": self.cfg.freq, "mode": "FT8 via FM",
                                 "last": self.last, "v": VERSION, **self.cfg.public()}, retain=True)

    def save(self):
        try:
            tmp = self.data / "state.json.tmp"
            tmp.write_text(json.dumps(self.book.state(), separators=(",", ":")))
            os.replace(tmp, self.data / "state.json")
        except OSError as e:                         # disk full, directory gone: keep listening
            log(f"state not saved: {e}")

    def record(self, rec):
        """Every decode ever, one JSON object per line; rotated once so it cannot fill the disk."""
        path = self.data / "decodes.ndjson"
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                os.replace(path, self.data / "decodes.1.ndjson")
        except OSError:
            pass
        try:
            with open(path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError as e:
            log(f"decode not written to {path}: {e}")

    def slot(self, t, raw):
        """A complete slot of IQ: demodulate, decode, book, publish."""
        audio, rf, aux = demodulate(raw)
        found = decode(self.jt9, audio, t, self.work)
        carrier = {"peak_db": rf["peak_db"], "dur_s": rf["carrier_s"]} if rf["carrier_s"] >= 0.5 else None
        kept = []
        for d in found:
            d["rf"] = rf["peak_db"]
            rec = self.book.add(d)
            if rec is None:
                log(f"ignored (no call exchange): {d['msg']!r}")
                continue
            kept.append(rec)
            log(f"decode {d['snr']:+d} dB dt {d['dt']:+.1f} {d['f']} Hz  {d['msg']}  (rf {rf['peak_db']:+.0f} dB)")
            self.record(rec)
            self.out.send("decode", rec)
            if self.psk is not None:
                self.psk.add(rec["de"], rec.get("grid"), rec["snr"], self.cfg.freq, t)
        if carrier and not kept:
            carrier.update(self.identify(t, audio, aux, rf))
            log(f"other signal: {carrier['label']}, {rf['carrier_s']:.1f} s, {rf['peak_db']:+.0f} dB over the floor")
            self.keep_audio(t, audio)
        elif kept:
            carrier = {"peak_db": rf["peak_db"], "dur_s": rf["carrier_s"], "kind": "ft8", "label": "FT8"}
        self.book.slot(t, carrier, len(kept))
        self.last = {"t": int(t), "n": len(kept), "what": carrier["label"] if carrier else None, **rf}
        self.status()
        if kept or carrier:
            self.out.send("recent", self.book.recent(), retain=True)
        if kept or carrier or time.time() - self.stats_at > 300:
            self.out.send("stats", self.book.stats(), retain=True)
            self.stats_at = time.time()
            self.save()

    def identify(self, t, audio, aux, rf):
        """A signal that is not FT8 via FM: FT8 sent in SSB is read (and named, never counted or spotted),
        everything else is classified from its envelope, bandwidth and audio."""
        if rf["carrier_s"] >= 10 and (aux.get("bw") or 1e9) < 300:
            for view, hz in ssb_views(aux["y"]):
                got = [d for d in decode(self.jt9, view, t, self.work) if ft8msg.parse(d["msg"])]
                if got:
                    off = hz + got[0]["f"] - 1500
                    return {"kind": "ssb-ft8", "off": int(off),
                            "label": f"FT8 in SSB, not FM: {got[0]['msg']} ({off / 1000:+.1f} kHz from the channel centre)"}
        return classify(audio, aux, rf["peak_db"])

    def keep_audio(self, t, audio):
        """Optionally keep the audio of slots with a signal that did not decode, for diagnosis.
        Off unless [storage] keep_audio says how many: such audio can contain other people's voices."""
        if self.cfg.keep_audio <= 0:
            return
        try:
            d = self.data / "undecoded"
            d.mkdir(exist_ok=True)
            write_wav(d / (datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".wav"), audio)
            for old in sorted(d.glob("*.wav"))[:-self.cfg.keep_audio]:
                old.unlink()
        except OSError as e:
            log(f"audio not kept: {e}")

    def announce(self):
        self.status()
        self.out.send("recent", self.book.recent(), retain=True)
        self.out.send("stats", self.book.stats(), retain=True)
        self.stats_at = time.time()

    def close(self):
        self.save()
        shutil.rmtree(self.work, ignore_errors=True)


class Receiver:
    """rtl_sdr on the stick; hands complete, clock-aligned 15 s slots of IQ bytes to a queue."""

    QUIET = ("Found", "  ", "Using", "Detached", "Sampling", "Tuned", "Tuner", "Reading", "Exact", "[R82XX]", "Allocating")

    def __init__(self, cfg, out):
        self.cfg, self.out, self.proc, self.started = cfg, out, None, 0.0

    def start(self):
        c = self.cfg
        cmd = ["rtl_sdr", "-f", str(c.freq + OFFSET), "-s", str(FS), "-g", c.gain, "-p", c.ppm, "-d", c.device, "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # buffered: read(n) waits for n bytes
        self.started = time.time()
        threading.Thread(target=self._errs, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _errs(self, p):
        for line in p.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line and not line.startswith(self.QUIET):
                log("rtl_sdr:", line)

    def _read(self, p):
        chunk, per_s = FS // 5 * 2, FS * 2          # 0.2 s of bytes
        cur, parts, have = None, [], 0
        while True:
            b = p.stdout.read(chunk)
            now = time.time()
            if len(b) < chunk:
                return
            slot = int(now // SLOT)
            if cur is None:                          # first chunk: start counting at the next boundary
                cur = slot + 1
                continue
            if slot < cur:
                continue
            if slot == cur and not parts and have == 0:
                # the chunk that crosses into our first slot: keep only the part after the boundary
                keep = min(chunk, int((now - cur * SLOT) * per_s) & ~1)
                parts, have = [b[chunk - keep:]], keep
                continue
            if slot == cur:
                parts.append(b)
                have += chunk
                continue
            # the boundary lies inside this chunk
            tail = min(chunk, int((now - slot * SLOT) * per_s) & ~1)
            parts.append(b[: chunk - tail])
            raw = b"".join(parts)
            want = SLOT * per_s
            if slot == cur + 1 and abs(len(raw) - want) < per_s // 2:
                raw = raw[:want] if len(raw) >= want else b"\x80" * (want - len(raw)) + raw
                try:
                    self.out.put_nowait((cur * SLOT, raw))
                except queue.Full:
                    log("decoder too slow, slot dropped")
            else:
                log(f"slot {cur * SLOT} dropped: {len(raw) / per_s:.1f} s of samples")
            cur, parts, have = slot, [b[chunk - tail:]], tail

    def stop(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


# ---------- running ----------
def must_pause(cmd):
    """True while the operator's pause_when command succeeds: another program wants the stick.
    The command is run directly, without a shell; one that fails or hangs never pauses the skimmer."""
    if not cmd:
        return False
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def trial(jt9):
    """One synthetic slot (FT8 via FM, as in the self-test) through the whole decoding path.
    -> (seconds it took, whether the message was decoded). Tells whether this computer is fast enough."""
    import selftest
    iq = selftest.fm_iq(selftest.ft8_audio("CQ K1ABC FN42", 1500), 2500, 20, np.random.default_rng(1))
    work = tempfile.mkdtemp(prefix="fmft8-")
    try:
        t0 = time.perf_counter()
        audio, _, _ = demodulate(iq)
        found = decode(jt9, audio, 1_800_000_000, work)
        return time.perf_counter() - t0, any(d["msg"] == "CQ K1ABC FN42" for d in found)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check(cfg):
    """Everything the skimmer needs, one line each. Returns True when it can run."""
    ok = True

    def line(good, what, detail):
        nonlocal ok
        ok = ok and good
        print(("ok    " if good else "MISSING ") + f"{what}: {detail}")

    print(f"fm-ft8-skimmer {VERSION}, Python {sys.version.split()[0]}, numpy {np.__version__}")
    print(f"config: {cfg.path}" + ("" if cfg.found else " (not there, using the defaults)"))
    line(sys.version_info >= (3, 9), "python", "3.9 or newer needed")
    rtl = shutil.which("rtl_sdr")
    line(bool(rtl), "rtl_sdr", rtl or "not found - install the rtl-sdr package")
    jt9 = find_jt9(cfg.jt9)
    line(bool(jt9), "jt9", jt9 or "not found - install WSJT-X, or set [decoder] jt9 to its path")
    if jt9:
        took, decoded = trial(jt9)
        if not decoded:
            line(False, "decoding", "the test signal was not decoded - run python3 selftest.py for details")
        else:
            line(took < 10, "speed", f"a test slot was decoded in {took:.1f} s"
                 + ("" if took < 5 else " - slow: a slot must be done well within its 15 s" if took < 10 else
                    " - too slow: this computer cannot keep up with the 15 s slots"))
    try:
        cfg.data.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=cfg.data):
            pass
        line(True, "data directory", str(cfg.data))
    except OSError as e:
        line(False, "data directory", f"{cfg.data}: {e}")
    print(f"listening on {cfg.freq / 1e6:.4f} MHz FM, RTL-SDR '{cfg.device}', gain {'auto' if cfg.gain == '0' else cfg.gain}")
    if cfg.pause_when:
        print("pauses while this succeeds: " + " ".join(cfg.pause_when))
    print("station: " + (f"{cfg.callsign or 'no callsign'}, {cfg.locator or 'no locator'}"))
    print("page: " + (f"http://{cfg.web_bind}:{cfg.web_port}/" if cfg.web else "off"))
    print("PSK Reporter: " + (f"on, as {cfg.callsign}" if cfg.psk else "off"))
    print("MQTT: " + (f"{cfg.mqtt_host}:{cfg.mqtt_port}, topic {cfg.mqtt_topic}/" if cfg.mqtt else "off"))
    for n in cfg.notes:
        print("note: " + n)
    return ok


def run(cfg):
    jt9 = find_jt9(cfg.jt9)
    if not jt9:
        raise ConfigError("jt9 not found: install WSJT-X, or set [decoder] jt9 to its path")
    if not shutil.which("rtl_sdr"):
        raise ConfigError("rtl_sdr not found: install the rtl-sdr package")
    for n in cfg.notes:
        log("note: " + n)
    page = broker = None
    if cfg.web:
        try:
            page = web.Page(cfg.web_bind, cfg.web_port, Path(__file__).resolve().parent / "web", log=log)
        except OSError as e:
            raise ConfigError(f"cannot open the page on {cfg.web_bind}:{cfg.web_port}: {e}") from None
        log(f"page: http://{cfg.web_bind}:{page.port}/")
    if cfg.mqtt:
        broker = mqttc.Mqtt(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_user, cfg.mqtt_pass, f"fm-ft8-skimmer-{os.getpid()}",
                            will=(f"{cfg.mqtt_topic}/status", {"state": "offline", "freq": cfg.freq, **cfg.public()}),
                            tls=cfg.mqtt_tls, log=log)
    psk = None
    if cfg.psk:
        psk = pskreporter.Reporter(cfg.callsign, cfg.locator, f"fm-ft8-skimmer {VERSION}", cfg.antenna, log=log)
    out = Outputs(page, broker, cfg.mqtt_topic)
    st = Station(cfg, out, jt9=jt9, psk=psk)
    slots = queue.Queue(maxsize=3)
    rx = Receiver(cfg, slots)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    log(f"fm-ft8-skimmer {VERSION}: {cfg.freq / 1e6:.4f} MHz FM, RTL-SDR '{cfg.device}', gain {'auto' if cfg.gain == '0' else cfg.gain}")
    st.announce()
    check_at, fails, fed = 0.0, 0, time.time()
    try:
        while not stop.is_set():
            now = time.time()
            if psk is not None:
                psk.tick()
            if now >= check_at:
                check_at = now + 10
                if must_pause(cfg.pause_when):
                    if rx.running() or rx.proc is not None:
                        rx.stop()
                    st.set_state("paused", "receiver in use by another program")
                elif not rx.running():
                    if rx.proc is not None:          # it stopped: stick unplugged, taken by another program, USB error
                        early = now - rx.started < 30
                        rx.stop()
                        fails += 1
                        st.set_state("no-sdr" if early else "paused",
                                     "receiver not available, retrying" if early else "receiver stopped, retrying")
                        check_at = now + min(300, 20 * fails)
                        continue
                    rx.start()
                    fed = now
                    time.sleep(2)
                    if rx.running():
                        st.set_state("listening")
                elif now - fed > STALL_S:            # rtl_sdr is there but silent (USB hang)
                    log(f"no samples from the receiver for {now - fed:.0f} s, restarting it")
                    rx.stop()
                    rx.proc, fails = None, fails + 1
                    st.set_state("paused", "receiver delivered no samples, restarting")
                    check_at = now + min(300, 20 * fails)
            try:
                t, raw = slots.get(timeout=1)
            except queue.Empty:
                if st.state != "listening" and now - st.stats_at > 300:
                    st.status()                      # keeps "ts" fresh so the page can tell a dead skimmer
                    st.stats_at = now
                continue
            fails, fed = 0, time.time()
            t0 = time.time()
            try:
                st.slot(t, raw)
            except Exception:                        # one slot that cannot be processed must not end the service
                log("slot failed:\n" + traceback.format_exc())
            if time.time() - t0 > 10:
                log(f"slot took {time.time() - t0:.1f} s to decode: this computer is too slow to keep up")
    finally:
        if psk is not None:
            psk.tick(force=True)
        rx.stop()
        st.set_state("offline", "skimmer stopped")
        st.close()
        time.sleep(0.5)
        out.close()
        log("stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Receive-only skimmer for FM-FT8 (standard FT8 via FM radios).")
    ap.add_argument("--config", metavar="FILE", help=f"settings file (default: {DEFAULT_CONFIG})")
    ap.add_argument("--check", action="store_true", help="check tools and settings, then exit")
    ap.add_argument("--version", action="version", version=f"fm-ft8-skimmer {VERSION}")
    args = ap.parse_args(argv)
    try:
        cfg = Config(args.config, must_exist=bool(args.config))
        if args.check:
            return 0 if check(cfg) else 1
        run(cfg)
    except ConfigError as e:
        print(f"fm-ft8-skimmer: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
