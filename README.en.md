# AULA Hub

[![License: MIT](https://img.shields.io/badge/license-MIT-7c4dff.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011%20x64-0ea5e9.svg)](#install)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab.svg)](requirements.txt)
[![Self-check](https://img.shields.io/badge/self--check-25%2F25%20PASS-22c55e.svg)](docs/SELF-CHECK.md)

**A modern, open-source control hub for the AULA F99 Pro keyboard** — replacing the
official driver, which still looks like it was designed for Windows 7 and cannot even
show the battery level.

> **Download**: [AULA-Hub-portable.zip](https://github.com/xx12ek/aula-f99-pro-hub/releases/latest) —
> portable, no installer, no Python, no admin rights.
>
> [中文说明 / Chinese README](README.md)

## Screenshots

| Lighting | Remapping | Settings | Misc |
|---|---|---|---|
| ![lighting](docs/screenshots/lighting.png) | ![remap](docs/screenshots/remap.png) | ![settings](docs/screenshots/settings.png) | ![misc](docs/screenshots/misc.png) |

## What it does

### Lighting
- **20 effects**, matching the firmware's built-in list one-to-one
- Brightness 0–9 / speed 0–4, continuous drag with easing snap-back
- **Per-key RGB**: 126 individually addressable LEDs, built-in HSV colour wheel
- **10 music visualiser modes** (WASAPI loopback + FFT, no extra dependency)

### Remapping and macros
- **4 layers**: base / FN1 / FN2 / Tap
- **8 function categories**: keyboard, mouse, media, commands, combos, modifiers,
  keyboard built-ins, software actions
- Every key shows its real function name, not a placeholder
- Macros are recorded into the keyboard's own 10 KB macro bank

### Knob
- **15 bindable actions** per direction
- Long-press cycles through **user-defined slots** (add / edit / delete)
- Volume passthrough: the real HID key is released so Windows draws its native
  volume OSD; other actions use our own OSD, calibrated pixel-by-pixel against
  the real Win11 one
- Firmware-mode guard: if the keyboard's own long-press drops it into firmware mode,
  the guard pulls it back automatically

### Everything else
- **Battery level** (the official driver sets `ShowPower=0` in all three profiles,
  hiding a feature the hardware actually has)
- **2.4 GHz wireless**: read all state, write lighting / brightness / speed / sleep
- Config backup and restore, JSON import and export, tray, autostart

## Install

**Option 1 — download (recommended)**

Grab `AULA-Hub-portable.zip` from
[Releases](https://github.com/xx12ek/aula-f99-pro-hub/releases/latest), unzip anywhere,
run `AULA-Hub.exe`. Windows 10/11 x64. Nothing else required.

**Option 2 — from source**

```powershell
git clone https://github.com/xx12ek/aula-f99-pro-hub.git
cd aula-f99-pro-hub
pip install -r requirements.txt
python app.py
```

Requires Python 3.10+. The UI runs in WebView2 (already present on Windows 10/11 with
Edge).

## Supported hardware

| Device | VID:PID | Status |
|---|---|---|
| AULA F99 Pro (wired) | `258A:010C` | Fully supported |
| AULA F99 Pro (2.4 GHz receiver) | `3554:FA09` | Read all, write lighting only |

Check your keyboard with:

```powershell
Get-PnpDevice -Class HIDClass | Where-Object { $_.InstanceId -match "258A|3554" } |
  Select-Object FriendlyName, InstanceId
```

Other AULA models may work partially — run `tools/selfcheck.py` and open an issue with
the output.

## Known limitations

These are firmware limits, not missing features. Each was verified empirically.

| Cannot | Why |
|---|---|
| Write remaps / per-key colours / macros over 2.4 GHz | The wireless channel only exposes read commands (24 + 18 command combinations tried; none worked) |
| Trigger the Windows native brightness OSD from software | Only real hardware keys do that (7 different APIs tested) |
| Flash firmware | The official `V3` tool is a burner with no `.hex` inside |
| Disable the knob's long-press firmware mode | Firmware behaviour; user space cannot turn it off, so we detect and undo it |

## How it was reverse-engineered

The official driver (`OemDrv.exe`) is a 32-bit MFC binary. Everything here was obtained
from it plus live device experiments:

- Extracted the HID report layouts with `pefile` + `capstone` and by walking the
  descriptor parser
- Dumped its UI string table to learn the original feature names and the mapping
  between UI labels and matrix record codes
- Probed the device directly over HID to confirm each byte offset, which is how the
  `ShowPower=0` cover-up and the `0x1A` firmware-mode flag were found

Full notes in [`docs/PROTOCOL.md`](docs/PROTOCOL.md), with every section marked
✅ verified / ⚠️ inferred / ❓ unknown. [`docs/SELF-CHECK.md`](docs/SELF-CHECK.md) has
the 25-point on-device self-check output.

## Safety

- No kernel driver, no system modification, no firmware writes
- Fully offline: the app never makes a network request
- The only global hook is a low-level keyboard hook that intercepts just the three
  knob keys and F13–F20 (used for software-action keys)
- Every write is read back and verified; config can be exported before changing anything

## License

MIT.
