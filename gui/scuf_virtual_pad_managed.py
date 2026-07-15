#!/usr/bin/env python3
"""
scuf_virtual_pad_managed.py - SCUF Envision Pro virtual pad (config-file driven)

Reads virtual_pad_config.json from the same directory.
Hot-reloads button remaps and deadzones automatically when the file changes.
Logs to /tmp/scuf-virtual-pad.log

Do NOT modify scuf_virtual_pad.py — that is the reference implementation.
"""

import os
import sys
import json
import struct
import selectors
import logging
from evdev import InputDevice, UInput, ecodes as e, AbsInfo

# ── Logging ───────────────────────────────────────────────────

LOG_PATH = "/tmp/scuf-virtual-pad.log"

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scuf-vpad")

# Also mirror WARNING+ to stderr so systemctl status shows errors
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setLevel(logging.WARNING)
_stderr.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
log.addHandler(_stderr)

# ── Constants ─────────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "virtual_pad_config.json")

DEFAULT_CONFIG = {
    "button_remap": {
        "BTN_SOUTH": "BTN_SOUTH",
        "BTN_EAST":  "BTN_EAST",
        "BTN_NORTH": "BTN_NORTH",
        "BTN_C":     "BTN_WEST",
        "BTN_WEST":  "BTN_TL",
        "BTN_Z":     "BTN_TR",
        "BTN_TR":    "BTN_START",
        "BTN_TL":    "BTN_SELECT",
        "BTN_TL2":   "BTN_THUMBL",
        "BTN_TR2":   "BTN_THUMBR",
    },
    "deadzones": {
        # Hardware thumbstick presets (set via scuf_config.py) control
        # deadzone and curve response. Keep software values at 0 so the
        # firmware settings are not overridden by a second filter layer.
        "left_deadzone":  0,
        "left_jitter":    0,
        "right_deadzone": 0,
        "right_jitter":   0,
    },
}

VID, PID = 0x2e95, 0x434d
L2_MAX = R2_MAX = 1023

VIRTUAL_BUTTONS = [
    e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST,
    e.BTN_TL, e.BTN_TR,
    e.BTN_SELECT, e.BTN_START, e.BTN_MODE,
    e.BTN_THUMBL, e.BTN_THUMBR,
    e.BTN_DPAD_UP, e.BTN_DPAD_DOWN, e.BTN_DPAD_LEFT, e.BTN_DPAD_RIGHT,
]


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        cfg = {
            "button_remap": {**DEFAULT_CONFIG["button_remap"], **data.get("button_remap", {})},
            "deadzones":    {**DEFAULT_CONFIG["deadzones"],    **data.get("deadzones",    {})},
        }
        log.info("Config loaded from %s", CONFIG_PATH)
        log.debug("button_remap: %s", cfg["button_remap"])
        log.debug("deadzones: %s", cfg["deadzones"])
        return cfg
    except Exception as ex:
        log.warning("Failed to load config (%s), using defaults", ex)
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}


def build_remap(cfg):
    remap = {}
    for src_name, dst_name in cfg["button_remap"].items():
        src = getattr(e, src_name, None)
        dst = getattr(e, dst_name, None)
        if src is not None and dst is not None:
            remap[src] = dst
        else:
            log.warning("Unknown button code: %s -> %s (skipped)", src_name, dst_name)
    return remap


