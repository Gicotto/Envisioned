# Envisioned

**Unofficial Linux configuration tool for the SCUF Envision Pro controller.**

Configure button remaps, thumbstick presets, trigger sensitivity, vibration, LED brightness, eco/auto-shutoff — plus an evdev bridge that presents the controller as an Xbox Elite 2 for full Steam / Proton compatibility.

Ships as:
- **`envisioned`** — Tauri v2 + React GUI.
- **`scuf_config.py`** — standalone Python CLI (works without the GUI).
- **`scuf_virtual_pad_managed.py`** — evdev bridge, runs as a systemd user service.

> **Disclaimer.** Envisioned is a community project, not affiliated with, authorized by, or endorsed by Corsair Memory, Inc. or SCUF Gaming. "SCUF" and "Envision" are trademarks of their respective owners. Use at your own risk. See [Brick-risk warning](#brick-risk-warning).

---

## Requirements

- **OS**: Linux with a modern kernel (evdev + hidraw). Tested on Arch / CachyOS.
- **Hardware**: SCUF Envision Pro (USB VID:PID `2e95:434d`).

### System packages

```bash
# Arch / CachyOS
sudo pacman -S python python-evdev rust nodejs npm \
    webkit2gtk-4.1 base-devel curl wget openssl \
    appmenu-gtk-module gtk3 libayatana-appindicator librsvg

# Python deps
pip install evdev pyusb
```

`pyusb` is **mandatory** — see [Brick-risk warning](#brick-risk-warning).

---

## Install

### 1. Build

```bash
git clone https://github.com/Gicotto/Envisioned.git
cd Envisioned/gui
npm install
npm run tauri build
# binary at: src-tauri/target/release/envisioned
```

Or run in dev mode:

```bash
npm run tauri:dev
```

### 2. Install udev rules (one-time, requires sudo)

Grants non-root access to the controller's hidraw + raw USB interfaces and hides the physical device from SDL2 / Steam joystick enumeration (the virtual bridge takes its place).

```bash
sudo sh -c "
echo 'SUBSYSTEM==\"hidraw\", ATTRS{idVendor}==\"2e95\", ATTRS{idProduct}==\"434d\", MODE=\"0666\"' \
  > /etc/udev/rules.d/99-scuf.rules && \
echo 'SUBSYSTEM==\"usb\",    ATTRS{idVendor}==\"2e95\", ATTRS{idProduct}==\"434d\", MODE=\"0666\"' \
  >> /etc/udev/rules.d/99-scuf.rules && \
echo 'SUBSYSTEM==\"input\",  ATTRS{idVendor}==\"2e95\", ATTRS{idProduct}==\"434d\", MODE=\"0666\", ENV{ID_INPUT_JOYSTICK}=\"\"' \
  >> /etc/udev/rules.d/99-scuf.rules && \
udevadm control --reload-rules && udevadm trigger
"
```

Re-plug the controller after applying the rule.

Also available inside the GUI under **Settings → Fix Permissions**.

### 3. Install the virtual pad service

Open the app → **Settings → Install Service**. This writes a systemd user unit that runs `scuf_virtual_pad_managed.py` automatically on login.

---

## Brick-risk warning

**Read this before running any write command.**

Every firmware write (profile remap, thumbstick preset, vibration, LED, restore, bootstrap) is preceded by a mandatory two-stage handshake against the controller. Stage 1 uses `pyusb` to issue a raw USB interrupt-OUT sequence on the gamepad HID interface; stage 2 is a four-command unlock on the config HID interface.

**If the handshake fails or is skipped, writes will fail with `06 01 status=9`. Repeated failed writes progressively corrupt the controller's slot-management state. Symptoms escalate:**

1. First failure — individual write rejected, slot unchanged.
2. Repeated failures — `0d 01 status=3` errors appear; device begins refusing other operations.
3. Worst case — mapped buttons stop responding on the physical controller. The device behaves as if no profile is loaded, and Envisioned can no longer recover it.

**Recovery from a fully corrupted state requires the vendor's Windows configuration tool.** It has firmware-level access that resets the slot state. Neither USB power-cycling nor Envisioned's normal protocol can recover once the device reaches stage 3.

Before running any write command, verify:

- `pyusb` is installed (`pip install pyusb`).
- The `usb` subsystem line is present in `/etc/udev/rules.d/99-scuf.rules` (see [udev rules](#2-install-udev-rules-one-time-requires-sudo)).
- The controller has been re-plugged since applying the rule.

If `scuf_config.py` prints `Warning: pyusb not installed` or `Warning: write unlock failed`, **stop immediately** and fix the prerequisite. Do not retry.

---

## Running

### GUI

```bash
./gui/src-tauri/target/release/envisioned
```

Run the built binary directly — no install step required.

### CLI

`scuf_config.py` is fully usable standalone:

```bash
cd gui

# Show current button mappings
python3 scuf_config.py show

# Remap a button (P1–P4 = back paddles, S1–S2 = side buttons)
# Targets: A B X Y LB RB LT RT L3 R3 Up Down Left Right
python3 scuf_config.py remap S1 B
python3 scuf_config.py remap P1 RB

# Clear a mapping
python3 scuf_config.py unmap S1

# Thumbstick presets
python3 scuf_config.py show-presets

# LED brightness (0–100)
python3 scuf_config.py brightness 80

# Eco / auto-shutoff
python3 scuf_config.py eco-mode on
python3 scuf_config.py auto-shutoff on --minutes 15

# Vibration
python3 scuf_config.py show-vibration
python3 scuf_config.py set-vibration --left 70 --right 70
```

Use `--profile N` (1–3) on any command to target a specific profile. Defaults to profile 1.

### Virtual pad service

The bridge reads from the physical SCUF, grabs its evdev interfaces to hide them from SDL2 / Steam, and exposes a virtual Xbox Elite 2 controller.

```bash
systemctl --user start scuf-virtual-pad
systemctl --user stop scuf-virtual-pad
systemctl --user restart scuf-virtual-pad
systemctl --user status scuf-virtual-pad
systemctl --user enable scuf-virtual-pad         # auto-start on login
journalctl --user -u scuf-virtual-pad -f          # live logs
```

Live log also at `/tmp/scuf-virtual-pad.log`.

---

## Uninstall

### Service

```bash
systemctl --user stop scuf-virtual-pad
systemctl --user disable scuf-virtual-pad
rm -f ~/.config/systemd/user/scuf-virtual-pad.service
systemctl --user daemon-reload
```

Also available in the GUI under **Settings → Uninstall Service**.

### Udev rules

```bash
sudo rm -f /etc/udev/rules.d/99-scuf.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Re-plug the controller. The physical device becomes visible to Steam again as a standard gamepad.

### Build artifacts

```bash
rm -rf gui/node_modules gui/src-tauri/target
```

---

## Device info

| Property | Value |
|---|---|
| USB VID:PID | `2e95:434d` |
| Virtual identity | Xbox Elite 2 (`045e:02ea`) |
| hidraw | `/dev/hidrawX` (auto-detected) |
| GUI log | `/tmp/scuf-gui.log` |
| Bridge log | `/tmp/scuf-virtual-pad.log` |

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
