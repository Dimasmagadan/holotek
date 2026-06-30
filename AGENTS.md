# AGENTS.md

## Status: greenfield

No commits, no source yet. This repo is a planned-but-unbuilt Python CLI daemon.
Authoritative design: `.mimocode/plans/1782807237673-holotek-sdd.md` (SDD v3).
**Do NOT trust `…722-playful-mountain.md` (v1)** — its HID protocol section
guesses a TEMPer-style init command and is explicitly superseded by v3.

## What holotek is

Python CLI daemon (`holotek.py`) that reads CO2 ppm from a USB zyTemp HID device
on macOS and fires `osascript` notifications on zone transitions. Single-file
core; `config.json` is hot-reloaded every poll. Menu-bar mode is deferred.

## Non-obvious facts an agent will get wrong

- **Device**: Holtek/zyTemp, USB `04d9:a052`, streams 8-byte XOR-encrypted HID
  packets. Opcodes: `0x50`=CO2, `0x42`=temp, `0x41`=humidity.
- **`hidapi` does NOT decrypt.** Decryption lives in `co2meter`
  (`CO2monitor._decrypt`). Don't reimplement XOR unless vendoring the raw-hidapi
  fallback (only if `co2meter` breaks on ARM64).
- **macOS raw HID ≠ "Input Monitoring".** Input Monitoring is for keystroke
  capture and is irrelevant here. First run may need `sudo`; otherwise rely on
  the brew `libusb`/`hidapi` user-space libs.
- **`hid` vs `hidapi` PyPI trap.** If import throws a `windll` AttributeError,
  `pip uninstall hid` (wrong package), then `pip install hidapi`.
- **Unencrypted device variants exist.** If `read_data()` hangs, retry with
  `CO2monitor(bypass_decrypt=True)` and set `config.bypass_decrypt: true`.

## Prerequisites (macOS — run before anything works)

```bash
brew install libusb hidapi
pip install hidapi co2meter
sudo python3 -c "import co2meter; m=co2meter.CO2monitor(); print(m.read_data())"
# expected: (datetime, co2_int, temp_float)
```

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
- Menu-bar path, if ever built, must NOT reuse the `while True` loop — `rumps`
  owns the mainloop. Share `load_config`/`zone` between both entry points.

## Source of truth for the device API

Upstream `vfilimonov/co2meter` — https://github.com/vfilimonov/co2meter.
If prose docs and the library disagree, trust the library.
