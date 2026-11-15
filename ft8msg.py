"""FT8 messages as a skimmer sees them: parsing, grouping into QSOs, hourly statistics.

Pure data handling, no radio and no network - selftest.py exercises it.

An FT8 message between two stations reads "<to> <from> <payload>"; the payload walks through
grid -> report -> R+report -> RR73/RRR -> 73. A skimmer may hear only one side, so a QSO is followed
from whatever arrives: its stage is the furthest step seen, `heard` lists the stations actually decoded.
"""
import re
import time

CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z](?:/[A-Z0-9]{1,4})?$")
GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}$")
REPORT_RE = re.compile(r"^(R?)([+-][0-9]{2})$")
STAGES = ("call", "report", "r-report", "confirmed")   # confirmed = RR73 / RRR / 73 seen
QSO_GAP_S = 600      # silence after which the same pair starts a new QSO
KEEP_HOURS = 32 * 24


def _call(tok):
    """Callsign of a token, also from <CALL> (hashed, resolved by the decoder); None if it is none."""
    tok = tok.strip("<>")
    if tok and tok != "..." and len(tok) >= 3 and CALL_RE.match(tok):
        return tok
    return None


def parse(text):
    """-> dict(kind, de, to, grid, report) or None when the text is not a standard call exchange.

    kind: cq | call | grid | report | r-report | rr73 | 73
    """
    tok = text.upper().split()
    if len(tok) < 2:
        return None
    if tok[0] in ("CQ", "QRZ"):
        rest = tok[1:]
        grid = rest[-1] if len(rest) >= 2 and GRID_RE.match(rest[-1]) and rest[-1] != "RR73" else None
        calls = [c for c in map(_call, rest[:-1] if grid else rest) if c]
        if not calls:
            return None
        return {"kind": "cq", "de": calls[-1], "to": None, "grid": grid, "report": None}
    to, de = _call(tok[0]), _call(tok[1])
    if not de or (not to and tok[0].strip("<>") != "..."):
        return None
    m = {"kind": "call", "de": de, "to": to, "grid": None, "report": None}
    if len(tok) == 2:
        return m
    if len(tok) > 3:
        return None
    x = tok[2]
    r = REPORT_RE.match(x)
    if x in ("RR73", "RRR"):
        m["kind"] = "rr73"
    elif x == "73":
        m["kind"] = "73"
    elif r:
        m["kind"] = "r-report" if r.group(1) else "report"
        m["report"] = r.group(2)
    elif GRID_RE.match(x):
        m["kind"] = "grid"
        m["grid"] = x
    else:
        return None
    return m


STAGE_OF = {"call": 0, "grid": 0, "report": 1, "r-report": 2, "rr73": 3, "73": 3}


class Book:
    """Decodes, QSOs and hourly buckets; `state` is plain JSON (kept on disk by the caller)."""

    def __init__(self, state=None):
        """state: what state() returned earlier. Raises ValueError if it does not have that shape."""
        s = {} if state is None else state
        if not isinstance(s, dict):
            raise ValueError("state is not an object")

        def rows(key):
            v = s.get(key, [])
            if not isinstance(v, list) or not all(isinstance(r, dict) for r in v):
                raise ValueError(f"'{key}' is not a list of records")
            return v

        self.decodes = rows("decodes")      # newest last
        self.qsos = rows("qsos")            # newest last
        self.carriers = rows("carriers")    # slots with a signal on the channel, newest last
        # hour epoch -> [slots listened, with any signal, messages, qsos, slots with FT8, slots with another signal]
        hours = s.get("hours", {})
        if not isinstance(hours, dict) or not all(
                isinstance(v, list) and len(v) == 6 and all(type(n) is int for n in v) for v in hours.values()):
            raise ValueError("'hours' is not a table of six counters per hour")
        self.hours = {int(k): v for k, v in hours.items()}

    def state(self):
        return {"decodes": self.decodes, "qsos": self.qsos, "carriers": self.carriers,
                "hours": {str(k): v for k, v in self.hours.items()}}

    def _hour(self, t):
        h = int(t) // 3600 * 3600
        if h not in self.hours:
            self.hours[h] = [0, 0, 0, 0, 0, 0]
            for old in [k for k in self.hours if k < h - KEEP_HOURS * 3600]:
                del self.hours[old]
        return self.hours[h]

    def slot(self, t, signal=None, n=0):
        """One 15 s slot was listened to. signal = dict(peak_db, dur_s, kind, label, ...) when something
        stood on the channel, n = FT8 messages decoded in it. Call it after the slot's add()s."""
        b = self._hour(t)
        b[0] += 1
        if signal or n:
            b[1] += 1
            b[4 if n else 5] += 1
            self.carriers.append({"t": int(t), "n": n, **(signal or {"kind": "ft8", "label": "FT8"})})
            del self.carriers[:-60]

    def add(self, d):
        """Take one decode (dict with t, snr, dt, f, msg); returns the stored record or None if unusable."""
        m = parse(d["msg"])
        if m is None:
            return None
        rec = {**d, **{k: v for k, v in m.items() if v is not None}}
        self.decodes.append(rec)
        del self.decodes[:-150]
        self._hour(d["t"])[2] += 1
        if m["kind"] != "cq" and m["to"]:
            rec["qso"] = self._qso(rec, m)
        return rec

    def _qso(self, rec, m):
        t, pair = rec["t"], sorted((m["de"], m["to"]))
        q = next((q for q in reversed(self.qsos) if sorted((q["a"], q["b"])) == pair), None)
        fresh = q is None or t - q["t1"] > QSO_GAP_S or (
            q["stage"] == "confirmed" and STAGE_OF[m["kind"]] <= 1 and t - q["t1"] > 45)
        if fresh:
            q = {"id": f"{int(t)}-{m['de']}-{m['to']}", "a": m["de"], "b": m["to"], "t0": t, "t1": t, "stage": "call",
                 "n": 0, "grid": {}, "rpt": {}, "heard": [], "snr": {}}
            self.qsos.append(q)
            del self.qsos[:-200]
            self._hour(t)[3] += 1
        q["t1"] = t
        q["n"] += 1
        if m["de"] not in q["heard"]:
            q["heard"].append(m["de"])
        q["snr"][m["de"]] = max(rec["snr"], q["snr"].get(m["de"], -99))
        if m["grid"]:
            q["grid"][m["de"]] = m["grid"]
        if m["report"]:
            q["rpt"][m["de"]] = m["report"]
        stage = STAGES[STAGE_OF[m["kind"]]]
        if STAGES.index(stage) > STAGES.index(q["stage"]):
            q["stage"] = stage
        return q["id"]

    # ---- what goes to the page ----
    def recent(self):
        return {"ts": int(time.time()), "decodes": self.decodes[-100:][::-1], "qsos": self.qsos[-50:][::-1],
                "carriers": self.carriers[-40:][::-1]}

    def stats(self):
        return {"ts": int(time.time()), "cols": ["t", "slots", "signal", "decodes", "qsos", "ft8", "other"],
                "hours": [[h, *self.hours[h]] for h in sorted(self.hours)]}