def make_uinput():
    cap = {
        e.EV_KEY: VIRTUAL_BUTTONS,
        e.EV_ABS: [
            # flat=0, fuzz=0: hardware preset controls deadzone/response.
            # Declaring non-zero flat here would add a third deadzone layer
            # on top of the firmware curve and any software filter.
            (e.ABS_X,     AbsInfo(0, -32768, 32767, 0, 0, 0)),
            (e.ABS_Y,     AbsInfo(0, -32768, 32767, 0, 0, 0)),
            (e.ABS_Z,     AbsInfo(0, 0, 1023, 0, 0, 0)),
            (e.ABS_RX,    AbsInfo(0, -32768, 32767, 0, 0, 0)),
            (e.ABS_RY,    AbsInfo(0, -32768, 32767, 0, 0, 0)),
            (e.ABS_RZ,    AbsInfo(0, 0, 1023, 0, 0, 0)),
            (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
            (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
        ],
        e.EV_FF: [e.FF_RUMBLE],
    }
    ui = UInput(cap, name="Virtual SCUF Envision Pro",
                vendor=0x045e, product=0x02ea, version=0x0301,
                bustype=0x0003)
    for axis in [e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY,
                 e.ABS_Z, e.ABS_RZ, e.ABS_HAT0X, e.ABS_HAT0Y]:
        ui.write(e.EV_ABS, axis, 0)
    ui.syn()
    log.info("UInput device created: 'Virtual SCUF Envision Pro' (vendor=045e product=02ea)")
    return ui


def find_scuf_devices():
    import evdev as ev
    import glob as gl
    evdev_path = None
    extra_evdev_paths = []
    hidraw_path = None
    for path in ev.list_devices():
        try:
            dev = ev.InputDevice(path)
            if dev.info.vendor == VID and dev.info.product == PID:
                caps = dev.capabilities()
                keys = caps.get(e.EV_KEY, [])
                if e.BTN_SOUTH in keys:
                    evdev_path = path
                    log.info("Found SCUF evdev gamepad node: %s (%s)", path, dev.name)
                else:
                    extra_evdev_paths.append(path)
                    log.info("Found SCUF evdev extra node: %s (%s)", path, dev.name)
        except Exception:
            pass
    if evdev_path is None and extra_evdev_paths:
        evdev_path = extra_evdev_paths.pop(0)
    for sysfs in sorted(gl.glob("/sys/class/hidraw/*/device/uevent")):
        try:
            content = open(sysfs).read().lower()
            if f"{VID:04x}" in content and f"{PID:04x}" in content:
                hidraw_path = "/dev/" + sysfs.split("/")[4]
                log.info("Found SCUF hidraw device: %s", hidraw_path)
                break
        except Exception:
            pass
    if not evdev_path:
        log.error("SCUF evdev device not found (VID=%04x PID=%04x)", VID, PID)
    if not hidraw_path:
        log.error("SCUF hidraw device not found")
    return evdev_path, extra_evdev_paths, hidraw_path


def centered_u16_to_trigger(raw, max_val):
    delta = max(0, min(raw - 0x8000, 0x7FFF))
    return int(round(delta / 0x7FFF * max_val))


def apply_stick_filter(axis_code, value, last, dz):
    if axis_code in (e.ABS_RX, e.ABS_RY):
        deadzone, jitter = dz["right_deadzone"], dz["right_jitter"]
    else:
        deadzone, jitter = dz["left_deadzone"], dz["left_jitter"]
    v = 0 if abs(value) < deadzone else value
    prev = last.get(axis_code)
    if prev is not None and v != 0 and abs(v - prev) < jitter:
        return None
    if prev is not None and v == 0 and prev == 0:
        return None
    last[axis_code] = v
    return v


def main():
    log.info("=" * 60)
    log.info("scuf_virtual_pad_managed starting")
    log.info("Config path: %s", CONFIG_PATH)

    evdev_path, extra_evdev_paths, hidraw_path = find_scuf_devices()
    if not evdev_path:
        log.critical("Aborting: SCUF evdev device not found")
        print("SCUF evdev device not found.", file=sys.stderr)
        sys.exit(1)
    if not hidraw_path:
        log.critical("Aborting: SCUF hidraw device not found")
        print("SCUF hidraw device not found.", file=sys.stderr)
        sys.exit(1)

    try:
        dev = InputDevice(evdev_path)
        dev.grab()
        log.info("Grabbed exclusive access to %s", evdev_path)
    except Exception as ex:
        log.critical("Failed to grab evdev device: %s", ex)
        sys.exit(1)

    extra_devs = []
    for path in extra_evdev_paths:
        try:
            xdev = InputDevice(path)
            xdev.grab()
            extra_devs.append(xdev)
            log.info("Grabbed extra SCUF evdev node: %s", path)
        except Exception as ex:
            log.warning("Could not grab extra evdev node %s: %s", path, ex)

    hid_fd = os.open(hidraw_path, os.O_RDONLY | os.O_NONBLOCK)
    log.info("Opened hidraw fd=%d", hid_fd)

    ui = make_uinput()

    sel = selectors.DefaultSelector()
    sel.register(dev.fd, selectors.EVENT_READ, data="evdev")
    for xdev in extra_devs:
        sel.register(xdev.fd, selectors.EVENT_READ, data="evdev_extra")
    sel.register(hid_fd, selectors.EVENT_READ, data="hidraw")

    cfg        = load_config()
    btn_remap  = build_remap(cfg)
    dz         = cfg["deadzones"]
    try:
        config_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        config_mtime = 0

    last_stick = {}
    last_l2 = last_r2 = 0
    hat_x = hat_y = 0
    last_dpad = {b: 0 for b in [
        e.BTN_DPAD_LEFT, e.BTN_DPAD_RIGHT,
        e.BTN_DPAD_UP,   e.BTN_DPAD_DOWN,
    ]}

    log.info("Bridge running — entering event loop")

    def apply_dpad():
        states = {
            e.BTN_DPAD_LEFT:  1 if hat_x == -1 else 0,
            e.BTN_DPAD_RIGHT: 1 if hat_x ==  1 else 0,
            e.BTN_DPAD_UP:    1 if hat_y == -1 else 0,
            e.BTN_DPAD_DOWN:  1 if hat_y ==  1 else 0,
        }
        for btn, state in states.items():
            if last_dpad[btn] != state:
                ui.write(e.EV_KEY, btn, state)
                last_dpad[btn] = state

    try:
        while True:
            for key, _mask in sel.select(timeout=1.0):
                src = key.data

                if src == "hidraw":
                    try:
                        data = os.read(hid_fd, 64)
                    except BlockingIOError:
                        continue
                    if data and data[0] == 0x06 and len(data) >= 14:
                        raw = struct.unpack_from("<H", data, 9)[0]
                        r2 = centered_u16_to_trigger(raw, R2_MAX)
                        if r2 != last_r2:
                            ui.write(e.EV_ABS, e.ABS_RZ, r2)
                            last_r2 = r2
                            ui.syn()

                elif src == "evdev_extra":
                    # Drain extra interfaces (paddle nodes) — grabbed to hide from Steam.
                    # Events are discarded until paddle forwarding is implemented.
                    fd_dev = next((d for d in extra_devs if d.fd == key.fd), None)
                    if fd_dev:
                        try:
                            list(fd_dev.read())
                        except BlockingIOError:
                            pass

                elif src == "evdev":
                    try:
                        batch = dev.read()
                    except BlockingIOError:
                        continue
                    for ev_event in batch:
                        if ev_event.type == e.EV_KEY:
                            out = btn_remap.get(ev_event.code, ev_event.code)
                            if out in VIRTUAL_BUTTONS:
                                log.debug(
                                    "BTN %s -> %s (value=%d)",
                                    ev_event.code, out, ev_event.value,
                                )
                                ui.write(e.EV_KEY, out, ev_event.value)
                        elif ev_event.type == e.EV_ABS:
                            code, val = ev_event.code, ev_event.value
                            if code in (e.ABS_X, e.ABS_Y):
                                f = apply_stick_filter(code, val, last_stick, dz)
                                if f is not None:
                                    ui.write(e.EV_ABS, code, f)
                            elif code == e.ABS_RX:
                                if val != last_l2:
                                    ui.write(e.EV_ABS, e.ABS_Z, val)
                                    last_l2 = val
                            elif code == e.ABS_Z:
                                f = apply_stick_filter(e.ABS_RX, val, last_stick, dz)
                                if f is not None:
                                    ui.write(e.EV_ABS, e.ABS_RX, f)
                            elif code == e.ABS_RZ:
                                f = apply_stick_filter(e.ABS_RY, val, last_stick, dz)
                                if f is not None:
                                    ui.write(e.EV_ABS, e.ABS_RY, f)
                            elif code == e.ABS_HAT0X:
                                hat_x = val
                                apply_dpad()
                            elif code == e.ABS_HAT0Y:
                                hat_y = val
                                apply_dpad()
                    ui.syn()

            # Hot-reload: check config file mtime every loop iteration (1s timeout above)
            try:
                mtime = os.path.getmtime(CONFIG_PATH)
                if mtime != config_mtime:
                    log.info("Config file changed — hot-reloading")
                    config_mtime = mtime
                    cfg       = load_config()
                    btn_remap = build_remap(cfg)
                    dz        = cfg["deadzones"]
                    log.info("Hot-reload complete")
            except OSError:
                pass

    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except Exception as ex:
        log.exception("Unhandled error in event loop: %s", ex)
        raise
    finally:
        log.info("Releasing grab and closing devices")
        try:
            dev.ungrab()
        except Exception:
            pass
        for xdev in extra_devs:
            try:
                xdev.ungrab()
            except Exception:
                pass
        try:
            ui.close()
        except Exception:
            pass
        os.close(hid_fd)
        log.info("scuf_virtual_pad_managed stopped")


if __name__ == "__main__":
    main()
