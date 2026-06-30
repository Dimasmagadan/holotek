# holotek — CO₂ Monitor Notification Daemon

Python CLI daemon that reads CO₂ ppm from a USB zyTemp (Holtek) HID device and fires macOS notifications on zone transitions.

## Prerequisites (macOS)

```bash
brew install libusb hidapi
pip install -r requirements.txt
```

Test the device:

```bash
python3 -c "import co2meter; m=co2meter.CO2monitor(); print(m.read_data_raw())"
# expected: (datetime, co2_int, temp_float)
```

If you get a permission error, the macOS HID driver owns the device.
Run the same command with `sudo` once, then unplug/replug the device.
After that, user-space access works without `sudo`.

If it hangs, set `"bypass_decrypt": true` in `config.json`.

## Usage

```bash
python3 holotek.py [--config path/to/config.json]
python3 holotek.py --menubar [--config path/to/config.json]
```

- **headless daemon** (`holotek.py`): runs in terminal, fires `osascript` notifications, Ctrl+C to exit.
- **menu-bar app** (`holotek.py --menubar`): background rumps app, shows CO₂ zone marker (`⚪`/`🟡`/`🔴`) in menu bar, fires native macOS notifications with sound. Quit from the dropdown menu.
- Single-instance enforced by lockfile (both modes).

## Configuration

| Key | Default | Range | Meaning |
|---|---|---|---|
| `thresholds.green_max` | 800 | integer ≥ 0 | upper bound for green zone (ppm) |
| `thresholds.yellow_max` | 1200 | integer ≥ green_max | upper bound for yellow zone (ppm) |
| `poll_interval_seconds` | 120 | > 0 | seconds between sensor reads |
| `notification_cooldown_seconds` | 1800 | ≥ 0 | min seconds between repeat alert notifications |
| `green_reentry_drop_ppm` | 200 | ≥ 0 | ppm drop that triggers immediate "back to normal" inside cooldown |
| `bypass_decrypt` | false | boolean | skip XOR decryption for unencrypted device variants |

## Zone semantics

- **green**:  `ppm ≤ green_max`
- **yellow**: `green_max < ppm ≤ yellow_max`
- **red**:    `ppm > yellow_max`

## Notification policy

- First sample after start produces no notification.
- Escalation (green→yellow, green→red, yellow→red) fires immediately.
- Same-zone repeat yellow/red suppressed within cooldown, refires after.
- Improving within alert (red→yellow) is rate-limited and preserves the baseline reading.
- Recovery to green with a drop ≥ `green_reentry_drop_ppm` bypasses cooldown.
- Recovery to green with a small drop respects cooldown.

## Project structure

```
holotek/
├── holotek.py       # daemon loop (argparse, signal, lockfile, poll)
├── menubar.py       # rumps menu-bar app (HolotekApp)
├── core.py          # shared logic (decide, zone, config, notification)
├── config.json      # thresholds and timing (hot-reloaded)
├── test_core.py     # unit tests
└── requirements.txt # dependencies
```

## Tests

```bash
pip install pytest
python3 -m pytest test_core.py -v
```
