/* The part of an MQTT client this page uses, in a few KB: mqtt.connect(url, {clientId, reconnectPeriod})
   returns a client with on("connect" | "reconnect" | "offline" | "message") and subscribe(topic or [topics]).
   MQTT 3.1.1 over WebSocket, subscribe-only, QoS 0, clean session. "message" handlers get
   (topic, payload); the payload is a Uint8Array whose toString() is the UTF-8 text, as in mqtt.js. */
"use strict";
window.mqtt = {
  connect(url, opts = {}) {
    const te = new TextEncoder(), td = new TextDecoder();
    const str = s => { const b = te.encode(s); return [b.length >> 8, b.length & 255, ...b]; };
    const varint = n => { const o = []; do { let d = n % 128; n = Math.floor(n / 128); if (n) d |= 128; o.push(d); } while (n); return o; };
    const pkt = (h, body) => new Uint8Array([h, ...varint(body.length), ...body]);
    const handlers = {}, wanted = new Set(), period = opts.reconnectPeriod || 5000;
    const emit = (ev, ...a) => (handlers[ev] || []).forEach(f => { try { f(...a); } catch (e) { console.error(e); } });
    let ws, buf = new Uint8Array(0), ping = 0, up = false, pid = 1;
    const sendSub = t => ws.send(pkt(0x82, [pid >> 8, pid++ & 255, ...str(t), 0]));
    const open = () => {
      ws = new WebSocket(url, "mqtt"); ws.binaryType = "arraybuffer"; buf = new Uint8Array(0);
      ws.onopen = () => ws.send(pkt(0x10, [...str("MQTT"), 4, 2, 0, 60, ...str(opts.clientId || "web-" + Math.random().toString(16).slice(2, 10))]));
      ws.onmessage = e => { const d = new Uint8Array(e.data), b = new Uint8Array(buf.length + d.length); b.set(buf); b.set(d, buf.length); buf = b; parse(); };
      ws.onclose = () => {
        clearInterval(ping); up = false; emit("offline");
        setTimeout(() => { emit("reconnect"); open(); }, period);
      };
      ws.onerror = () => ws.close();
    };
    const parse = () => {
      for (;;) {
        let n = 0, m = 1, i = 1, b;
        do { if (i >= buf.length) return; b = buf[i++]; n += (b & 127) * m; m *= 128; } while (b & 128);
        if (buf.length < i + n) return;
        const type = buf[0] >> 4, qos = (buf[0] >> 1) & 3, body = buf.slice(i, i + n);
        buf = buf.slice(i + n);
        if (type === 2) {  // CONNACK
          if (body[1] !== 0) { ws.close(); return; }
          up = true;
          ping = setInterval(() => { if (ws.readyState === 1) ws.send(new Uint8Array([0xc0, 0])); }, 30000);
          wanted.forEach(sendSub);
          emit("connect");
        } else if (type === 3) {  // PUBLISH
          const tl = (body[0] << 8) | body[1], payload = body.slice(2 + tl + (qos ? 2 : 0));
          payload.toString = () => td.decode(payload);
          emit("message", td.decode(body.subarray(2, 2 + tl)), payload);
        }
      }
    };
    const client = {
      on(ev, f) { (handlers[ev] = handlers[ev] || []).push(f); return client; },
      subscribe(t) { for (const x of [].concat(t)) if (!wanted.has(x)) { wanted.add(x); if (up) sendSub(x); } return client; },
    };
    open();
    return client;
  },
};
