# AGENTS.md

## Status: active development

Authoritative design: `.mimocode/plans/holotek-sdd-v4.md` (SDD v4).
**Do NOT trust `…7673-holotek-sdd.md` (v3)** or `…722-playful-mountain.md` (v1):
v3's core loop had four bugs (unsatisfiable `co2meter>=1.0.0` pin, pandas-
conditional `read_data()` return type, no `None` guard, first-sample alert),
and v1 guessed a TEMPer-style init command. v4 fixes all of these against the
verified `co2meter` source.

## What holotek is

Python CLI daemon (`holotek.py`) that reads CO2 ppm from a USB zyTemp HID device
on macOS and fires `osascript` notifications on zone transitions. Single-file
core; `config.json` is hot-reloaded every poll. Menu-bar mode (`menubar.py`,
`--menubar`) is built on raw AppKit/PyObjC and shares `load_config`/`zone`/
`decide`/`read_ppm` with the CLI path.

## Non-obvious facts an agent will get wrong

- **Device rev 2.00 = NO encryption, but it DOES need stream activation.**
  Two hardware revisions: old (serial 1.40, bcdDevice 0x0100) sends
  XOR-encrypted packets; new (serial 2.00, bcdDevice 0x0200 — this one)
  sends **plaintext**. `dmage/co2mon` auto-detects: `release_number >
  0x0100 → decode_data = 0`. Our `read_sensors()` in `core.py` reads raw
  HID directly and validates plaintext (opcode 0x50, checksum, end marker
  0x0D). The magic feature-report (`send_feature_report([0x00]+[0]*8)`)
  is REQUIRED to start streaming on rev 2.00 — without it no packets
  arrive after a fresh `open_path`. AGENTS.md previously claimed feature
  reports "disrupt streaming" — that was wrong: they ACTIVATE it, with
  ~2 s latency before the first packet. We no longer use `co2meter` at
  all; `_DeviceHandle` opens `hid.device()` directly and sends the
  feature-report exactly once in `_ensure_open`.
- **The HID handle must stay OPEN between polls.** Closing it lets macOS
  stop streaming, and re-opening + re-activating costs ~2 s on the next
  read. `_DeviceHandle` caches the handle in `self._h`; `read_sensors`
  drains via `mon.drain()` without touching `hid.device()` again. With a
  persistent handle, each drain takes ~100–200 ms.
- **Drain the WHOLE buffer every poll.** The macOS HID driver buffers
  every packet the device streams (~1 pkt/s). At `poll_interval_seconds=
  120` that's ~120 queued packets. Reading FIFO returns the STALEST
  packet, not the current one — that was the original "app shows a
  different CO2 than the device screen" bug. `_drain_hid` reads until
  `h.read()` returns `[]`, with every valid packet overwriting the
  previous; the function returns the FRESHEST reading.
- **macOS raw HID ≠ "Input Monitoring".** Input Monitoring is for keystroke
  capture and is irrelevant here. First run may need `sudo`; otherwise rely on
  the brew `libusb`/`hidapi` user-space libs.
- **`hid` vs `hidapi` PyPI trap.** If import throws a `windll` AttributeError,
  `pip uninstall hid` (wrong package), then `pip install hidapi`.
- **PyObjC delivers selectors only to NSObject targets.** In `menubar.py` the
  `NSTimer` tick and the Quit `NSMenuItem` must target the `_AppDelegate`
  (an `NSObject`), NOT the plain-Python `HolotekApp`. Targeting a non-Cocoa
  object silently no-ops — the timer never fires (menu stuck on "starting…")
  and Quit does nothing. The delegate forwards to `HolotekApp`.
- **Build the menu bar item AFTER launch.** Create the `NSStatusItem` in
  `applicationDidFinishLaunching_`, not before `NSApplication.run()` — on recent
  macOS an item created pre-launch may never appear. Use
  `NSApplicationActivationPolicyAccessory` (no Dock icon).
- **Don't use a black glyph for the green/idle state.** A black circle is
  invisible on the dark menu bar; markers are colored emoji (🟢🟡🔴).

## Prerequisites (macOS — run before anything works)

```bash
brew install libusb hidapi
pip install -r requirements.txt
# Smoke test (must print a non-empty packet list):
python3 -c "import hid; h=hid.device(); h.open_path(hid.enumerate(0x04d9,0xa052)[0]['path']); h.send_feature_report([0x00]+[0]*8); print(h.read(8, timeout_ms=3000))"
```

## Running / verifying manually (agent notes)

```bash
./start.sh                       # nohup .venv/bin/python3 holotek.py --menubar, logs to /tmp/holotek_app.log
tail -f /tmp/holotek_app.log     # watch it live
kill $(cat /tmp/holotek-$(id -u).lock)   # stop it (no dedicated stop script)
```

