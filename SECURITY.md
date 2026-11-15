# Security

## Reporting a problem

Please report security problems through GitHub's private vulnerability reporting ("Report a
vulnerability" under the repository's Security tab), not in a public issue. Say what you
found, how to reproduce it, and which version (`python3 skimmer.py --version`).

Only the latest version is maintained.

## What the skimmer is exposed to

- **The radio.** Anyone in range can transmit anything on the frequency. Decoded text is
  filtered to plain ASCII, limited in length, parsed against the FT8 message patterns, and
  shown on the page as text only.
- **The page server.** Read-only, GET and HEAD, a fixed table of paths, no login, bound to
  127.0.0.1 unless configured otherwise. It is not hardened for direct exposure to the
  internet: use a reverse proxy.
- **The broker**, if MQTT is enabled. The skimmer only publishes. A static page reading from a
  broker trusts what the broker delivers, so the topic must not be writable by others.
- **PSK Reporter**, if enabled. Callsigns heard are sent under your callsign. A transmitted
  callsign cannot be verified.

## What it does not do

It does not transmit, does not run as root, does not execute commands through a shell, does
not accept input from page visitors, and does not log them.
