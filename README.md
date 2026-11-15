# fm-ft8-skimmer

A receive-only skimmer for **FM-FT8**: standard FT8 sent through ordinary FM radios.

It listens on one FM channel with an RTL-SDR, decodes every FT8 message it hears and shows the
result on a small web page: messages, QSOs, how busy the frequency is, and what else was on
it. Run one, and stations around you can check whether their FM-FT8 signal gets out.

FM-FT8 itself, with the proposed calling frequency 144.575 MHz and a guide for setting up a
station, is described at <https://github.com/DL1TJ/fm-ft8>.

```
antenna -> RTL-SDR -> skimmer.py -> web page on http://127.0.0.1:8073
                          |
                          +-> PSK Reporter (optional)
                          +-> MQTT broker  (optional)
```

- Receive only. It never transmits.
- Stands alone: no account, no server, no broker needed. Nothing leaves your computer unless
  you switch on PSK Reporter or MQTT.
- Few dependencies: Python with numpy, the `rtl_sdr` tool, and `jt9` from WSJT-X.

## What you need

- An RTL-SDR stick and an antenna for 2 m. A vertical is right: FM stations are vertically polarised.
- A computer that is always on, with Linux or macOS. A Raspberry Pi is enough; see
  [Raspberry Pi](#raspberry-pi) below.
- A correct clock. FT8 works in 15-second slots; the system clock must be synchronised (NTP),
  which it is by default on most systems.
- Software:
  - Python 3.9 or newer with **numpy**
  - **rtl-sdr** (provides `rtl_sdr`)
  - **WSJT-X** (provides `jt9`, the FT8 decoder; WSJT-X itself does not need to run)

## Quick start

On Debian, Ubuntu or Raspberry Pi OS. It takes about five minutes.

**1. Install the three things it needs**

```sh
sudo apt update
sudo apt install python3-numpy rtl-sdr wsjtx git
```

**2. Get the skimmer**

```sh
cd ~
git clone https://github.com/DL1TJ/fm-ft8-skimmer.git
cd fm-ft8-skimmer
```

There is nothing to build and nothing is installed into the system: it runs from this directory.

**3. Plug in the RTL-SDR and check**

```sh
python3 skimmer.py --check
```

Every line should start with `ok`. The `speed` line shows how long this computer needs for one
15-second slot. If something is `MISSING`, the line says what to install.

**4. Run it**

```sh
python3 skimmer.py
```

**5. Open the page**

On the same computer: <http://127.0.0.1:8073>

It shows "listening" and fills up as FT8 messages are heard on 144.575 MHz FM. Stop the skimmer
with Ctrl-C.

That is all that is needed. The next two steps are optional.

**6. Tell it who you are** (optional)

```sh
mkdir -p ~/.config/fm-ft8-skimmer
cp config.example.ini ~/.config/fm-ft8-skimmer/config.ini
nano ~/.config/fm-ft8-skimmer/config.ini
```

Set your callsign and locator under `[station]`. To see the page from other computers on your
network, set `bind = 0.0.0.0` under `[web]` and open `http://<address of this computer>:8073`.
Run `python3 skimmer.py --check` again to see how the settings were understood.

**7. Start it automatically** (optional)

```sh
mkdir -p ~/.config/systemd/user
cp systemd/fm-ft8-skimmer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fm-ft8-skimmer
loginctl enable-linger "$USER"
```

The skimmer now starts with the computer and restarts if it stops. Its log:
`journalctl --user -u fm-ft8-skimmer -f`. After changing the settings:
`systemctl --user restart fm-ft8-skimmer`.

**To update** later: `cd ~/fm-ft8-skimmer && git pull`, then restart it.

### macOS

`brew install numpy librtlsdr git`, install WSJT-X from its website, then continue at step 2.
Step 7 does not apply; run it in a terminal.

### If something does not work

Run `python3 selftest.py`. It tests the whole decoding path without a radio and says which part
fails. Then see [Troubleshooting](#troubleshooting).

## Raspberry Pi

The skimmer is written to run on a Raspberry Pi: it needs about 100 MB of memory and no
graphical desktop.

- **System:** Raspberry Pi OS (64-bit) based on Debian 12 "bookworm" or newer; the Lite image is
  fine. Follow the quick start as it is. To keep a headless system small, add
  `--no-install-recommends`: `sudo apt install --no-install-recommends python3-numpy rtl-sdr wsjtx git`.
  WSJT-X is installed only for its `jt9` program; it brings some graphics libraries along that
  are never used.
- **Model:** any with a 64-bit system (Pi 3, 4, 5, Zero 2 W). Whether yours is fast enough is
  not a guess: `python3 skimmer.py --check` decodes a test signal and prints how long a slot
  takes. It must stay well under the 15 seconds of a slot.
- **Clock:** a Pi has no battery clock. It needs a network connection to set its time (it does
  so by itself); without it nothing will decode.
- **Power:** an RTL-SDR draws up to about 300 mA. Use a good power supply; on a Zero, a powered
  USB hub.
- **Permissions:** the `rtl-sdr` package lets members of the `plugdev` group use the stick.
  The default user is in it; check with `groups`.
- **SD card:** temporary files are kept in memory (`/dev/shm`). On disk the skimmer writes a
  small state file every few minutes and one line per decoded message.

What has been verified: the self-test and the timing check pass on ARM64 with the package
versions of Debian 12 and 13 (Python 3.11 / numpy 1.24 / WSJT-X 2.6 and Python 3.13 /
numpy 2.2 / WSJT-X 2.7), limited to one CPU core and 512 MB. Reports from real Pi hardware,
and from 32-bit systems, are welcome.

## Settings

The settings file is `~/.config/fm-ft8-skimmer/config.ini` (step 6 of the quick start). Every
setting is optional and explained in `config.example.ini`. The ones most people change:

```ini
[station]
callsign = K1ABC
locator = FN42ab
antenna = vertical, 10 m above ground

[receiver]
frequency = 144.575
gain = auto
```

`python3 skimmer.py --check` prints how the settings were understood. A misspelt setting is
an error, not silently ignored.

## Reporting to PSK Reporter

With `[pskreporter] enabled = yes` and your callsign and locator set, every station heard is
reported to <https://pskreporter.info> as heard by you, like WSJT-X does. The skimmer follows
PSK Reporter's rules for automatic reporters: at most one datagram every five minutes, a
callsign at most once in five minutes. Only messages that decode with full confidence are
reported.

Leave it off while you are experimenting on a dummy load or with test callsigns.

## Publishing the page

The built-in page is meant for you and your network. To show it to the world, either

- put a reverse proxy (nginx, Caddy ...) with HTTPS in front of the built-in page, or
- publish through MQTT and host the page as static files:
  1. Enable `[mqtt]`. The skimmer publishes `status`, `recent`, `stats` (retained) and
     `decode` under the configured topic.
  2. Copy the `web/` directory to any web server.
  3. Next to it, create `data/config.json`:
     ```json
     {
       "mqtt": { "url": "wss://broker.example/mqtt", "root": "fmft8" },
       "links": [ { "text": "Legal notice", "href": "https://example.org/legal" } ]
     }
     ```
     The page then reads live from the broker over WebSocket. The broker must allow anonymous
     **read-only** access to that topic; never put credentials into the page. `links` is
     optional and adds footer links, for example a legal notice. If your web server sends a
     Content-Security-Policy, it must allow the broker in `connect-src`.

## What it publishes and stores

| Name | Content |
|---|---|
| `status` | `listening` / `paused` / `no-sdr` / `offline`, frequency, your station details, the last slot with its RF level |
| `recent` | the latest 100 messages, 50 QSOs and 40 slots with a signal |
| `stats` | per UTC hour for 32 days: slots listened, slots with any signal, messages, QSOs, slots with FT8, slots with another signal |
| `decode` | each decoded message as it arrives (MQTT only) |

On disk, in `~/.local/share/fm-ft8-skimmer` unless configured otherwise:

- `state.json` - the above, so the statistics survive a restart.
- `decodes.ndjson` - every decode, one JSON object per line. Rotated once at 50 MB.
- `undecoded/*.wav` - only if `keep_audio` is set: the audio of signals that did not decode.

Nothing about visitors of the page is logged.

## How it works

`rtl_sdr` delivers IQ samples at 240 kS/s, tuned 30 kHz above the channel to keep the
receiver's DC spike out of it. For each 15-second slot the skimmer filters the channel out,
demodulates FM, and hands 12 kHz audio to `jt9`. Working on IQ instead of ready-made FM audio
also gives the strength of the carrier, so a signal that did not decode is noted as well,
with a guess at what it was: FM voice, a bare carrier, a tone, packet, SSB, CW, or FT8 that
was sent in SSB instead of FM.

Things to know when reading the page:

- **SNR** is measured in the demodulated audio. It is not comparable with FT8 reports on SSB.
  **RF** is the strength of the FM carrier over the noise floor.
- **"What it was"** for other signals is a heuristic for a usage log, not a decoder.
- A QSO is followed even when only one of the two stations is heard.
- Callsigns are taken from what was received. Anyone can transmit any callsign; the skimmer
  cannot know who really sent a message.
- Hours in which the skimmer was not listening are shown as such, not as silence.

| File | |
|---|---|
| `skimmer.py` | settings, signal path, the receive loop |
| `ft8msg.py` | message parsing, QSO grouping, hourly statistics |
| `web.py`, `web/` | the built-in page |
| `pskreporter.py` | PSK Reporter datagrams |
| `mqttc.py` | a small MQTT publisher |
| `selftest.py` | tests for all of the above, without a radio |

## Troubleshooting

| Symptom | Things to check |
|---|---|
| `--check` says `rtl_sdr` or `jt9` is missing | Install rtl-sdr / WSJT-X, or set `[decoder] jt9` to the full path |
| State "paused" | `pause_when` is set and its command succeeds (another program has the stick), or the receiver stopped and is being restarted |
| State "receiver missing" | Is the stick plugged in? Is another program using it (SDR software, `rtl_tcp`, a DVB driver)? On Linux the kernel's DVB-T driver must not grab it: the rtl-sdr package normally blacklists `dvb_usb_rtl28xxu`; replug the stick or reboot after installing |
| Listening, but a known signal does not decode | The clock (check `timedatectl`); the frequency correction `ppm` of the stick; the signal may be too weak for FM |
| "Channel" always shows a signal, or never | Gain. Look at `noise_db` under `last` in <http://127.0.0.1:8073/data/status.json>: as a rule of thumb it should be above about -60; if it is far lower, the noise does not reach the stick's converter and `gain` should be raised |
| Log says "slot took ... s to decode" | The computer is too slow or busy; slots are dropped when decoding cannot keep up |
| Page says "offline" | The skimmer is not running, or the page was opened from a different address than it listens on |

The self-test is the first thing to run when something is odd: `python3 selftest.py`.

## Security

- The skimmer runs without root and never executes anything through a shell.
- The page server answers only GET and HEAD for a fixed list of files, has no login and listens
  on 127.0.0.1 by default. It is not meant to face the internet directly.
- Everything received over the air is treated as untrusted text: it is filtered to plain
  ASCII, and the page inserts it as text, never as HTML. A strict Content-Security-Policy is set.
- MQTT uses TLS with certificate verification by default. Keep the config file private if it
  holds a password (`chmod 600`); the skimmer warns if it is not.
- See [SECURITY.md](SECURITY.md) for how to report a problem.

## Licence

[MIT](LICENSE). The skimmer runs `jt9` from WSJT-X as a separate program; WSJT-X is licensed
under the GPL and is not included here. This project is not affiliated with the WSJT-X
developers or with PSK Reporter.