- **Use `.venv/bin/python`, not system `python3`.** The system interpreter
  doesn't have `hid`/`pyobjc` installed — both `pytest` and any
  ad-hoc script must run through `.venv/bin/python`, otherwise you get
  `ModuleNotFoundError: No module named 'hid'` that looks like a real failure
  but is just the wrong interpreter.
- **No hardware attached ≠ broken.** Without a device, the log will show
  `WARNING ... device gone; reconnecting` on a loop — that's expected, not a
  crash. If a real zyTemp monitor is plugged in it reconnects automatically
  and you'll see `INFO ... CO2=<n> ppm zone=<z> trend=<t> notify=<bool>` lines
  each poll (`poll_interval_seconds` apart, 120s by default) — that's the
  fastest way to confirm a change works end-to-end.
- **Don't screenshot the whole desktop to check the menu bar icon.** A
  full-screen `screencapture` grabs whatever else is on screen (other apps,
  logged-in sessions, etc.), not just the status item. Also, `System Events`
  menu-bar introspection via `osascript` needs Accessibility permission
  granted to the terminal/agent first (`osascript is not allowed assistive
  access (-1719)` otherwise) — don't try to grant that yourself. Verify via
  the log + `ps -p <pid>` instead; only screenshot a tightly cropped region
  (and only after checking with the user) if you actually need to see the
  glyph render.

## Conventions locked by the design (don't "improve" these)

- Zone boundaries are not up for debate:
  green `ppm ≤ green_max`; yellow `green_max < ppm ≤ yellow_max`; red `> yellow_max`.
  `red_min` was dropped — it duplicated `yellow_max` and collided at the edge.
- `send_notification(title, body)` — fixed 2-arg signature; notifications via
  `osascript -e 'display notification …'`.
- Green transitions DO notify ("back to normal"); cooldown applies only to
  yellow/red repeats.
- Config keys match `config.json` exactly (`poll_interval_seconds`,
  `notification_cooldown_seconds`, etc.); bad JSON must not crash the daemon.
- Menu-bar path does NOT reuse the CLI `while True` loop — `NSApplication`
  owns the main run loop; polling runs on a daemon thread, the `NSTimer`
  pushes readings to the UI. Both entry points share `load_config`/`zone`/
  `decide`/`read_ppm`.

## zyTemp HID protocol (rev 2.00)

Device streams 8-byte plaintext packets on interrupt endpoint. Each packet:

```
[opcode] [value_hi] [value_lo] [checksum] [0x0D] [0x00] [0x00] [0x00]
checksum = (opcode + value_hi + value_lo) & 0xFF
```

Opcodes (all validated in practice on our unit, serial 2.00):

| Opcode | Name | Decode | Behavior |
|--------|------|--------|----------|
| `0x50` | **CO2** | `(hi<<8)\|lo` ppm | Direct CO₂ concentration |
| `0x42` | **Temp** | `val*0.0625-273.15` °C | Ambient temperature |
| `0x41` | Humidity | `val/100` % | Always 0 (unpopulated sensor) |
| `0x71` | Raw CO2? | raw | 95-97% of CO₂ value — maybe unfiltered/uncorrected |
| `0x43` | Raw temp? | unknown | Tracks `0x42` (ratio ~0.77) — maybe NTC ADC |
| `0x4F` | Unknown | — | Changes with environment |
| `0x6D` | Unknown | — | Rock-stable at 2794 — calibration constant |
| `0x6E` | Unknown | — | Changed when CO₂ dropped — ABC-related? |
| `0x52` | Unknown | — | Near-constant (~10310) — factory cal? |
| `0x56` | Unknown | — | Drifts slightly |
| `0x57` | Unknown | — | Drifts slightly |

Protocol confirmed by Henryk Plötz (Hackaday 2015), dmage/co2mon C library,
nikvoronin/Co2.Monitor (C#). Upstream RE sources:
- https://github.com/dmage/co2mon (C, libco2mon)
- https://github.com/nikvoronin/Co2.Monitor (C#)
- https://hackaday.io/project/5301-reverse-engineering-a-low-cost-usb-co-monitor

## Source of truth for the device API

Historical reference: upstream `vfilimonov/co2meter` — https://github.com/vfilimonov/co2meter.
We **no longer depend on `co2meter`** — `_DeviceHandle` in `core.py` opens
`hid.device()` directly. The library was useful for confirming the protocol
(opcode 0x50 = CO2, 0x42 = temp, magic feature-report activates the stream),
but its `read_data_raw()` re-opens and re-sends the feature-report on every
call, which is wasteful and was the source of long-standing confusion about
whether feature-reports "disrupt" streaming (they don't — they activate it).

