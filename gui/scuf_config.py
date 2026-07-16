#!/usr/bin/env python3
"""
scuf_config.py - SCUF Envision Pro config tool (Linux)

Usage:
  scuf_config.py show [--profile N] [--json]
  scuf_config.py remap <source> <target> [--profile N] [--hidraw /dev/hidrawX]
  scuf_config.py unmap <source> [--profile N]
  scuf_config.py show-presets [--profile N] [--json]
  scuf_config.py set-preset <name> [--left-curve TYPE] [--right-curve TYPE]
                                    [--left-dz PCT] [--left-max-dz PCT]
                                    [--right-dz PCT] [--right-max-dz PCT]
                                    [--profile N]
  scuf_config.py show-trigger-presets [--profile N] [--json]
  scuf_config.py set-trigger-preset <name> [--left-curve TYPE] [--right-curve TYPE]
                                           [--left-dz PCT] [--left-max-dz PCT]
                                           [--right-dz PCT] [--right-max-dz PCT]
                                           [--profile N]
  scuf_config.py brightness <percent>
  scuf_config.py eco-mode <on|off>
  scuf_config.py auto-shutoff <on|off> [--minutes N]
  scuf_config.py show-vibration [--profile N] [--json]
  scuf_config.py set-vibration [--left PCT] [--right PCT] [--profile N]

Source keys: P1 P2 P3 P4 S1 S2
Target keys: A B X Y  LB RB  LT RT  L3 R3  Up Down Left Right
Curve types: dynamic  linear  exponential  aggressive  custom

Thumbstick and trigger presets share the same <gamepadSensitivityPresets> XML block,
distinguished by <inputDevice>0</inputDevice> (sticks) vs <inputDevice>1</inputDevice> (triggers).
Curve integers are identical for both: Custom=0 Exponential=1 Linear=2 Dynamic=3 Aggressive=4

Examples:
  scuf_config.py show
  scuf_config.py remap P4 RB
  scuf_config.py remap S1 A --profile 2
  scuf_config.py show-presets
  scuf_config.py set-preset "HW Thumbstick Preset 1" --left-curve linear --right-curve linear --left-dz 3 --right-dz 3
  scuf_config.py show-trigger-presets
  scuf_config.py set-trigger-preset "aggressive" --left-curve aggressive --right-curve aggressive
"""

import os
import sys
import fcntl
import errno
import uuid
import zlib
import struct
import re
import json
import argparse

VID, PID = 0x2e95, 0x434d
CHUNK_SIZE = 512

# Main-profile XML blob slots — one per physical profile stored on device.
# Profile 1 = index 0.
MAIN_SLOTS = [
    bytes.fromhex('606d'),
    bytes.fromhex('6a6d'),
    bytes.fromhex('746d'),
]

# Per-profile manifest slots.
# Manifest contains sub-slot references + action count + profile name.
MANIFEST_SLOTS = [
    bytes.fromhex('0700'),  # Profile 1
    bytes.fromhex('0800'),  # Profile 2
    bytes.fromhex('0900'),  # Profile 3
]

# Map actual main slot bytes → corresponding manifest slot bytes.
# Used by _write_profile_atomic to look up the manifest for any slot it receives.
MAIN_TO_MANIFEST = {MAIN_SLOTS[i]: MANIFEST_SLOTS[i] for i in range(len(MAIN_SLOTS))}

SOURCE_KEYS = {
    'P1': 'GamepadP1', 'P2': 'GamepadP2',
    'P3': 'GamepadP3', 'P4': 'GamepadP4',
    'S1': 'GamepadS1', 'S2': 'GamepadS2',
}

TARGET_KEYS = {
    'A':     'GamepadFunctionButtonBottom',
    'B':     'GamepadFunctionButtonRight',
    'X':     'GamepadFunctionButtonLeft',
    'Y':     'GamepadFunctionButtonTop',
    'LB':    'GamepadLeftBumper',
    'L1':    'GamepadLeftBumper',
    'RB':    'GamepadRightBumper',
    'R1':    'GamepadRightBumper',
    'LT':    'GamepadLeftTrigger',
    'L2':    'GamepadLeftTrigger',
    'RT':    'GamepadRightTrigger',
    'R2':    'GamepadRightTrigger',
    'L3':    'GamepadLeftStickButton',
    'R3':    'GamepadRightStickButton',
    'UP':    'GamepadDPadUp',
    'DOWN':  'GamepadDPadDown',
    'LEFT':  'GamepadDPadLeft',
    'RIGHT': 'GamepadDPadRight',
}

# Reverse map for display
KEYNAME_DISPLAY = {v: k for k, v in {**SOURCE_KEYS, **TARGET_KEYS}.items()}

# ---------------------------------------------------------------------------
# Thumbstick preset constants
# ---------------------------------------------------------------------------

CURVE_CUSTOM      = 0
CURVE_EXPONENTIAL = 1
CURVE_LINEAR      = 2
CURVE_DYNAMIC     = 3
CURVE_AGGRESSIVE  = 4

CURVE_NAMES = {
    'custom':      CURVE_CUSTOM,
    'exponential': CURVE_EXPONENTIAL,
    'linear':      CURVE_LINEAR,
    'dynamic':     CURVE_DYNAMIC,
    'aggressive':  CURVE_AGGRESSIVE,
}
CURVE_DISPLAY = {v: k for k, v in CURVE_NAMES.items()}

# Default custom curve control points: physical% -> output%
# Stored in XML for all curve types; only applied by firmware when curve=Custom.
DEFAULT_CURVE_PTS = {0: 0, 20: 5, 40: 15, 60: 30, 80: 60, 100: 100}

# ---------------------------------------------------------------------------
# Vibration constants
# Vibration slots: hardcoded per-profile (offsets from main are inconsistent).
#   Profile 1: 69 6d (+9)   Profile 2: 7f 6d (+21)   Profile 3: 7d 6d (+9)
# Blob format: 4-byte static header + flat [id_16le][value_32le] KV entries.
# ---------------------------------------------------------------------------

VIB_HEADER    = bytes.fromhex('3dd40e00')  # static; unchanged between writes
VIB_LEFT_ID   = 0x0084                     # left motor intensity, 0-100
VIB_RIGHT_ID  = 0x0085                     # right motor intensity, 0-100

# Per-profile vibration slot IDs.
# Offsets from main slot are inconsistent (+9 for P1/P3, +21 for P2),
# so use a hardcoded table rather than a formula.
VIB_SLOTS = [
    bytes.fromhex('696d'),  # Profile 1 (main 606d + 9)
    bytes.fromhex('7f6d'),  # Profile 2 (main 6a6d + 21)
    bytes.fromhex('7d6d'),  # Profile 3 (main 746d + 9)
]


# ---------------------------------------------------------------------------
# HID transport
# ---------------------------------------------------------------------------

def _pad(data: bytes) -> bytes:
    return data.ljust(64, b'\x00')

def send(fd: int, cmd: bytes):
    os.write(fd, _pad(cmd))

def recv(fd: int) -> bytes:
    return os.read(fd, 64)


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

def find_config_hidraw() -> str | None:
    """
    Return path to the SCUF config-channel hidraw device (HID Interface 4).
    Identifies by VID:PID in sysfs and `:1.4` interface marker in the device path.
    Falls back to handshake probe if path parsing is inconclusive.
    """
    candidates = []

    for name in sorted(os.listdir('/sys/class/hidraw')):
        uevent_path = f'/sys/class/hidraw/{name}/device/uevent'
        try:
            uevent = open(uevent_path).read().lower()
        except OSError:
            continue
        if f'{VID:04x}' not in uevent or f'{PID:04x}' not in uevent:
            continue

        real_path = os.path.realpath(f'/sys/class/hidraw/{name}/device')
        node = f'/dev/{name}'

        # Interface 4 appears as `:1.4` in the sysfs USB device path
        if ':1.4' in real_path:
            return node

        candidates.append(node)

    # Fallback: probe each candidate with a VID query
    for node in candidates:
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
            send(fd, b'\x02\x08\x02\x11')
            resp = os.read(fd, 64)
            os.close(fd)
            if resp[4:6] == bytes([0x95, 0x2e]):  # 0x2e95 LE
                return node
        except OSError:
            pass

    return None


def find_gamepad_hidraw() -> str | None:
    """Return path to the SCUF gamepad hidraw device (HID Interface 0, endpoint 0x01)."""
    for name in sorted(os.listdir('/sys/class/hidraw')):
        uevent_path = f'/sys/class/hidraw/{name}/device/uevent'
        try:
            uevent = open(uevent_path).read().lower()
        except OSError:
            continue
        if f'{VID:04x}' not in uevent or f'{PID:04x}' not in uevent:
            continue
        real_path = os.path.realpath(f'/sys/class/hidraw/{name}/device')
        if ':1.0' in real_path:
            return f'/dev/{name}'
    return None


# Two-part write-unlock:
#  1. Two 32-byte packets to ep_01 (gamepad interface), sent once per device session.
#  2. A config-channel command sequence (`01 03 00 02`, `01 80 00 01`, ...) sent before
#     each read/write operation.
# Without part 1 the `01 XX` commands in part 2 return status=9, which in turn blocks
# `06 01` chunk writes with status=9.
_UNLOCK_PKT1 = bytes.fromhex('0500000000000000000000000000005445535450504050000000000000000000')
_UNLOCK_PKT2 = bytes.fromhex('0500000000000000000000000000000000000050504050000000000000000000')
_GAMEPAD_INTF = 3    # interface number from USB config descriptor
_GAMEPAD_EP_OUT = 0x01


def _prep_write(fd: int):
    """Send the command-channel pre-write sequence. Required before every 06 01 chunk write.

    Sets `OperatingMode = 2` (write-enabled).  Pair with `_post_write_restore`
    to flip back to `OperatingMode = 1` after writes complete — without that,
    device stays in mode 2 (paddle/macro/lights disabled) until 60s firmware
    timer expires."""
    for cmd in (
        b'\x02\x08\x01\x03\x00\x02',  # OperatingMode = 2 (write mode)
        b'\x02\x08\x01\x81\x00\x01',  # write-mode setup A
        b'\x02\x08\x01\x7d\x00\x02',  # write-mode setup B
        b'\x02\x08\x01\xde\x00\x02',  # write-mode setup C
    ):
        send(fd, cmd)
        recv(fd)


def _post_write_restore(fd: int):
    """Restore device to normal operation after writes complete.

    `_prep_write` sets OperatingMode=2 (write-enabled). Device stays disabled
    in mode 2 until either a VBUS cycle or a 60s firmware timeout. Setting
    OperatingMode=1 here releases device immediately — paddles, macros, lights
    restored.
    """
    send(fd, b'\x02\x08\x01\x03\x00\x01')  # OperatingMode = 1 (normal operation)
    recv(fd)


def _prep_read(fd: int):
    """Send the command-channel pre-read sequence. Required before every 08 00 read."""
    for cmd in (
        b'\x02\x08\x01\x03\x00\x02',  # enter command mode
        b'\x02\x08\x01\x80\x00\x01',  # read-mode setup A
        b'\x02\x08\x01\x7c\x00\x02',  # read-mode setup B
        b'\x02\x08\x01\xdd\x00\x02',  # read-mode setup C
    ):
        send(fd, cmd)
        recv(fd)


def _pre_open_setconfig_dance():
    """Do 4x SetConfiguration(1) BEFORE any hidraw fd is opened.
    Must run before hidraw fd opens because detaching interface 4 for
    set_configuration invalidates any open hidraw fd. Waits for kernel to
    re-enumerate hidraw before returning.
    """
    try:
        import usb.core, usb.util
    except ImportError:
        print('  apply-dance skipped: pyusb not installed')
        return
    import time
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  apply-dance skipped: device not found')
        return
    # Try set_configuration WITHOUT detaching interface 4 (config hidraw).
    # If kernel allows it, hidraw fd later opens cleanly. If it fails
    # "Resource busy", fall back to full detach path (invalidates hidraw).
    for i in range(4):
        try:
            dev.set_configuration(1)
            time.sleep(0.03)
        except usb.core.USBError as e:
            print(f'  apply-dance SetConfiguration #{i+1} failed: {e}')
            break
    else:
        print('  apply-dance: 4x SetConfiguration(1) sent (no detach)')
    usb.util.dispose_resources(dev)
    time.sleep(0.2)


def _send_write_unlock():
    """Send the two write-unlock packets via raw USB interrupt OUT to ep_01.

    Without this the controller's RAM dispatch never refreshes after flash commit.

    Requires read/write access to the USB device node (/dev/bus/usb/...). Install
    the udev rule from README.md or run with sudo if this fails.
    """
    try:
        import usb.core, usb.util
    except ImportError:
        print('  Warning: pyusb not installed; run: pip install pyusb')
        return

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  Warning: device not found via pyusb')
        return

    reattach = False
    try:
        if dev.is_kernel_driver_active(_GAMEPAD_INTF):
            dev.detach_kernel_driver(_GAMEPAD_INTF)
            reattach = True
        usb.util.claim_interface(dev, _GAMEPAD_INTF)
        import time
        dev.write(_GAMEPAD_EP_OUT, _UNLOCK_PKT1, timeout=2000)
        time.sleep(1.0)  # ~1s gap between packets
        dev.write(_GAMEPAD_EP_OUT, _UNLOCK_PKT2, timeout=2000)
        print('  write unlock sent via ep_01')
        class_reqs = []  # Handled via HIDIOCSFEATURE on the config hidraw fd.
        print(f'  (skipped {len(class_reqs)} HID feature reports — sent via HIDIOCSFEATURE elsewhere)')
    except usb.core.USBError as e:
        print(f'  Warning: write unlock failed: {e}')
        print('  Hint: install udev rule to grant USB access:')
        print(f'    echo \'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{VID:04x}", '
              f'ATTRS{{idProduct}}=="{PID:04x}", MODE="0666"\' | '
              'sudo tee /etc/udev/rules.d/99-scuf.rules')
    finally:
        try:
            usb.util.release_interface(dev, _GAMEPAD_INTF)
        except Exception:
            pass
        if reattach:
            try:
                dev.attach_kernel_driver(_GAMEPAD_INTF)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Protocol: read a profile blob from a slot
# ---------------------------------------------------------------------------

def read_blob(fd: int, slot: bytes) -> bytes:
    """
    Read raw blob from device slot via 0x0d/0x09/0x08 protocol.
    Returns the raw bytes (header + zlib stream).
    """
    send(fd, b'\x02\x08\x0d\x00' + slot)
    recv(fd)  # 01 00 0d 00 ACK

    send(fd, b'\x02\x08\x09\x00')
    resp = recv(fd)  # 01 00 09 00 [A][0e][lo][hi]
    total = struct.unpack_from('<H', resp, 6)[0]

    if total == 0:
        send(fd, b'\x02\x08\x05\x01\x00')
        recv(fd)
        return b''

    data = bytearray()
    for _ in range(-(-total // 60)):  # ceil(total/60)
        send(fd, b'\x02\x08\x08\x00')
        data.extend(recv(fd)[4:])  # skip 4-byte response header

    send(fd, b'\x02\x08\x05\x01\x00')
    recv(fd)

    return bytes(data[:total])


# ---------------------------------------------------------------------------
# Protocol: write a profile blob to a slot
# ---------------------------------------------------------------------------

def write_blob(fd: int, slot: bytes, blob: bytes, debug: bool = False):
    """
    Write raw blob to device slot via 0x0d/0x09/0x06/0x07/0x05 protocol.
    Splits blob into CHUNK_SIZE chunks with 0x06 header + 0x07 continuations.
    Raises RuntimeError if the device rejects a chunk write (non-zero status).

    CALLER MUST have already invoked `_send_write_unlock()` and `_prep_write(fd)`
    once at the top of the write batch. Calling `_prep_write` per-slot causes
    incompatible command repetition — the 01 03 / 81 / 7d / de prep sequence
    should be sent 2-3 times total per batch, not per-write.
    """
    send(fd, b'\x02\x08\x0d\x01' + slot)
    resp = recv(fd)
    if debug:
        print(f'  write_blob {slot.hex()}: 0d01 resp={resp[:8].hex()}')

    send(fd, b'\x02\x08\x09\x01')
    resp = recv(fd)
    if debug:
        print(f'  write_blob {slot.hex()}: 0901 resp={resp[:8].hex()}')

    chunks = [blob[i:i + CHUNK_SIZE] for i in range(0, len(blob), CHUNK_SIZE)]
    for chunk in chunks:
        size_hdr = struct.pack('<H', len(chunk))
        pkt = b'\x02\x08\x06\x01' + size_hdr + b'\x00\x00' + chunk[:56]
        if debug:
            print(f'  06 01 pkt[:16]={pkt[:16].hex()}')
        send(fd, pkt)
        resp = recv(fd)
        status = resp[3] if len(resp) > 3 else 0xFF
        if status != 0:
            raise RuntimeError(
                f'write_blob: slot {slot.hex()} chunk write rejected by device '
                f'(06 01 status={status}). resp={resp[:8].hex()}'
            )
        for off in range(56, len(chunk), 60):
            send(fd, b'\x02\x08\x07\x01' + chunk[off:off + 60])
            resp = recv(fd)
            status = resp[3] if len(resp) > 3 else 0xFF
            if status != 0:
                raise RuntimeError(
                    f'write_blob: slot {slot.hex()} continuation rejected '
                    f'(07 01 status={status}). resp={resp[:8].hex()}'
                )

    send(fd, b'\x02\x08\x05\x01\x01')
    recv(fd)


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def blob_to_xml(blob: bytes) -> tuple[bytes, int]:
    """
    Decompress XML from blob.
    Returns (xml_bytes, zlib_offset) so we can reconstruct the blob later.
    Uses decompressobj so trailing null-pad bytes are silently ignored.
    """
    zlib_off = next(
        (i for i in range(min(32, len(blob) - 1))
         if blob[i] == 0x78 and blob[i + 1] == 0xda),
        None
    )
    if zlib_off is None:
        raise ValueError('No zlib stream found in blob')
    d = zlib.decompressobj()
    xml = d.decompress(blob[zlib_off:])
    return xml, zlib_off


def _strip_xml_whitespace(xml_bytes: bytes) -> bytes:
    """Strip inter-tag whitespace from XML bytes. Safe for element-only content."""
    return re.sub(rb'>\s+<', b'><', xml_bytes).strip()


def xml_to_blob(xml_bytes: bytes, original_blob: bytes, zlib_off: int) -> bytes:
    """
    Recompress XML and reconstruct blob, updating the 24-bit big-endian
    uncompressed-XML-size field at header bytes 4-6.
    Pads output to len(original_blob) with null bytes so the device always
    receives exactly the slot-sized write it expects.
    """
    xml_bytes = _strip_xml_whitespace(xml_bytes)
    header = bytearray(original_blob[:zlib_off])
    xml_len = len(xml_bytes)
    # Bytes 4-6: 24-bit big-endian uncompressed XML length
    header[4] = (xml_len >> 16) & 0xFF
    header[5] = (xml_len >> 8) & 0xFF
    header[6] = xml_len & 0xFF
    blob = bytes(header) + zlib.compress(xml_bytes, level=9)
    # Pad to original slot size — device expects exactly this many bytes
    slot_max = len(original_blob)
    if len(blob) < slot_max:
        blob += b'\x00' * (slot_max - len(blob))
    elif len(blob) > slot_max:
        raise ValueError(
            f'Compressed blob ({len(blob)} bytes) exceeds slot max ({slot_max} bytes)'
        )
    return blob


def parse_paddles(xml_str: str) -> dict[str, str]:
    """Return {source_key: keyName} for all active remaps found in XML.

    Active remap = value block where both <second><key>GamepadXX</key></second>
    (non-empty source) and <keyName>GamepadYY</keyName> (non-empty target) are
    present. Works for both:
      - polymorphic_id=1 + key + keyName  (legacy format)
      - polymorphic_id=2147483649 KeyRemapAction + keyName (default-profile format)
    Reserved-but-empty slots have a keyName but no <key> — those are excluded.
    """
    result = {}
    for block in re.finditer(r'<value\d+>(.*?)</value\d+>', xml_str, re.DOTALL):
        body = block.group(1)
        key_m = re.search(r'<key>([^<]+)</key>', body)
        kn_m = re.search(r'<keyName>([^<]+)</keyName>', body)
        if key_m and kn_m and key_m.group(1) in SOURCE_KEYS.values():
            result[key_m.group(1)] = kn_m.group(1)
    return result


def remove_source_block(xml_str: str, source_key: str) -> str:
    """Remove the <value*> block that contains <key>{source_key}</key>."""
    return re.sub(
        r'<value\d+>(?:(?!<value\d+>).)*?' + re.escape(f'<key>{source_key}</key>') + r'.*?</value\d+>\s*',
        '',
        xml_str,
        flags=re.DOTALL,
    )


def set_keyname(xml_str: str, source_key: str, new_keyname: str) -> str:
    """
    Replace <keyName> inside the <value*> block that contains
    <key>{source_key}</key>. Raises ValueError if source key not found.
    """
    found = [False]

    def replacer(m):
        block = m.group(0)
        if f'<key>{source_key}</key>' not in block:
            return block
        found[0] = True
        return re.sub(
            r'<keyName>[^<]+</keyName>',
            f'<keyName>{new_keyname}</keyName>',
            block
        )

    result = re.sub(r'<value\d+>.*?</value\d+>', replacer, xml_str, flags=re.DOTALL)
    if not found[0]:
        raise ValueError(f'{source_key} not found in profile XML')
    return result


def claim_empty_slot(xml_str: str, source_key: str, keyname: str,
                     original_blob: bytes, zlib_off: int) -> str:
    """
    Find the first <value*> block with an empty <second><key></key> and
    assign it to source_key/keyname. Slot size is fixed; pre-clears names on
    all empty-key blocks to reclaim space. Raises ValueError if it still won't fit.
    """
    slot_max = len(original_blob)

    # Step 1: clear <name> display labels from ALL value blocks to recover bytes.
    # These are UI labels only — device firmware ignores them for remap logic.
    def clear_names(m):
        block = m.group(0)
        block = re.sub(r'<name>[^<]*</name>', '<name></name>', block, count=1)
        return block

    base_xml = re.sub(r'<value\d+>.*?</value\d+>', clear_names, xml_str, flags=re.DOTALL)

    # Step 2: claim the first empty-key slot
    found = [False]

    def claim(m):
        if found[0]:
            return m.group(0)
        block = m.group(0)
        if re.search(r'<second>[^<]*<key></key>', block, re.DOTALL):
            found[0] = True
            block = re.sub(r'(<second>[^<]*<key>)</key>',
                           rf'\g<1>{source_key}</key>', block, flags=re.DOTALL)
            block = re.sub(r'<keyName>[^<]+</keyName>',
                           f'<keyName>{keyname}</keyName>', block, count=1)
        return block

    result = re.sub(r'<value\d+>.*?</value\d+>', claim, base_xml, flags=re.DOTALL)
    if not found[0]:
        raise ValueError(f'No empty slot available to assign {source_key}')

    # Step 3: verify resulting blob fits in the slot
    try:
        xml_to_blob(result.encode('utf-8'), original_blob, zlib_off)
    except ValueError:
        raise ValueError(
            f'{source_key} cannot be remapped to {keyname} in this profile '
            f'(compressed blob exceeds {slot_max}-byte slot). '
            f'Use a profile where {source_key} is already configured, or pick a shorter target name.'
        )
    return result


# ---------------------------------------------------------------------------
# Default-profile seed (for profiles whose main slot is only the 59-byte
# factory blob with no XML).
#
# Template XML has one KeyRemapAction slot (source=GamepadP1, target=A).
# To seed a different remap we swap the <key>GamepadP1</key> with the target
# source button and swap the <keyName> with the target keyname. The resulting
# XML + zlib blob uses the standard 0b b1 02 00 header.
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
_TEMPLATE_WITH_REMAP_PATH = os.path.join(_TEMPLATES_DIR, 'default_profile_with_one_remap.xml')
_TEMPLATE_CLEAN_PATH      = os.path.join(_TEMPLATES_DIR, 'default_profile_clean.xml')
_TEMPLATE_SOURCE_KEY      = 'GamepadP1'             # source button baked into with-remap template
_TEMPLATE_TARGET_KEYNAME  = 'GamepadFunctionButtonBottom'  # target baked in (= A button)


def _load_template(clean: bool = False) -> str:
    """Load a default-profile template XML.

    clean=False → template with a single GamepadP1 → A KeyRemapAction baked in.
    clean=True  → template with zero mappings (empty <actions size="dynamic"/>).
    """
    path = _TEMPLATE_CLEAN_PATH if clean else _TEMPLATE_WITH_REMAP_PATH
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundError(
            f'Default-profile template missing: {path}.'
        )


def _load_default_template() -> str:
    """Back-compat shim for the original loader. Returns the with-remap template."""
    return _load_template(clean=False)


# A single KeyRemapAction <value>…</value> body (no outer <valueN> wrapper).
# Per-remap template: substitute __KEYNAME__ / __SRC__ / __UUID__ / __ID__
# for each active remap when building a multi-remap XML from the clean base.
_KEYREMAP_BLOCK_TEMPLATE = (
    '<first><polymorphic_id>2147483649</polymorphic_id>'
    '<polymorphic_name>KeyRemapAction</polymorphic_name>'
    '<ptr_wrapper><id>__ID__</id><data><cereal_class_version>400</cereal_class_version>'
    '<base><cereal_class_version>202</cereal_class_version><name>Action __N__</name>'
    '<id>__UUID__</id><repeatOptions><cereal_class_version>300</cereal_class_version>'
    '<repeatCount>1</repeatCount><repeatMode>NoRepeat</repeatMode><delay>0</delay>'
    '<delayMode>Constant</delayMode><randomDelayFrom>0</randomDelayFrom>'
    '<randomDelayTo>0</randomDelayTo></repeatOptions>'
    '<executionHints><cereal_class_version>201</cereal_class_version>'
    '<terminateOnSecondExec>false</terminateOnSecondExec>'
    '<restartOnSecondExec>false</restartOnSecondExec><execHint>OnBoth</execHint>'
    '<retainOriginalKeyOutput>false</retainOriginalKeyOutput></executionHints>'
    '<actionLighting>{00000000-0000-0000-0000-000000000000}</actionLighting>'
    '<actionSoundPath></actionSoundPath><attachedActions size="dynamic"/></base>'
    '<keyName>__KEYNAME__</keyName><keyStroke size="dynamic"/>'
    '<holdingKeyEnabled>false</holdingKeyEnabled><holdingKeyType>OnPress</holdingKeyType>'
    '<holdingKeyOnPressInterval>100</holdingKeyOnPressInterval><programPath></programPath>'
    '<sniperSwitchMode>WhilePressed</sniperSwitchMode><remapGroup>10</remapGroup>'
    '</data></ptr_wrapper></first>'
    '<second><cereal_class_version>400</cereal_class_version><key>__SRC__</key>'
    '<layer>StandardLayer</layer><event>Click</event><distance>0</distance></second>'
)


def _uuid4_braced() -> str:
    """Return a braced lowercased UUID4."""
    import uuid
    return '{' + str(uuid.uuid4()) + '}'


def build_profile_xml_with_remaps(remaps: dict[str, str]) -> str:
    """Build a complete profile XML from the clean template, with only the
    specified {source_key → target_keyname} remaps as KeyRemapAction blocks.

    remaps: {'GamepadP1': 'GamepadFunctionButtonBottom', ...}

    Guarantees: no dormant poly_id=1 blocks, no phantom mappings. Each entry
    in `remaps` becomes exactly one active remap on the device.
    """
    if not remaps:
        return _load_template(clean=True)

    base = _load_template(clean=True)
    # Build each KeyRemapAction value block with unique IDs.
    blocks = []
    base_id = 2147483653  # first action id
    for i, (src, kn) in enumerate(remaps.items()):
        block_body = (
            _KEYREMAP_BLOCK_TEMPLATE
            .replace('__KEYNAME__', kn)
            .replace('__SRC__', src)
            .replace('__UUID__', _uuid4_braced())
            .replace('__ID__', str(base_id + i))
            .replace('__N__', str(i + 1))
        )
        blocks.append(f'<value{i}>{block_body}</value{i}>')

    # Replace the empty <actions size="dynamic"/> with a populated one.
    return re.sub(
        r'<actions\s+size="dynamic"\s*/>',
        f'<actions size="dynamic">{"".join(blocks)}</actions>',
        base,
        count=1,
    )


def seed_default_profile_xml(source_key: str, new_keyname: str) -> str:
    """Return XML for a default profile with a SINGLE remap.

    Thin wrapper over build_profile_xml_with_remaps for the common case.
    """
    return build_profile_xml_with_remaps({source_key: new_keyname})


def _blob_from_xml(xml_str: str) -> bytes:
    """Compress XML with the standard 7-byte profile-blob header (0b b1 02 00 + 3-byte BE length)."""
    xml_bytes = _strip_xml_whitespace(xml_str.encode('utf-8'))
    xml_len = len(xml_bytes)
    header = bytearray(7)
    header[0:4] = bytes.fromhex('0bb10200')
    header[4] = (xml_len >> 16) & 0xFF
    header[5] = (xml_len >> 8) & 0xFF
    header[6] = xml_len & 0xFF
    return bytes(header) + zlib.compress(xml_bytes, level=9)


def build_default_profile_blob(source_key: str, new_keyname: str) -> bytes:
    """Compressed blob for a default profile + one remap. Used by cmd_remap."""
    return _blob_from_xml(seed_default_profile_xml(source_key, new_keyname))


def build_profile_blob_with_remaps(remaps: dict[str, str]) -> bytes:
    """Compressed blob for a profile with an arbitrary set of active remaps.

    Starts from the clean (zero-mapping) template and injects one KeyRemapAction
    block per entry in `remaps`. Use this when you need to rebuild a profile
    from scratch — e.g. when legacy set_keyname / claim_empty_slot paths don't
    apply (no existing mapping of that source, no reserved empty slot).
    """
    return _blob_from_xml(build_profile_xml_with_remaps(remaps))


# Target-keyname → byte-13 code in the 22-byte action-header blob at slot 61 6d.
#
# Face buttons / bumpers: unique per-button codes — firmware decodes the button
# directly from this byte.
#
# DPad / triggers / stick clicks: all use 0x0a. The firmware reads the XML
# preset entry (slot 626d–676d index chain → 606d blob) to determine which
# specific button fires. The action header code is a type discriminator only.
#
# Back / Start / Guide: unknown codes; likely distinct but not yet included.
_ACTION_HDR_TARGET_CODES = {
    'GamepadFunctionButtonBottom': 0x1e,   # A
    'GamepadFunctionButtonRight':  0x04,   # B
    'GamepadFunctionButtonLeft':   0x06,   # X
    'GamepadFunctionButtonTop':    0x08,   # Y
    'GamepadLeftBumper':           0x12,   # LB
    'GamepadRightBumper':          0x14,   # RB
    # DPad / triggers / stick clicks — confirmed 0x0a across all 8 captures.
    'GamepadDPadUp':               0x0a,
    'GamepadDPadDown':             0x0a,
    'GamepadDPadLeft':             0x0a,
    'GamepadDPadRight':            0x0a,
    'GamepadLeftTrigger':          0x0a,
    'GamepadRightTrigger':         0x0a,
    'GamepadLeftStickButton':      0x0a,   # L3
    'GamepadRightStickButton':     0x0a,   # R3
}


# Canonical per-profile manifest. Used to bootstrap a profile whose manifest
# has been truncated (action_hdr=0000) by a prior factory-reset. Writing these
# values restores the sub-slot references the firmware needs for button dispatch.
_CANONICAL_MANIFESTS = {
    bytes.fromhex('606d'): bytes.fromhex(  # P1 manifest, written to slot 0700
        '1ef11e000000686d816d616d806d696d00000000826d00000000000000001e00'
        '454e564953494f4e2050524f2044656661756c742050726f66696c6520310000'
        '00000000000000'
    ),
    # P2 and P3 canonical manifests TBD.
}


# P1 bootstrap bundle — the complete set of slot contents written when a
# user remaps the first paddle on a firmware-reset P1. The baked-in remap is
# P1 → GamepadFunctionButtonBottom (A). Other remaps are derived by modifying
# 606d (XML) and 616d (byte 13 code) at bootstrap time — all other slots stay
# identical.
#
# WARNING: writing only PART of this bundle (e.g. just the manifest) to a
# factory-reset profile can corrupt the controller. Always write the full
# bundle atomically.
_P1_BOOTSTRAP_SLOTS = [
    # (slot_hex, filename) — atomic write order
    ('606d', '606d.bin'),   # main profile XML (1788 B)
    ('616d', '616d.bin'),   # action header (22 B) — the dispatch payload
    ('626d', '626d.bin'),   # preset entry 1 (10 B)
    ('636d', '636d.bin'),   # preset entry 2
    ('646d', '646d.bin'),   # preset entry 3
    ('656d', '656d.bin'),   # preset entry 4
    ('666d', '666d.bin'),   # preset entry 5
    ('676d', '676d.bin'),   # preset entry 6
    ('686d', '686d.bin'),   # preset directory (52 B)
    ('696d', '696d.bin'),   # enable map (34 B)
    ('806d', '806d.bin'),   # vib_link (4 B: "il\0\0")
    ('816d', '816d.bin'),   # vib config (88 B, 3d d4 0e 00 header)
    ('826d', '826d.bin'),   # compact manifest (59 B, 26 00 04 00 header)
    ('0700', '0700.bin'),   # P1 manifest (71 B) — written last
    ('2b00', '2b00.bin'),   # global metadata (59 B) — pre-session writes
    ('0200', '0200.bin'),   # global metadata (32 B) — pre-session writes
]

_BOOTSTRAP_DIR = os.path.join(_TEMPLATES_DIR, 'p1_bootstrap')


def _load_p1_bootstrap_bundle() -> dict[str, bytes]:
    """Return {slot_hex: blob} for every slot in a factory-reset + first-remap
    session. Raises FileNotFoundError if any bundle file is missing."""
    out = {}
    for slot_hex, fname in _P1_BOOTSTRAP_SLOTS:
        path = os.path.join(_BOOTSTRAP_DIR, fname)
        try:
            with open(path, 'rb') as f:
                out[slot_hex] = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(
                f'Bootstrap bundle slot file missing: {path}.'
            )
    return out


def _load_p1_full_sequence(variant: str = 'A') -> list[tuple[int, bytes]]:
    """Load the full OUT sequence for one of the captured P1 variants.

    variant: 'A', 'B', 'X', 'Y', 'LB', 'RB' — which capture to replay.
    Default 'A' loads `sequence.bin` (legacy p1_to_A capture, kept for
    backwards compatibility).

    SCUF_TRIM_INIT=1: drop the init/preamble packets (everything before
    the first RemoveFile(0x6d61) marker).
    """
    # Non-P1 source captures use `sequence_<variant>.bin` naming
    # (e.g. 'p2_A' → sequence_p2_A.bin). P1 source captures use
    # `sequence_p1_to_<variant>.bin`. Legacy fallback for A.
    non_p1 = os.path.join(_BOOTSTRAP_DIR, f'sequence_{variant}.bin')
    if os.path.exists(non_p1):
        path = non_p1
    elif variant == 'A' and os.path.exists(os.path.join(_BOOTSTRAP_DIR, 'sequence.bin')):
        path = os.path.join(_BOOTSTRAP_DIR, 'sequence.bin')
    else:
        path = os.path.join(_BOOTSTRAP_DIR, f'sequence_p1_to_{variant}.bin')
    with open(path, 'rb') as f:
        count = struct.unpack('<I', f.read(4))[0]
        out = []
        for _ in range(count):
            delay = struct.unpack('<I', f.read(4))[0]
            ln = struct.unpack('<H', f.read(2))[0]
            out.append((delay, f.read(ln)))

    if os.environ.get('SCUF_TRIM_INIT') == '1':
        # Save proper begins at first RemoveFile of action-header sub-slot 0x6d61.
        # Packet: 02 08 0c <subslot_lo> <subslot_hi> ...
        # i.e. starts with hex 02080c616d
        marker = bytes.fromhex('02080c616d')
        trim_idx = next((i for i, (_, pkt) in enumerate(out)
                         if pkt[:5] == marker), -1)
        if trim_idx > 0:
            dropped = trim_idx
            out = out[trim_idx:]
            # Zero the first delay so we don't replay the cumulative
            # pre-marker silence.
            if out:
                d, p = out[0]
                out[0] = (0, p)
            print(f'SCUF_TRIM_INIT=1: dropped {dropped} init packets, '
                  f'replaying {len(out)} save-core packets', flush=True)
        else:
            print('SCUF_TRIM_INIT=1: marker 02080c616d not found, '
                  'replaying full sequence', flush=True)

    return out


def _replay_p1_sequence(fd: int, overrides: dict[int, bytes] | None = None,
                         speedup: float = 1.0):
    """Replay the full captured command sequence with inter-packet timing.

    overrides: {seq_index: replacement_bytes} — swap specific commands for
    remap variants (e.g. replace 606d chunks and 616d action_hdr).
    speedup: divide delays by this factor (default 1.0 = captured timing).
    Pass speedup=10 to replay 10× faster (skip UI-driven pauses).
    """
    import fcntl, time
    overrides = overrides or {}
    seq = _load_p1_full_sequence()

    _send_write_unlock()

    # Set non-blocking mode once for the whole replay
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    try:
        for i, (delay_us, pkt) in enumerate(seq):
            if delay_us > 0:
                time.sleep((delay_us / 1_000_000.0) / max(1e-6, speedup))
            out = overrides.get(i, pkt)
            # Temporarily switch to blocking for the write
            fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            send(fd, out)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            # Drain response within a 100ms window
            deadline = time.monotonic() + 0.10
            while time.monotonic() < deadline:
                try:
                    os.read(fd, 64)
                    break
                except BlockingIOError:
                    time.sleep(0.001)
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, fl)


def build_p1_bootstrap_bundle(source_key: str, target_keyname: str) -> dict[str, bytes]:
    """Return a bootstrap bundle customized for a specific single remap.

    Uses the p1_to_A bundle as base, then modifies:
     - 606d (main XML): swap source and target keys
     - 616d (action header): swap byte-13 code to match new target

    All other slots are copied verbatim from the capture — they don't depend on
    which remap is applied.
    """
    bundle = _load_p1_bootstrap_bundle()

    # Swap target keyname in main XML
    main_blob = bundle['606d']
    if main_blob[:4] != b'\x0b\xb1\x02\x00':
        raise ValueError(f'Unexpected main blob header: {main_blob[:4].hex()}')
    xml = zlib.decompress(main_blob[7:]).decode('utf-8', errors='replace')
    xml = xml.replace(
        f'<keyName>{_TEMPLATE_TARGET_KEYNAME}</keyName>',
        f'<keyName>{target_keyname}</keyName>',
        1,
    )
    # Swap source key if different from baked-in GamepadP1
    if source_key != _TEMPLATE_SOURCE_KEY:
        xml = xml.replace(
            f'<key>{source_key}</key>',
            '<key>__PLACEHOLDER_OLD__</key>',
            1,
        )
        xml = xml.replace(
            f'<key>{_TEMPLATE_SOURCE_KEY}</key>',
            f'<key>{source_key}</key>',
            1,
        )
        xml = xml.replace(
            '<key>__PLACEHOLDER_OLD__</key>',
            f'<key>{_TEMPLATE_SOURCE_KEY}</key>',
        )
    bundle['606d'] = _blob_from_xml(xml)

    # Swap byte-13 code in action header
    code = _ACTION_HDR_TARGET_CODES.get(target_keyname)
    if code is None:
        raise ValueError(
            f'No byte-13 code known for {target_keyname}. Add to '
            '_ACTION_HDR_TARGET_CODES after capturing that single-remap.'
        )
    ah = bytearray(bundle['616d'])
    ah[13] = code
    # Keep bytes 14-16 at 0 — 4-byte LE int but only byte 13 varies in range.
    bundle['616d'] = bytes(ah)

    return bundle


def _write_bootstrap_bundle(fd: int, bundle: dict[str, bytes]):
    """Atomically write every slot in the bundle in canonical order.

    Order:
     1. Pre-session writes to 2b00 and 0200 (global metadata — REQUIRED,
        without these the device reverts writes on next validation cycle).
     2. 02 3e session init.
     3. Lock all profile-range slots (0c).
     4. Write each slot (0b → 0d01 → 0901 → 06 01/07 01 → 05 01 01).
     5. Manifest write (no 0b prefix).
     6. Per-profile commit trigger + readback of manifest + first sub-slot.
    """
    _send_write_unlock()
    _prep_write(fd)

    # Phase 0: pre-session global metadata writes. Each write uses the full
    # 0b → 0d01 → 0901 → 06 01 → 05 01 01 cycle without 02 3e wrapping.
    # 2b00 written four times and 0200 three times — partial writes can corrupt
    # the device.
    for _ in range(4):
        send(fd, b'\x02\x08\x0b\x2b\x00'); recv(fd)
        write_blob(fd, b'\x2b\x00', bundle['2b00'])
    for _ in range(3):
        send(fd, b'\x02\x08\x0b\x02\x00'); recv(fd)
        write_blob(fd, b'\x02\x00', bundle['0200'])

    # Phase 1: session init
    send(fd, b'\x02\x08\x02\x3e')
    recv(fd)

    # Phase 2: lock all profile slots
    lock_slots = [bytes.fromhex(s) for s, _ in _P1_BOOTSTRAP_SLOTS
                  if s.startswith('6') or s.startswith('8')]
    for slot in lock_slots:
        send(fd, b'\x02\x08\x0c' + slot)
        recv(fd)

    # Phase 3: write each profile slot (0b + write_blob) except manifest
    for slot_hex, _ in _P1_BOOTSTRAP_SLOTS:
        if slot_hex == '0700':
            continue  # manifest handled below
        if slot_hex in ('0200', '2b00'):
            continue  # handled in Phase 0
        slot = bytes.fromhex(slot_hex)
        send(fd, b'\x02\x08\x0b' + slot)
        recv(fd)
        write_blob(fd, slot, bundle[slot_hex])

    # Phase 4: manifest write (no 0b prefix)
    write_blob(fd, bytes.fromhex('0700'), bundle['0700'])

    # Phase 5: readback cycle matching post-session sequence exactly. Pattern:
    #   commit-trigger (0d01 2500)
    #   P1:  readback 0700 (manifest) + 616d (first sub-slot)
    #   commit-trigger
    #   P2:  readback 0800 + 6b6d
    #   commit-trigger
    #   P3:  readback 0900 + ALL P3 sub-slots (746d, 756d, 766d, 776d-7c6d, 7d6d)
    #   commit-trigger
    #
    # P3 gets the comprehensive readback as the "current active profile".
    # Without this the device treats the write as uncommitted and reverts on
    # power cycle.
    send(fd, b'\x02\x08\x0d\x01\x25\x00'); recv(fd)

    # P1 readback
    msize = len(read_blob(fd, MANIFEST_SLOTS[0]))
    if msize:
        _readback_slot(fd, MANIFEST_SLOTS[0], num_chunks=max(1, -(-msize // 60)))
    ssize = len(read_blob(fd, bytes.fromhex('616d')))
    if ssize:
        _readback_slot(fd, bytes.fromhex('616d'), num_chunks=max(1, -(-ssize // 60)))
    send(fd, b'\x02\x08\x0d\x01\x25\x00'); recv(fd)

    # P2 readback
    msize = len(read_blob(fd, MANIFEST_SLOTS[1]))
    if msize:
        _readback_slot(fd, MANIFEST_SLOTS[1], num_chunks=max(1, -(-msize // 60)))
    ssize = len(read_blob(fd, bytes.fromhex('6b6d')))
    if ssize:
        _readback_slot(fd, bytes.fromhex('6b6d'), num_chunks=max(1, -(-ssize // 60)))
    send(fd, b'\x02\x08\x0d\x01\x25\x00'); recv(fd)

    # P3 readback — manifest + ALL sub-slots (full-verification pattern)
    msize = len(read_blob(fd, MANIFEST_SLOTS[2]))
    if msize:
        _readback_slot(fd, MANIFEST_SLOTS[2], num_chunks=max(1, -(-msize // 60)))
    for p3_slot_hex in ('7d6d', '776d', '786d', '796d', '7a6d', '7b6d', '7c6d',
                        '756d', '766d', '746d'):
        p3_slot = bytes.fromhex(p3_slot_hex)
        ssize = len(read_blob(fd, p3_slot))
        if ssize:
            _readback_slot(fd, p3_slot, num_chunks=max(1, -(-ssize // 60)))
    send(fd, b'\x02\x08\x0d\x01\x25\x00'); recv(fd)


def build_action_header_blob(main_slot: bytes, target_keyname: str) -> bytes:
    """Build the 22-byte action-header blob for slot 61 6d.

    Binary layout:
        0b b1 01 01  00 00 00 00  01  <main_slot:2>  02 01  <code:4 LE>  02 00 00 00  00

    Raises ValueError if target_keyname isn't in the known-codes table. Caller
    is responsible for deciding fallback (e.g. skipping the action-header write,
    which leaves whatever the device already has in place).
    """
    code = _ACTION_HDR_TARGET_CODES.get(target_keyname)
    if code is None:
        raise ValueError(
            f'No known action-header byte-13 code for target {target_keyname}.'
        )
    out = bytearray(22)
    out[0:4] = b'\x0b\xb1\x01\x01'
    out[8] = 0x01
    out[9:11] = main_slot
    out[11:13] = b'\x02\x01'
    out[13:17] = code.to_bytes(4, 'little')
    out[17:21] = b'\x02\x00\x00\x00'
    return bytes(out)


def build_clean_default_blob() -> bytes:
    """Compressed blob for a factory-default profile with ZERO button mappings.

    Uses `default_profile_clean.xml`. The inner <actions size="dynamic"/> is
    empty so no unintended remap is introduced. Used by cmd_set_preset /
    cmd_set_vibration (and any other editor) when the target profile is in
    factory-default state and needs an XML blob seeded before edits can be
    applied.
    """
    return _blob_from_xml(_load_template(clean=True))


# ---------------------------------------------------------------------------
# Thumbstick preset XML helpers
# ---------------------------------------------------------------------------

def _curve_pts_xml(pts: dict) -> str:
    entries = ''.join(
        f'<value{i}><key>{k}</key><value>{v}</value></value{i}>'
        for i, (k, v) in enumerate(sorted(pts.items()))
    )
    return f'<customCurve size="dynamic">{entries}</customCurve>'


def parse_presets(xml_str: str, input_device: int = 0) -> list[dict]:
    """
    Return list of preset dicts from <gamepadSensitivityPresets>.
    input_device: 0 = thumbstick presets, 1 = trigger presets.
    Both live in the same XML block distinguished by <inputDevice>N</inputDevice>
    at the outer preset level (appears just before <name>).

    Each dict: {name, predefined, sticks: [{side, curve, curve_name, deadzone, max_deadzone, curve_pts}]}
    """
    start = xml_str.find('<gamepadSensitivityPresets')
    end   = xml_str.find('</gamepadSensitivityPresets>')
    if start == -1:
        return []
    block = xml_str[start:end]

    results = []
    for m in re.finditer(r'<name>([^<]+)</name>', block):
        name = m.group(1)
        # The outer <inputDevice> appears ~30-150 chars before <name> in the XML.
        # Look back 300 chars and take the last match before <name>.
        ctx_before = block[max(0, m.start() - 300):m.start()]
        outer_id_matches = list(re.finditer(r'<inputDevice>(\d+)</inputDevice>', ctx_before))
        if outer_id_matches:
            if int(outer_id_matches[-1].group(1)) != input_device:
                continue
        # If no <inputDevice> found in lookback, skip — likely a stray <name> tag.
        else:
            continue

        ctx = block[max(0, m.start() - 300): m.end() + 4000]

        predefined_m = re.search(r'<predefined>([^<]+)</predefined>', ctx)
        predefined = predefined_m.group(1).lower() in ('true', '1') if predefined_m else False

        sticks = []
        for s in re.finditer(
            r'<inputDevicePosition>(\d+)</inputDevicePosition>'
            r'<value>(\d+)</value>'
            r'<deadzone>(\d+)</deadzone>'
            r'<maximumDeadzone>(\d+)</maximumDeadzone>'
            r'.*?<customCurve[^>]*>(.*?)</customCurve>',
            ctx, re.DOTALL
        ):
            pos, curve_val, dz, maxdz, cc_raw = s.groups()
            pts = {int(k): int(v) for k, v in
                   re.findall(r'<key>(\d+)</key><value>(\d+)</value>', cc_raw)}
            curve_int = int(curve_val)
            sticks.append({
                'side':         'left' if pos == '0' else 'right',
                'curve':        curve_int,
                'curve_name':   CURVE_DISPLAY.get(curve_int, str(curve_int)),
                'deadzone':     int(dz),
                'max_deadzone': int(maxdz),
                'curve_pts':    pts,
            })

        results.append({'name': name, 'predefined': predefined, 'sticks': sticks})

    return results


def set_preset_values(xml_str: str, preset_name: str,
                      left_curve: int | None, right_curve: int | None,
                      left_dz: int | None, left_max_dz: int | None,
                      right_dz: int | None, right_max_dz: int | None,
                      left_pts: dict | None = None,
                      right_pts: dict | None = None,
                      input_device: int = 0) -> str:
    """
    Update curve type and/or deadzone for a named preset.
    Only fields with non-None values are changed; others keep current values.
    Raises ValueError if preset_name not found.
    """
    if f'<name>{preset_name}</name>' not in xml_str:
        raise ValueError(
            f'Preset {preset_name!r} not found. '
            f'Create it via the vendor Windows tool first, then run show-presets to list available names.'
        )

    # Read current values to fill in any None fields
    current = {p['name']: p for p in parse_presets(xml_str, input_device)}.get(preset_name, {})
    cur_sticks = {s['side']: s for s in current.get('sticks', [])}

    def resolve(new_val, side, field):
        if new_val is not None:
            return new_val
        stick = cur_sticks.get(side, {})
        defaults = {'curve': CURVE_LINEAR, 'deadzone': 2, 'max_deadzone': 2, 'curve_pts': DEFAULT_CURVE_PTS}
        return stick.get(field, defaults[field])

    updates = {
        0: (  # left
            resolve(left_curve,   'left', 'curve'),
            resolve(left_dz,      'left', 'deadzone'),
            resolve(left_max_dz,  'left', 'max_deadzone'),
            left_pts if left_pts is not None else cur_sticks.get('left', {}).get('curve_pts', DEFAULT_CURVE_PTS),
        ),
        1: (  # right
            resolve(right_curve,  'right', 'curve'),
            resolve(right_dz,     'right', 'deadzone'),
            resolve(right_max_dz, 'right', 'max_deadzone'),
            right_pts if right_pts is not None else cur_sticks.get('right', {}).get('curve_pts', DEFAULT_CURVE_PTS),
        ),
    }

    for pos, (curve, dz, max_dz, pts) in updates.items():
        replacement = (
            f'<inputDevicePosition>{pos}</inputDevicePosition>'
            f'<value>{curve}</value>'
            f'<deadzone>{dz}</deadzone>'
            f'<maximumDeadzone>{max_dz}</maximumDeadzone>'
            + _curve_pts_xml(pts)
        )
        pattern = (
            rf'<inputDevicePosition>{pos}</inputDevicePosition>'
            r'<value>\d+</value>'
            r'<deadzone>\d+</deadzone>'
            r'<maximumDeadzone>\d+</maximumDeadzone>'
            r'<customCurve[^>]*>.*?</customCurve>'
        )
        updated = [False]

        def replace_in_presets_block(m):
            blk = m.group(0)
            if f'<name>{preset_name}</name>' not in blk:
                return blk
            new_blk = re.sub(pattern, replacement, blk, count=1, flags=re.DOTALL)
            if new_blk != blk:
                updated[0] = True
            return new_blk

        xml_str = re.sub(
            r'<gamepadSensitivityPresets[^>]*>.*?</gamepadSensitivityPresets>',
            replace_in_presets_block,
            xml_str, flags=re.DOTALL,
        )
        if not updated[0]:
            raise ValueError(f'Could not update {"left" if pos == 0 else "right"} stick in preset {preset_name!r}')

    return xml_str


# ---------------------------------------------------------------------------
# Profile-level operations
# ---------------------------------------------------------------------------

def load_profile(fd: int, slot: bytes) -> tuple[bytes, bytes, int] | None:
    """
    Read slot and decompress.
    Returns (xml_bytes, raw_blob, zlib_offset) or None if slot is empty/small.
    """
    blob = read_blob(fd, slot)
    if len(blob) < 64:
        return None
    try:
        xml, zlib_off = blob_to_xml(blob)
        return xml, blob, zlib_off
    except ValueError:
        return None


def list_profiles(fd: int) -> list[tuple[int, bytes, bytes, bytes, int]]:
    """
    Scan all MAIN_SLOTS and return (profile_idx, slot, xml, blob, zlib_off)
    for EVERY profile position (1–3), preserving index. Profiles without an XML
    blob (e.g. default/factory state storing only a compact manifest) get
    (idx, slot, b'', b'', 0).

    profile_idx is 0-based matching MAIN_SLOTS; profile_number = profile_idx + 1.
    """
    profiles = []
    for i, slot in enumerate(MAIN_SLOTS):
        result = load_profile(fd, slot)
        if result:
            xml, blob, zlib_off = result
            profiles.append((i, slot, xml, blob, zlib_off))
        else:
            profiles.append((i, slot, b'', b'', 0))
    return profiles


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_show(fd: int, profile_idx: int | None, as_json: bool = False):
    profiles = list_profiles(fd)

    if profile_idx:
        targets = [p for p in profiles if p[0] == profile_idx - 1]
    else:
        targets = profiles

    json_out = []
    for pidx, slot, xml, _blob, _off in targets:
        profile_number = pidx + 1
        paddles = parse_paddles(xml.decode('utf-8', errors='replace')) if xml else {}
        has_xml = bool(xml)

        if as_json:
            mappings = []
            for src_key in sorted(SOURCE_KEYS.values()):
                if src_key in paddles:
                    kn = paddles[src_key]
                    short = KEYNAME_DISPLAY.get(kn, kn)
                    src_short = KEYNAME_DISPLAY.get(src_key, src_key)
                    mappings.append({
                        'source': src_key,
                        'source_short': src_short,
                        'target_keyname': kn,
                        'target_short': short,
                    })
            json_out.append({
                'profile_number': profile_number,
                'slot': slot.hex(),
                'has_xml': has_xml,
                'mappings': mappings,
            })
        else:
            tag = '' if has_xml else '  [default — no custom mappings]'
            print(f'Profile {profile_number}  (slot {slot.hex()}){tag}:')
            for src_key in sorted(SOURCE_KEYS.values()):
                if src_key in paddles:
                    kn = paddles[src_key]
                    short = KEYNAME_DISPLAY.get(kn, kn)
                    print(f'  {src_key:<14} → {short}  ({kn})')
    if as_json:
        print(json.dumps(json_out))


# Targets with capture sequences available, per source paddle. Extend as
# captures are added. Format: {source_key: {target_keyname: capture_variant}}
_VERBATIM_CAPTURES = {
    'GamepadP1': {
        'GamepadFunctionButtonBottom': 'A',
        'GamepadFunctionButtonRight':  'B',
        'GamepadFunctionButtonLeft':   'X',
        'GamepadFunctionButtonTop':    'Y',
        'GamepadLeftBumper':           'LB',
        'GamepadRightBumper':          'RB',
        'GamepadDPadUp':               'Up',
        'GamepadDPadDown':             'Down',
        'GamepadDPadLeft':             'Left',
        'GamepadDPadRight':            'Right',
        'GamepadLeftTrigger':          'LT',
        'GamepadRightTrigger':         'RT',
        'GamepadLeftStickButton':      'L3',
        'GamepadRightStickButton':     'R3',
    },
    'GamepadP2': {
        'GamepadFunctionButtonBottom': 'p2_A',
        'GamepadFunctionButtonRight':  'p2_B',
        'GamepadFunctionButtonLeft':   'p2_X',
        'GamepadLeftTrigger':          'p2_LT',
    },
    'GamepadP3': {
        'GamepadFunctionButtonBottom': 'p3_A',
        'GamepadFunctionButtonRight':  'p3_B',
    },
    'GamepadP4': {
        'GamepadFunctionButtonBottom': 'p4_A',
        'GamepadFunctionButtonRight':  'p4_B',
        'GamepadFunctionButtonLeft':   'p4_X',
    },
    'GamepadS1': {
        'GamepadFunctionButtonBottom': 's1_A',
    },
    'GamepadS2': {
        'GamepadFunctionButtonBottom': 's2_A',
    },
}


def cmd_remap(fd: int, source: str, target: str, profile_idx: int | None):
    source_key = SOURCE_KEYS.get(source.upper(), source)
    target_kn = TARGET_KEYS.get(target.upper(),
                 target if target.startswith('Gamepad') else None)
    if target_kn is None:
        print(f'Unknown target: {target!r}', file=sys.stderr)
        print(f'Valid short names: {", ".join(sorted(TARGET_KEYS))}', file=sys.stderr)
        sys.exit(1)

    profiles = list_profiles(fd)
    idx = (profile_idx - 1) if profile_idx else 0
    match = next((p for p in profiles if p[0] == idx), None)
    if match is None:
        print(f'Profile {idx + 1} not found.', file=sys.stderr)
        sys.exit(1)
    _i, slot, _xml, _blob, _off = match

    # For Profile 1, use verbatim capture replay if a sequence for this
    # (source, target) combo exists. On success, the new mapping takes effect
    # after a controller USB restart.
    if idx == 0:  # Profile 1
        variant = _VERBATIM_CAPTURES.get(source_key, {}).get(target_kn)
        if variant is None:
            avail = _VERBATIM_CAPTURES.get(source_key, {})
            avail_str = ', '.join(KEYNAME_DISPLAY.get(k, k) for k in avail) or '(none yet)'
            print(
                f'No capture sequence available for Profile 1 {source_key} → {target_kn}.\n'
                f'Available targets for {source_key}: {avail_str}',
                file=sys.stderr,
            )
            sys.exit(1)
        seq = _load_p1_full_sequence(variant=variant)
        print(f'Profile 1: replaying capture for {source_key} → {target_kn} '
              f'(variant p1_to_{variant}, {len(seq)} commands)')
        _replay_sequence(fd, seq)
        # Restore OperatingMode=1 to release device from write mode. Without
        # this, paddles/macros/lights remain disabled until 60s firmware
        # timeout. With this, device returns to normal in <1s.
        _post_write_restore(fd)
        short_tgt = KEYNAME_DISPLAY.get(target_kn, target_kn)
        print(f'Profile {idx + 1}: {source_key} → {short_tgt}')
        print('  Mapping written + device restored to OperatingMode=1. Paddles live.')
        return

    # Profiles 2 and 3: no verbatim captures yet.
    print(
        f'Profile {idx + 1} remap not yet supported — no capture sequences for P2/P3.\n'
        f'Use the vendor Windows tool to remap, or capture P2/P3 sequences and add them.',
        file=sys.stderr,
    )
    sys.exit(1)


def _load_live_sequence() -> list[tuple[int, bytes]] | None:
    """Return None — live-sequence override path removed for public release."""
    return None


def _post_commit_ms_os_probe():
    """Read BOS descriptor, extract MS OS 2.0 vendor code, issue MS OS 2.0
    descriptor set read. Mirrors what Windows does at enum to detect XInput
    compatibility. Firmware may treat this as 'Windows host' signal."""
    try:
        import usb.core, usb.util
    except ImportError:
        print('  MS OS probe skipped: pyusb not installed')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  MS OS probe skipped: device not found')
        return
    # GET_DESCRIPTOR(BOS=0x0F00) — standard request, bmRT=0x80 (IN/Std/Dev)
    try:
        bos = dev.ctrl_transfer(0x80, 0x06, 0x0F00, 0x0000, 256, 1000)
        print(f'  BOS descriptor read: {len(bos)} bytes '
              f'{bytes(bos[:16]).hex(" ")}')
    except usb.core.USBError as e:
        print(f'  BOS descriptor read failed: {e}')
        return
    # Parse BOS for PlatformCapability with MS OS 2.0 platform UUID.
    # UUID: D8DD60DF-4589-4CC7-9CD2-659D9E648A9F (little-endian encoded in BOS)
    # Look for this pattern to find the MS OS 2.0 vendor code.
    MS_OS_UUID = bytes.fromhex('df60ddd89545c74c9cd2659d9e648a9f')
    buf = bytes(bos)
    idx = buf.find(MS_OS_UUID)
    if idx < 0:
        print('  MS OS 2.0 platform UUID not in BOS; firmware does not advertise')
        return
    # After UUID (16B) + dwWindowsVersion (4B) comes wMSOSDescriptorSetTotalLength (2B) + bMS_VendorCode (1B) + bAltEnumCode (1B)
    vend_off = idx + 16 + 4 + 2
    if len(buf) <= vend_off:
        print('  MS OS descriptor header truncated')
        return
    total_len = int.from_bytes(buf[idx+16+4:idx+16+4+2], 'little')
    vendor_code = buf[vend_off]
    print(f'  MS OS 2.0: vendor_code=0x{vendor_code:02x} total_len={total_len}')
    # Now read MS OS 2.0 descriptor set: bmRT=0xC0 (IN/Vendor/Device),
    # bReq=vendor_code, wValue=0, wIndex=0x07 (MS_OS_20_DESCRIPTOR_INDEX), wLength=total_len
    try:
        data = dev.ctrl_transfer(0xC0, vendor_code, 0x0000, 0x0007, total_len, 2000)
        print(f'  MS OS 2.0 descriptor set read: {len(data)} bytes')
    except usb.core.USBError as e:
        print(f'  MS OS 2.0 descriptor read failed: {e}')


def _post_commit_config_swap(sleep_s: float = 0.3):
    """Send SetConfiguration(0) then SetConfiguration(1) explicitly.
    Closer to Windows PDO swap than authorize cycle. WILL kill hidraw fd."""
    try:
        import usb.core, usb.util
    except ImportError:
        print('  config-swap skipped: pyusb not installed')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  config-swap skipped: device not found')
        return
    import time as _t
    detached = []
    try:
        for intf in dev.get_active_configuration():
            ino = intf.bInterfaceNumber
            if dev.is_kernel_driver_active(ino):
                try:
                    dev.detach_kernel_driver(ino)
                    detached.append(ino)
                except Exception:
                    pass
        # SetConfiguration(0) = unconfigured
        try:
            dev.ctrl_transfer(0x00, 0x09, 0x0000, 0x0000, None, 1000)
            print('  SetConfiguration(0) sent')
        except usb.core.USBError as e:
            print(f'  SetConfiguration(0) failed: {e}')
        _t.sleep(sleep_s)
        try:
            dev.ctrl_transfer(0x00, 0x09, 0x0001, 0x0000, None, 1000)
            print('  SetConfiguration(1) sent')
        except usb.core.USBError as e:
            print(f'  SetConfiguration(1) failed: {e}')
        _t.sleep(sleep_s)
    finally:
        for ino in detached:
            try: dev.attach_kernel_driver(ino)
            except Exception: pass
        try:
            import usb.util as _uu
            _uu.dispose_resources(dev)
        except Exception:
            pass


def _post_commit_authorize_cycle(wait_s: float = 1.2):
    """Write 0 then 1 to sysfs authorized to trigger full disconnect + rebind.
    Closest Linux userspace equivalent of Windows ScufDriver5.sys
    GoToMimicMode + IoInvalidateDeviceRelations, which forces PnP stack to
    tear down and recreate the device PDO.  Heavier than USBDEVFS_RESET.
    Requires write access to /sys/bus/usb/devices/BUS-PORT/authorized —
    typically root."""
    try:
        import usb.core
    except ImportError:
        print('  authorize-cycle skipped: pyusb not installed')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  authorize-cycle skipped: device not found')
        return
    # Build sysfs path from bus + port_numbers
    bus_num = dev.bus
    # dev.port_numbers returns tuple like (3,) or (5, 2)
    try:
        ports = '.'.join(str(p) for p in dev.port_numbers)
    except Exception:
        ports = str(dev.port_number)
    sysfs_id = f'{bus_num}-{ports}'
    auth_path = f'/sys/bus/usb/devices/{sysfs_id}/authorized'
    if not os.path.exists(auth_path):
        print(f'  authorize-cycle skipped: {auth_path} missing')
        return
    import time as _t
    try:
        with open(auth_path, 'w') as f:
            f.write('0\n')
        print(f'  authorize=0 written ({auth_path})')
        _t.sleep(0.4)
        with open(auth_path, 'w') as f:
            f.write('1\n')
        print(f'  authorize=1 written; waiting {wait_s}s for udev rebind')
        _t.sleep(wait_s)
    except PermissionError as e:
        print(f'  authorize-cycle permission error: {e}')
        print('  Grant write: sudo chmod 666 ' + auth_path)
    except OSError as e:
        print(f'  authorize-cycle failed: {e}')


def _post_commit_clear_halt(endpoint_addrs: list[int]):
    """Issue clear-halt on each endpoint to mimic Windows ScufDriver5.sys
    URB_FUNCTION_ABORT_PIPE (which includes CLEAR_FEATURE(ENDPOINT_HALT)).
    Uses pyusb to detach kernel driver from endpoint's interface, claim,
    clear halt, release, reattach. Safe envelope."""
    try:
        import usb.core, usb.util
    except ImportError:
        print('  CLEAR_HALT skipped: pyusb not installed')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  CLEAR_HALT skipped: device not found')
        return
    cfg = dev.get_active_configuration()
    # Map ep address → interface number
    ep_to_intf = {}
    for intf in cfg:
        for ep in intf:
            ep_to_intf[ep.bEndpointAddress] = intf.bInterfaceNumber
    for ep_addr in endpoint_addrs:
        intf_n = ep_to_intf.get(ep_addr)
        if intf_n is None:
            print(f'  CLEAR_HALT ep=0x{ep_addr:02x} skipped: '
                  f'endpoint not in active config')
            continue
        reattach = False
        try:
            if dev.is_kernel_driver_active(intf_n):
                try:
                    dev.detach_kernel_driver(intf_n)
                    reattach = True
                except usb.core.USBError as e:
                    print(f'  CLEAR_HALT ep=0x{ep_addr:02x} '
                          f'detach intf {intf_n} failed: {e}')
                    continue
            try:
                dev.clear_halt(ep_addr)
                print(f'  CLEAR_HALT ep=0x{ep_addr:02x} (intf {intf_n}) OK')
            except usb.core.USBError as e:
                print(f'  CLEAR_HALT ep=0x{ep_addr:02x} failed: {e}')
        finally:
            if reattach:
                try: dev.attach_kernel_driver(intf_n)
                except Exception: pass
    import usb.util as _uu
    _uu.dispose_resources(dev)


def _hid_class_init(intf: int = 4):
    """Mimic Windows HID class driver auto-init at handle open.

    Sends:
      - SET_IDLE(duration=0, reportID=0)  bmRT=0x21 bReq=0x0A wVal=0x0000 wIdx=intf
      - SET_PROTOCOL(report=1)            bmRT=0x21 bReq=0x0B wVal=0x0001 wIdx=intf
      - GET_REPORT_DESCRIPTOR              bmRT=0x81 bReq=0x06 wVal=0x2200 wIdx=intf

    Linux hidraw is raw passthrough — does NOT auto-init these. Windows hid.dll
    triggers them on every CreateFile. If firmware uses these as a 'Windows HID
    host attached' signal, Linux remap must do them too.

    Skips quietly if pyusb missing or device claimed (Resource busy).
    """
    try:
        import usb.core, usb.util
    except ImportError:
        print('  hid-class-init skipped: pyusb missing')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print('  hid-class-init skipped: device not found')
        return
    # NO detach — ctrl_transfer on ep0 doesn't require interface claim and
    # avoids invalidating the kernel hidraw node we'll open afterward.
    try:
        # SET_IDLE — duration=0 (infinite), reportID=0 (all reports)
        dev.ctrl_transfer(0x21, 0x0A, 0x0000, intf, None, 1000)
        print(f'  HID SET_IDLE(0, 0) intf={intf} OK')
    except usb.core.USBError as e:
        print(f'  HID SET_IDLE failed: {e}')
    try:
        # SET_PROTOCOL — 1 = report mode
        dev.ctrl_transfer(0x21, 0x0B, 0x0001, intf, None, 1000)
        print(f'  HID SET_PROTOCOL(1) intf={intf} OK')
    except usb.core.USBError as e:
        print(f'  HID SET_PROTOCOL failed: {e}')
    try:
        # GET_REPORT_DESCRIPTOR — wValue = (0x22 type << 8) | 0
        d = dev.ctrl_transfer(0x81, 0x06, 0x2200, intf, 256, 1000)
        print(f'  HID GET_REPORT_DESCRIPTOR intf={intf} OK ({len(d)} bytes)')
    except usb.core.USBError as e:
        print(f'  HID GET_REPORT_DESCRIPTOR failed: {e}')
    import usb.util as _uu
    _uu.dispose_resources(dev)


def _pipe_toggle_reset(ep_addrs: list[int]):
    """Force pipe data-toggle reset on listed endpoints by SET_FEATURE then
    CLEAR_FEATURE on ENDPOINT_HALT (feature 0).  Forces toggle to DATA0.
    Speculative: if Windows HID class preserves toggle continuously and Linux
    desyncs, this may align before replay."""
    try:
        import usb.core, usb.util
    except ImportError:
        print('  pipe-toggle-reset skipped: pyusb missing')
        return
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return
    for ep in ep_addrs:
        try:
            # SET_FEATURE ENDPOINT_HALT (0) on endpoint
            dev.ctrl_transfer(0x02, 0x03, 0x0000, ep, None, 1000)
            print(f'  pipe-toggle ep=0x{ep:02x}: SET_FEATURE(HALT) sent')
        except usb.core.USBError as e:
            print(f'  pipe-toggle ep=0x{ep:02x} SET_FEATURE failed: {e}')
        try:
            # CLEAR_FEATURE ENDPOINT_HALT (0) on endpoint
            dev.ctrl_transfer(0x02, 0x01, 0x0000, ep, None, 1000)
            print(f'  pipe-toggle ep=0x{ep:02x}: CLEAR_FEATURE(HALT) sent')
        except usb.core.USBError as e:
            print(f'  pipe-toggle ep=0x{ep:02x} CLEAR_FEATURE failed: {e}')
    import usb.util as _uu
    _uu.dispose_resources(dev)


def _hidiocsfeature(fd: int, data: bytes):
    """Send HID SET_REPORT Feature via ioctl on the config hidraw fd.
    data[0] is the HID report ID; device expects full report (ID + body)."""
    import fcntl
    HIDIOC_TYPE = ord('H')
    size = len(data)
    # _IOC(_IOC_WRITE|_IOC_READ=3, type, nr, size) with Linux size-in-bits 16-29
    nr = 0x06
    ioc = (3 << 30) | (size << 16) | (HIDIOC_TYPE << 8) | nr
    buf = bytearray(data)
    fcntl.ioctl(fd, ioc, buf, True)


def _send_apply_transition_features(fd: int):
    """Send the 6 HID feature reports for the apply-transition signal.
    Sent via HID SET_REPORT (bmRT=0x21 bReq=0x09 wVal=0x03XX wIdx=4) on the
    config interface. report_id is data[0]."""
    reports = [
        bytes.fromhex('42 20 06 00 00 00 00 00 00'),
        bytes.fromhex('41 20 08 00 00 01 01 12 00 12 00'),
        bytes.fromhex('42 20 06 01 00 00 00 00 00'),
        bytes.fromhex('42 20 06 00 00 00 00 00 00'),
        bytes.fromhex('41 20 08 00 00 01 00 bd 00 1d 00'),
        bytes.fromhex('42 20 06 01 00 00 00 00 00'),
    ]
    ok = 0
    for r in reports:
        try:
            _hidiocsfeature(fd, r)
            ok += 1
        except OSError as e:
            print(f'  Warning: HIDIOCSFEATURE {r[:4].hex(" ")} failed: {e}')
    print(f'  sent {ok}/{len(reports)} HID feature reports on config intf')


def _replay_sequence(fd: int, seq: list[tuple[int, bytes]]):
    """Replay a captured OUT command sequence with per-packet delays.

    Scheduling: RELATIVE (chain-from-actual-previous-send), NOT absolute from t0.
    Some commit-trigger → follow-up-OPEN pairs have very tight margins
    (e.g. trigger[452]→OPEN[453] = 2989 us, with ACK arriving at ~2744 us).
    With absolute scheduling, a late trigger compresses the next packet's gap,
    which can cause the OPEN to fire before the trigger's ACK arrives —
    preventing RAM dispatch refresh. Relative scheduling preserves each
    consecutive pair's gap regardless of individual packet lateness.
    """
    import fcntl, time
    # Response-gated replay: send OUT, poll read for matching ACK
    # (`01 00 CMD ...`), proceed on match or 100 ms timeout. Ignores captured
    # delay_us entirely.
    if os.environ.get('SCUF_RESPONSE_GATED') == '1':
        # Skip _send_write_unlock (TESTPP) and feature reports by default;
        # re-sending them per-invocation can put device in a bad post-save
        # state. Opt-in via SCUF_GATED_UNLOCK=1.
        if os.environ.get('SCUF_GATED_UNLOCK') == '1':
            _send_write_unlock()
            _send_apply_transition_features(fd)
        # Strip preamble to save-flow start. Use first OpenFile(KeyMappings)
        # as marker; fallback to FreeStorageSize query.
        KM_OPEN = b'\x02\x08\x0d\x01\x02\x00'
        FS_QUERY = b'\x02\x08\x02\x3e'
        save_flow_start = next(
            (i for i, (_d, p) in enumerate(seq) if p.startswith(KM_OPEN)),
            -1,
        )
        if save_flow_start < 0:
            save_flow_start = next(
                (i for i, (_d, p) in enumerate(seq) if p.startswith(FS_QUERY)),
                0,
            )
            marker = '02 08 02 3e (fallback)'
        else:
            marker = '02 08 0d 01 02 00 (OpenFile KeyMappings)'
        if save_flow_start > 0:
            print(f'  stripping preamble: skipping first {save_flow_start} '
                  f'cmds (save-flow begins at {marker})')
            seq = seq[save_flow_start:]
        # Bracket save-flow with Ping (0x12) at start and end.
        PING = (0, b'\x02\x08\x12\x00')
        if not (seq and seq[0][1].startswith(b'\x02\x08\x12\x00')):
            seq = [PING] + seq + [PING]
            print('  bracketed Ping (02 08 12 00) — save-flow start + end')
        # Send _prep_write SETs explicitly to ensure the device is in
        # write-capable mode.
        _prep_write(fd)
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        started = time.monotonic()
        timeouts = 0
        # Async/threaded replay: writer thread blasts writes at captured timing
        # without per-packet ACK gating. Drain thread consumes ACKs in parallel.
        if os.environ.get('SCUF_ASYNC_REPLAY') == '1':
            import threading
            ack_count = [0]
            stop_drain = threading.Event()
            def drain():
                while not stop_drain.is_set():
                    try:
                        resp = os.read(fd, 64)
                        if resp:
                            ack_count[0] += 1
                    except BlockingIOError:
                        time.sleep(0.0001)
            drainer = threading.Thread(target=drain, daemon=True)
            drainer.start()
            try:
                last_send = None
                for idx, (delay_us, pkt) in enumerate(seq):
                    if last_send is not None and delay_us > 0:
                        target = last_send + delay_us / 1_000_000.0
                        rem = target - time.monotonic()
                        if rem > 0:
                            time.sleep(rem)
                    send(fd, pkt)
                    last_send = time.monotonic()
            finally:
                # Let drain pick up trailing ACKs.
                time.sleep(0.5)
                stop_drain.set()
                drainer.join(timeout=1)
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            elapsed = time.monotonic() - started
            print(f'  async-replay: {len(seq)} cmds in {elapsed:.3f}s, '
                  f'{ack_count[0]} ACKs drained')
        else:
            try:
                for idx, (_delay_us, pkt) in enumerate(seq):
                    send(fd, pkt)
                    if len(pkt) < 3:
                        continue
                    cmd_byte = pkt[2]
                    deadline = time.monotonic() + 0.100
                    matched = False
                    while time.monotonic() < deadline:
                        try:
                            resp = os.read(fd, 64)
                            if (len(resp) >= 3 and resp[0] == 0x01
                                    and resp[1] == 0x00 and resp[2] == cmd_byte):
                                matched = True
                                break
                        except BlockingIOError:
                            time.sleep(0.0002)
                    if not matched:
                        timeouts += 1
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            elapsed = time.monotonic() - started
            print(f'  gated-replay: {len(seq)} cmds in {elapsed:.3f}s, '
                  f'{timeouts} ACK timeouts')
        # Post-commit CLEAR_FEATURE(ENDPOINT_HALT) via USBDEVFS_CLEAR_HALT.
        # Firmware resets endpoint data toggle and MAY double as the dispatch-
        # refresh trigger.
        halt_eps = os.environ.get('SCUF_CLEAR_HALT', '')
        if halt_eps:
            _post_commit_clear_halt([int(e, 0) for e in halt_eps.split(',') if e.strip()])
        apply_mode = os.environ.get('SCUF_APPLY_SUBSYS', '')
        if apply_mode:
            # Post-replay ApplySubsystemSettings (0x15).
            subcmds = [int(s, 0) for s in apply_mode.split(',') if s.strip()]
            for sub in subcmds:
                try:
                    send(fd, bytes([0x02, 0x08, 0x15, sub]))
                    try:
                        os.read(fd, 64)
                    except BlockingIOError:
                        pass
                    print(f'  sent ApplySubsystemSettings(0x15 sub=0x{sub:02x})')
                except OSError as e:
                    print(f'  ApplySubsystem 0x15 sub=0x{sub:02x} failed: {e}')
        session_ctl = os.environ.get('SCUF_SESSION_CTL', '')
        if session_ctl:
            # HostSoftwareSessionControl (0x1b) session bracket. Comma-separated
            # subcmds. e.g. SCUF_SESSION_CTL=0,1 for begin-end, or just 1 for
            # end-only.
            subcmds = [int(s, 0) for s in session_ctl.split(',') if s.strip()]
            for sub in subcmds:
                try:
                    send(fd, bytes([0x02, 0x08, 0x1b, sub]))
                    try:
                        os.read(fd, 64)
                    except BlockingIOError:
                        pass
                    print(f'  sent SessionCtl(0x1b sub=0x{sub:02x})')
                except OSError as e:
                    print(f'  SessionCtl 0x1b sub=0x{sub:02x} failed: {e}')
        ping_hold = float(os.environ.get('SCUF_PING_HOLD', '0'))
        if ping_hold > 0:
            # Post-replay Ping (0x12) loop at 100ms cadence for N seconds —
            # heartbeat to prevent firmware from timing out dispatch state.
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            end_at = time.monotonic() + ping_hold
            pings = 0
            try:
                while time.monotonic() < end_at:
                    try:
                        send(fd, b'\x02\x08\x12\x00')
                        pings += 1
                    except OSError:
                        break
                    # Drain any responses
                    try:
                        os.read(fd, 64)
                    except BlockingIOError:
                        pass
                    # Cadence — env var SCUF_PING_GAP_MS controls (default 100ms).
                    # Set to 0 for a tight loop.
                    gap_ms = float(os.environ.get('SCUF_PING_GAP_MS', '100'))
                    if gap_ms > 0:
                        time.sleep(gap_ms / 1000.0)
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            elapsed = time.monotonic() - (end_at - ping_hold)
            rate = pings / elapsed if elapsed > 0 else 0
            print(f'  ping-hold: sent {pings} Pings over {elapsed:.1f}s '
                  f'({rate:.0f}/sec)')
        if os.environ.get('SCUF_AUTHORIZE_CYCLE') == '1':
            # Heavier than USB bus reset: unauthorize + reauthorize triggers
            # full kernel-level disconnect + udev re-bind.
            _post_commit_authorize_cycle()
        if os.environ.get('SCUF_MS_OS_READ') == '1':
            # Probe MS OS 2.0 descriptor. Firmware MAY interpret this read as
            # "Windows host attached" and refresh the dispatch table.
            _post_commit_ms_os_probe()
        if os.environ.get('SCUF_CONFIG_SWAP') == '1':
            # Explicit SetConfiguration(0) → SetConfiguration(1) sequence.
            # WILL kill hidraw fd — last-ditch; run as standalone fix with
            # udev wait.
            _post_commit_config_swap()
        return
    # Optional live-sequence override paths (kept for compatibility; live
    # sequence loader returns None in public builds).
    if os.environ.get('SCUF_MATCH_LIVE') == '1':
        live = _load_live_sequence() or []
        live_set = {p.ljust(64, b'\0')[:64] for _, p in live}
        def _keep(p: bytes) -> bool:
            if len(p) < 3 or p[0] != 0x02 or p[1] != 0x08:
                return True
            cmd = p[2]
            # Only filter SET (0x01) and GET (0x02) by live membership.
            # Keep all session/chunk/commit ops unconditionally.
            if cmd not in (0x01, 0x02):
                return True
            key = p.ljust(64, b'\0')[:64]
            return key in live_set
        before = len(seq)
        filtered = []
        carry = 0
        for d, p in seq:
            if _keep(p):
                filtered.append((d + carry, p))
                carry = 0
            else:
                carry += d
        seq = filtered
        print(f'  match-live: kept {len(seq)}/{before} packets '
              f'({before - len(seq)} dropped)')
    if os.environ.get('SCUF_USE_LIVE') == '1':
        live = _load_live_sequence()
        if live:
            print(f'  using live sequence ({len(live)} writes) instead of '
                  f'verbatim ({len(seq)} writes)')
            seq = live
    # Override commit-trigger delays with app-layer timing (41/47/201 ms before
    # commits 2/3/4). Verbatim wire gaps of ~6.5 ms are too tight for the
    # firmware's flash-commit state machine.
    if os.environ.get('SCUF_COMMIT_SLOW') == '1':
        commit_bytes = b'\x02\x08\x0d\x01\x25\x00'
        commit_gaps_us = [41000, 47000, 201000]
        commit_idx = 0
        out = []
        for d, p in seq:
            if p.startswith(commit_bytes) and 0 < commit_idx <= len(commit_gaps_us):
                d = commit_gaps_us[commit_idx - 1]
            if p.startswith(commit_bytes):
                commit_idx += 1
            out.append((d, p))
        seq = out
        print(f'  commit-slow: overrode delay before commits 2/3/4 with '
              f'{commit_gaps_us} us (found {commit_idx} commit triggers)')
    # Unlock + HID features default ON. Set SCUF_SKIP_UNLOCK=1 to skip here
    # (saves ~2.5s/run; only needed on first-use post-VBUS).
    if os.environ.get('SCUF_SKIP_UNLOCK') != '1':
        _send_write_unlock()
        _send_apply_transition_features(fd)
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    slow_mult = float(os.environ.get('SCUF_REPLAY_SLOW', '1'))
    # Strip `02 08 01 XX` SET + `02 08 02 XX` GET init packets. Keep
    # session/chunk/commit ops (cmds 0x0b/0x0c/0x0d/0x09/0x06/0x07/0x08/0x05).
    if os.environ.get('SCUF_STRIP_INIT') == '1':
        def _is_init(pkt: bytes) -> bool:
            if len(pkt) < 3 or pkt[0] != 0x02 or pkt[1] != 0x08:
                return False
            return pkt[2] in (0x01, 0x02)
        before = len(seq)
        # When dropping a packet, fold its delay_us into the next packet so
        # relative timing of kept packets stays consistent with capture.
        filtered = []
        carry = 0
        for d, p in seq:
            if _is_init(p):
                carry += d
            else:
                filtered.append((d + carry, p))
                carry = 0
        seq = filtered
        print(f'  strip-init: dropped {before - len(seq)} SET/GET packets '
              f'({len(seq)} kept)')
    total_delay_us = sum(d for d, _ in seq)
    started = time.monotonic()
    late_max_us = 0
    last_send_t: float | None = None
    try:
        for idx, (delay_us, pkt) in enumerate(seq):
            eff_delay_s = (delay_us * slow_mult) / 1_000_000.0
            # Target: fire exactly delay_us after the ACTUAL previous send time.
            # During the wait, drain the device's response buffer so ACKs and
            # periodic status reports don't accumulate.
            if last_send_t is not None and eff_delay_s > 0:
                target = last_send_t + eff_delay_s
                while True:
                    remaining = target - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        os.read(fd, 64)
                    except BlockingIOError:
                        if remaining > 0.001:
                            time.sleep(0.0005)
                lateness = (time.monotonic() - target) * 1_000_000
                if lateness > late_max_us:
                    late_max_us = int(lateness)
            send(fd, pkt)
            last_send_t = time.monotonic()
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, fl)
    elapsed = time.monotonic() - started
    expected = total_delay_us / 1_000_000.0
    print(f'  replay: {elapsed:.3f}s actual vs {expected:.3f}s expected '
          f'(+{elapsed - expected:.3f}s drift, max late={late_max_us}us)')
    # Post-commit keepalive: send cheap GET (02 08 02 02 = GET brightness)
    # every 50 ms for N seconds to keep device out of idle-after-commit state.
    keepalive_s = float(os.environ.get('SCUF_KEEPALIVE', '0'))
    if keepalive_s > 0:
        end = time.monotonic() + keepalive_s
        sent = 0
        while time.monotonic() < end:
            try:
                send(fd, b'\x02\x08\x02\x02')
                sent += 1
            except OSError:
                break
            try:
                os.read(fd, 64)
            except BlockingIOError:
                pass
            time.sleep(0.05)
        print(f'  keepalive: sent {sent} GETs over {keepalive_s}s')
    # Drain post-commit telemetry events (`03 00 0a 00` event notifications)
    # for a few seconds after commit. Dispatch RAM refresh may require the
    # host to actively consume these.
    drain_s = float(os.environ.get('SCUF_REPLAY_DRAIN', '0'))
    if drain_s > 0:
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        end = time.monotonic() + drain_s
        ev = 0
        try:
            while time.monotonic() < end:
                try:
                    r = os.read(fd, 64)
                    if r: ev += 1
                except BlockingIOError:
                    time.sleep(0.005)
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, fl)
        print(f'  drained {ev} post-commit events in {drain_s}s')


def _find_usb_device_sysfs_path() -> str | None:
    """Locate the SCUF's USB device directory under /sys/bus/usb/devices/.

    Returns path like '/sys/bus/usb/devices/1-3' where `authorized`, `reset`,
    and interface symlinks live. Returns None if device not found.
    """
    root = '/sys/bus/usb/devices'
    try:
        entries = os.listdir(root)
    except OSError:
        return None
    for name in entries:
        # Device directories have form 'N-M' (no colon); interface dirs have colon.
        if ':' in name:
            continue
        path = os.path.join(root, name)
        try:
            with open(os.path.join(path, 'idVendor')) as f:
                vid = int(f.read().strip(), 16)
            with open(os.path.join(path, 'idProduct')) as f:
                pid = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        if vid == VID and pid == PID:
            return path
    return None


def _notify_restart_required():
    """Print clear instructions for manual controller restart.

    Tried to automate this via (a) pyusb USBDEVFS_RESET and (b) sysfs
    `authorized=0/1` toggle. Both simulate USB disconnect at kernel level
    but neither causes the controller's firmware to refresh its dispatch
    RAM. The SCUF only reloads button dispatch on actual VBUS power loss —
    which requires a physical unplug, a USB hub with per-port power control,
    or an external USB power relay.

    """
    print()
    print('  WARNING: UNPLUG + REPLUG controller to activate the new mapping.')
    print('     (Software USB reset does not refresh dispatch firmware —')
    print('      VBUS power loss is required.)')


def cmd_show_presets(fd: int, profile_idx: int | None, as_json: bool = False,
                     input_device: int = 0):
    device_label = 'trigger' if input_device == 1 else 'thumbstick'
    profiles = list_profiles(fd)
    targets = [p for p in profiles if p[0] == profile_idx - 1] if profile_idx else profiles
    json_out = []

    for pidx, slot, xml, _blob, _off in targets:
        profile_number = pidx + 1
        if not xml:
            if as_json:
                json_out.append({'profile_number': profile_number, 'slot': slot.hex(),
                                 'has_xml': False, 'presets': []})
            else:
                print(f'Profile {profile_number}  (slot {slot.hex()}) '
                      f'[{device_label} presets]: no XML (default profile)')
            continue
        xml_str = xml.decode('utf-8', errors='replace')
        presets = parse_presets(xml_str, input_device)

        if as_json:
            json_out.append({'profile_number': profile_number, 'slot': slot.hex(),
                             'has_xml': True, 'presets': presets})
        else:
            print(f'Profile {profile_number}  (slot {slot.hex()}) [{device_label} presets]:')
            if not presets:
                print('  No presets found.')
                continue
            for p in presets:
                tag = '  [built-in]' if p['predefined'] else ''
                print(f'  {p["name"]!r}{tag}')
                for s in p['sticks']:
                    print(
                        f'    {s["side"]:5s}: curve={s["curve_name"]:<12}'
                        f'  dz={s["deadzone"]}%  max_dz={s["max_deadzone"]}%'
                    )

    if as_json:
        print(json.dumps(json_out))


def cmd_set_preset(fd: int, preset_name: str, profile_idx: int | None,
                   left_curve: int | None, right_curve: int | None,
                   left_dz: int | None, left_max_dz: int | None,
                   right_dz: int | None, right_max_dz: int | None,
                   input_device: int = 0):
    profiles = list_profiles(fd)
    idx = (profile_idx - 1) if profile_idx else 0
    match = next((p for p in profiles if p[0] == idx), None)
    if match is None:
        print(f'Profile {idx + 1} not found.', file=sys.stderr)
        sys.exit(1)
    _i, slot, xml, blob, zlib_off = match
    if not xml:
        # Default-state profile: seed a clean no-remap blob then apply the preset
        # change to the seeded XML. The clean template has empty
        # <actions size="dynamic"/> so we don't introduce any side-effect remap.
        seeded_blob = build_clean_default_blob()
        xml_str = zlib.decompress(seeded_blob[7:]).decode('utf-8', errors='replace')
        # Fall through to the normal preset-edit path below, but use the seeded
        # blob's own header/zlib_off for length math.
        blob = seeded_blob
        zlib_off = 7
    else:
        xml_str = xml.decode('utf-8', errors='replace')

    try:
        updated_xml = set_preset_values(
            xml_str, preset_name,
            left_curve, right_curve,
            left_dz, left_max_dz,
            right_dz, right_max_dz,
            input_device=input_device,
        )
    except ValueError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)

    new_blob = xml_to_blob(updated_xml.encode('utf-8'), blob, zlib_off)
    _write_profile_atomic(fd, slot, new_blob)

    result_presets = {p['name']: p for p in parse_presets(updated_xml, input_device)}
    p = result_presets.get(preset_name)
    if p:
        print(f'Profile {idx + 1}: preset {preset_name!r} updated')
        for s in p['sticks']:
            print(f'  {s["side"]:5s}: curve={s["curve_name"]}  dz={s["deadzone"]}%  max_dz={s["max_deadzone"]}%')


def cmd_unmap(fd: int, source: str, profile_idx: int | None):
    source_key = SOURCE_KEYS.get(source.upper(), source)
    if source_key not in SOURCE_KEYS.values():
        print(f'Unknown source: {source!r}. Valid: {", ".join(sorted(SOURCE_KEYS))}', file=sys.stderr)
        sys.exit(1)

    profiles = list_profiles(fd)
    idx = (profile_idx - 1) if profile_idx else 0
    match = next((p for p in profiles if p[0] == idx), None)
    if match is None:
        print(f'Profile {idx + 1} not found.', file=sys.stderr)
        sys.exit(1)
    _i, slot, xml, _blob, _off = match
    existing = parse_paddles(xml.decode('utf-8', errors='replace')) if xml else {}
    if source_key not in existing:
        print(f'Profile {idx + 1}: {source_key} already unmapped')
        return

    # Same guard as cmd_remap.
    manifest_slot = MAIN_TO_MANIFEST.get(slot)
    if manifest_slot is not None:
        m = _parse_manifest(read_blob(fd, manifest_slot))
        if not m or m.get('action_hdr', b'\x00\x00') == b'\x00\x00':
            print(
                f'Error: Profile {idx + 1} has no compact action-header slot. '
                f'Use the vendor Windows tool to set up this profile before editing here.',
                file=sys.stderr,
            )
            sys.exit(1)

    del existing[source_key]
    new_blob = build_profile_blob_with_remaps(existing)
    _write_profile_atomic(fd, slot, new_blob)
    _post_write_restore(fd)
    print(f'Profile {idx + 1}: {source_key} cleared')


def cmd_remap_synth(fd: int, source: str, target: str, profile_idx: int | None):
    """Programmatic remap — synthesizes XML blob from build_profile_blob_with_remaps,
    writes via _write_profile_atomic, restores OperatingMode=1.  Works for any
    source/target/profile combo without needing a verbatim capture.

    Prerequisites:
      - Target profile must have been set up at least once via the vendor
        Windows tool (compact action-header slot present).
    """
    source_key = SOURCE_KEYS.get(source.upper(), source)
    target_kn = TARGET_KEYS.get(target.upper(),
                                target if target.startswith('Gamepad') else None)
    if target_kn is None:
        print(f'Unknown target: {target!r}', file=sys.stderr)
        print(f'Valid: {", ".join(sorted(TARGET_KEYS))}', file=sys.stderr)
        sys.exit(1)

    profiles = list_profiles(fd)
    idx = (profile_idx - 1) if profile_idx else 0
    match = next((p for p in profiles if p[0] == idx), None)
    if match is None:
        print(f'Profile {idx + 1} not found.', file=sys.stderr)
        sys.exit(1)
    _i, slot, xml, _blob, _off = match

    manifest_slot = MAIN_TO_MANIFEST.get(slot)
    if manifest_slot is not None:
        m = _parse_manifest(read_blob(fd, manifest_slot))
        if not m or m.get('action_hdr', b'\x00\x00') == b'\x00\x00':
            print(f'Error: Profile {idx + 1} not initialized. Run the vendor '
                  f'Windows tool to bootstrap before using synthetic remap.',
                  file=sys.stderr)
            sys.exit(1)

    # Preserve existing profile state — modify only the one remap surgically.
    # Using build_profile_blob_with_remaps wipes everything else (sticks,
    # vibration, colors); firmware accepts the partial write but dispatch
    # breaks.  set_keyname / claim_empty_slot edit only the relevant <value>
    # block.
    xml_str = xml.decode('utf-8', errors='replace') if xml else ''
    existing = parse_paddles(xml_str)
    if source_key in existing:
        new_xml = set_keyname(xml_str, source_key, target_kn)
    else:
        # Need original blob + zlib_off for size-constrained slot claim.
        original_blob = _blob
        zlib_off = _off if isinstance(_off, int) else 7
        new_xml = claim_empty_slot(xml_str, source_key, target_kn,
                                   original_blob, zlib_off)
    new_blob = _blob_from_xml(new_xml)
    # Update action_hdr sub-slot too — encodes target in firmware-RAM dispatch.
    # Without this override, _write_profile_atomic re-reads + re-writes stale
    # action_hdr that still points at prior target.
    overrides: dict[bytes, bytes] = {}
    if manifest_slot is not None:
        m = _parse_manifest(read_blob(fd, manifest_slot))
        if m and m.get('action_hdr', b'\x00\x00') != b'\x00\x00':
            try:
                new_action_hdr = build_action_header_blob(slot, target_kn)
                overrides[m['action_hdr']] = new_action_hdr
            except (KeyError, ValueError) as e:
                print(f'Warning: action header rebuild failed: {e}', file=sys.stderr)
    # CRITICAL: _prep_write toggles OperatingMode 1→2 (write-enabled).  Without
    # this, _write_profile_atomic still persists writes to flash BUT firmware
    # RAM dispatch never refreshes — paddle still fires old mapping.
    _prep_write(fd)
    _write_profile_atomic(fd, slot, new_blob, sub_blob_overrides=overrides)
    _post_write_restore(fd)
    short = KEYNAME_DISPLAY.get(target_kn, target_kn)
    print(f'Profile {idx + 1}: {source_key} → {short} (synthetic, action_hdr updated)')


# ---------------------------------------------------------------------------
# Brightness
# ---------------------------------------------------------------------------

def set_brightness(fd: int, percent: int):
    """
    Set LED brightness via a standalone 3-packet command sequence.
    Protocol (Interface 4 hidraw):
      SET:      02 08 01 02 00 [lo] [hi] 00  (value = percent*10 as LE uint16, range 0-1000)
      CONFIRM1: 02 08 02 02 00 00...
      CONFIRM2: 02 08 02 02 00 00...
    """
    percent = max(0, min(100, percent))
    value = percent * 10
    lo, hi = value & 0xFF, (value >> 8) & 0xFF
    send(fd, bytes([0x02, 0x08, 0x01, 0x02, 0x00, lo, hi, 0x00]))
    recv(fd)
    send(fd, bytes([0x02, 0x08, 0x02, 0x02, 0x00, 0x00]))
    recv(fd)
    send(fd, bytes([0x02, 0x08, 0x02, 0x02, 0x00, 0x00]))
    recv(fd)


def cmd_set_brightness(fd: int, percent: int):
    set_brightness(fd, percent)
    print(f'Brightness set to {percent}%')


# ---------------------------------------------------------------------------
# Eco mode
# ---------------------------------------------------------------------------

def set_eco_mode(fd: int, enabled: bool):
    """
    Toggle eco mode (global).
    Protocol: 02 08 01 0b 00 [0/1] 00
    """
    send(fd, bytes([0x02, 0x08, 0x01, 0x0b, 0x00, 1 if enabled else 0, 0x00]))
    recv(fd)


def cmd_set_eco_mode(fd: int, enabled: bool):
    set_eco_mode(fd, enabled)
    print(f"Eco mode {'on' if enabled else 'off'}")


# ---------------------------------------------------------------------------
# Auto shutoff
# ---------------------------------------------------------------------------

def set_auto_shutoff(fd: int, enabled: bool, minutes: int | None = None):
    """
    Toggle auto-shutoff (global) and optionally set the timer.
    Protocol:
      TOGGLE: 02 08 01 0d 00 [0/1] 00
      TIMER:  02 08 01 0e 00 [ms as LE uint32] 00  (sent only when enabled)
    Timer unit: milliseconds.  minutes * 60000.
    """
    send(fd, bytes([0x02, 0x08, 0x01, 0x0d, 0x00, 1 if enabled else 0, 0x00]))
    recv(fd)
    if enabled and minutes is not None:
        ms = int(minutes) * 60000
        payload = struct.pack('<I', ms)
        send(fd, bytes([0x02, 0x08, 0x01, 0x0e, 0x00]) + payload + b'\x00')
        recv(fd)


def cmd_set_auto_shutoff(fd: int, enabled: bool, minutes: int | None = None):
    set_auto_shutoff(fd, enabled, minutes)
    if enabled:
        suffix = f' ({minutes} min)' if minutes is not None else ''
        print(f'Auto shutoff on{suffix}')
    else:
        print('Auto shutoff off')


# ---------------------------------------------------------------------------
# Vibration
# ---------------------------------------------------------------------------

def vib_slot(profile_idx: int) -> bytes:
    """Return the 2-byte vibration sub-slot ID for a profile index (0-based)."""
    return VIB_SLOTS[profile_idx]


def parse_vib_blob(blob: bytes) -> dict:
    """Return {'left': int, 'right': int} from a vibration KV blob."""
    result = {}
    off = 4  # skip 4-byte static header
    while off + 6 <= len(blob):
        kid = struct.unpack_from('<H', blob, off)[0]
        val = struct.unpack_from('<I', blob, off + 2)[0]
        if kid == VIB_LEFT_ID:
            result['left'] = int(val)
        elif kid == VIB_RIGHT_ID:
            result['right'] = int(val)
        off += 6
    return result


def set_vib_values(blob: bytes, left: int, right: int) -> bytes:
    """Return updated vibration KV blob with new left/right intensity values."""
    data = bytearray(blob)
    off = 4
    while off + 6 <= len(data):
        kid = struct.unpack_from('<H', data, off)[0]
        if kid == VIB_LEFT_ID:
            struct.pack_into('<I', data, off + 2, max(0, min(100, left)))
        elif kid == VIB_RIGHT_ID:
            struct.pack_into('<I', data, off + 2, max(0, min(100, right)))
        off += 6
    return bytes(data)


def write_vib_blob(fd: int, slot: bytes, blob: bytes):
    """Write a vibration blob, prefixing with the required 0x0b command."""
    _send_write_unlock()
    _prep_write(fd)
    send(fd, b'\x02\x08\x0b' + slot)
    recv(fd)
    write_blob(fd, slot, blob)


# ---------------------------------------------------------------------------
# Atomic profile write protocol
#
# Every profile slot is written atomically: main blob + 8–9 sub-slots +
# global slot + manifest. The device validates consistency across all slots
# before committing; writing only the main blob causes a silent rollback.
# ---------------------------------------------------------------------------

def _write_slot(fd: int, slot: bytes, blob: bytes):
    """Write blob to slot with the required 0x0b unlock-for-write prefix.
    Skips the write if blob is empty — slot was already erased by _lock_slot.
    """
    if not blob:
        return
    send(fd, b'\x02\x08\x0b' + slot)
    resp = recv(fd)
    print(f'  _write_slot {slot.hex()}: 0b resp={resp[:8].hex()}')
    write_blob(fd, slot, blob, debug=True)


def _lock_slot(fd: int, slot: bytes):
    """Send 0x0c pre-write lock; required for every slot before atomic write."""
    send(fd, b'\x02\x08\x0c' + slot)
    resp = recv(fd)
    status = resp[3] if len(resp) > 3 else 0xFF
    if status not in (0, 1):
        raise RuntimeError(f'_lock_slot: slot {slot.hex()} unexpected status={status} resp={resp[:8].hex()}')


def _readback_slot(fd: int, slot: bytes, num_chunks: int):
    """Re-commit slot from flash: open for write, echo chunks via 0x08, finalize."""
    send(fd, b'\x02\x08\x0d\x01' + slot)
    recv(fd)
    send(fd, b'\x02\x08\x09\x01')
    recv(fd)
    for _ in range(num_chunks):
        send(fd, b'\x02\x08\x08\x01')
        recv(fd)
    send(fd, b'\x02\x08\x05\x01\x01')
    recv(fd)


def _parse_manifest(blob: bytes) -> dict:
    """Parse a profile manifest blob into its referenced sub-slot IDs."""
    if len(blob) < 22:
        return {}
    return {
        'preset_dir':  bytes(blob[6:8]),
        'vib_kv':      bytes(blob[8:10]),
        'action_hdr':  bytes(blob[10:12]),
        'vib_link':    bytes(blob[12:14]),
        'enable_map':  bytes(blob[14:16]),
        'global_slot': bytes(blob[20:22]),
        'raw':         blob,
    }


def _preset_sub_slots(preset_dir_blob: bytes) -> list[bytes]:
    """Extract preset-entry slot IDs from a preset-directory blob.
    Format: 4-byte header, then N × 8-byte entries [4b field][2b slot][2b field].
    """
    slots = []
    off = 4
    while off + 8 <= len(preset_dir_blob):
        slot = bytes(preset_dir_blob[off + 4: off + 6])
        if any(b != 0 for b in slot):
            slots.append(slot)
        off += 8
    return slots


def _write_profile_atomic(fd: int, main_slot: bytes, new_main_blob: bytes,
                          sub_blob_overrides: dict[bytes, bytes] | None = None):
    """
    Atomic multi-slot write:
      1. Write-unlock via Interface 0 (required or device returns status=9 on 06 01)
      2. Session init (0x02 0x3e)
      3. Lock all sub-slots via 0x0c
      4. Write every slot with 0x0b prefix; manifest last (no prefix)
      5. Read-back phase for all 3 profile manifests + action headers

    main_slot must be the actual slot bytes returned by list_profiles(), NOT
    a position index — list_profiles() skips empty slots so index != slot.
    """
    _send_write_unlock()

    manifest_slot = MAIN_TO_MANIFEST.get(main_slot)
    if manifest_slot is None:
        raise ValueError('Unknown main slot: %s' % main_slot.hex())

    manifest_blob = read_blob(fd, manifest_slot)
    m = _parse_manifest(manifest_blob)
    if not m:
        raise ValueError('Could not parse manifest for slot %s' % main_slot.hex())

    preset_dir_blob = read_blob(fd, m['preset_dir'])
    preset_slots    = _preset_sub_slots(preset_dir_blob)

    NULL_SLOT = b'\x00\x00'

    # Build the list of sub-slots that actually hold data (skip null slots).
    # global_slot == main_slot on this device — include it ONCE via main_slot.
    raw_sub = [
        m['action_hdr'], *preset_slots,
        m['preset_dir'], m['enable_map'], m['vib_link'], m['vib_kv'],
    ]
    # Include thumbstick preset (vib_header) + thumbstick preset table slots
    # for the active profile (0x806d / 0x826d). These hold stick curves +
    # deadzones. Firmware refuses to refresh dispatch without them.
    main_lo = main_slot[0]
    THUMB_OFFSETS = (0x20, 0x22)  # base + 0x20 = vib_header, +0x22 = thumb presets
    for off in THUMB_OFFSETS:
        candidate = bytes([main_lo + off, main_slot[1]])
        # Verify this slot is populated before adding to write list
        try:
            existing_data = read_blob(fd, candidate)
            if existing_data and any(b != 0 for b in existing_data[:8]):
                raw_sub.append(candidate)
        except Exception:
            pass
    seen = {main_slot}
    sub_slots: list[bytes] = []
    for s in raw_sub:
        if s != NULL_SLOT and s not in seen:
            seen.add(s)
            sub_slots.append(s)

    # Read current data for ALL slots BEFORE session init / locking.
    # _lock_slot (0x0c) erases the slot — reading after would return empty bytes.
    # Also needed to decide which slots actually require locking (non-empty only).
    main_blob_current = read_blob(fd, main_slot)
    sub_blobs = [read_blob(fd, s) for s in sub_slots]
    # Apply caller-provided sub-slot overrides (e.g. updated action_hdr).
    if sub_blob_overrides:
        for i, slot in enumerate(sub_slots):
            if slot in sub_blob_overrides:
                sub_blobs[i] = sub_blob_overrides[slot]

    # Flush any stale HID responses left in the kernel buffer from prior failed runs.
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    flushed = 0
    while True:
        try:
            stale = os.read(fd, 64)
            print(f'  flush: {stale[:8].hex()}')
            flushed += 1
        except BlockingIOError:
            break
    fcntl.fcntl(fd, fcntl.F_SETFL, fl)
    if flushed:
        print(f'  flushed {flushed} stale responses')

    send(fd, b'\x02\x08\x02\x3e')
    resp = recv(fd)
    print(f'  02 3e resp={resp[:8].hex()}')

    # Send the write-mode prep sequence (01 03 / 81 / 7d / de) ONCE per atomic
    # write, not per-slot. Call here before the lock phase so all subsequent
    # write_blob() invocations inherit the prepared state.
    _prep_write(fd)

    # Phase 1: lock (erase) all slots before writing.
    # Only lock slots we actually intend to write (have data). Empty sub-slots
    # (e.g. wiped P2) must not be locked — the device requires 0b unlock for
    # every slot that was 0c-locked, and we can't write empty slots with 0b.
    # Per-profile "special slots" (action header, compact manifest, pointer)
    # are dynamically allocated by device firmware — NOT a fixed formula.
    # P3 has been observed to use 80/81/82 6d in one save and 80/81 6d in
    # another save of the same profile. Slot selection must be driven by the
    # profile manifest (already done via sub_slots below).
    write_slots = [main_slot] + [s for s, b in zip(sub_slots, sub_blobs) if b]
    all_lock_slots = list(dict.fromkeys(write_slots))
    for slot in all_lock_slots:
        send(fd, b'\x02\x08\x0c' + slot)
        resp = recv(fd)
        s = resp[3] if len(resp) > 3 else 0xFF
        print(f'  0c {slot.hex()} status={s}')
        if s not in (0, 1):
            raise RuntimeError(f'_lock_slot: slot {slot.hex()} unexpected status={s}')

    # Phase 2: write all slots with 0b prefix.
    # main_slot (= global_slot) written once with new_main_blob.
    _write_slot(fd, main_slot, new_main_blob)
    for slot, blob in zip(sub_slots, sub_blobs):
        _write_slot(fd, slot, blob)
    write_blob(fd, manifest_slot, manifest_blob)  # manifest: no 0b prefix

    # Phase 3: read-back for all 3 profiles in fixed order P1→P2→P3.
    # Each cycle: readback manifest + readback first sub-slot, then commit
    # trigger. Without the sub-slot readback the device auto-reverts the main
    # profile blob ~4-5s after write.
    # First sub-slot per profile: P1 = 61 6d, P2 = 6b 6d, P3 = 75 6d. These
    # are the "preset-entry 1" slots (5-byte increment from main slot).
    first_subslots = [bytes.fromhex('616d'), bytes.fromhex('6b6d'), bytes.fromhex('756d')]
    for i in range(3):
        manifest_size = len(read_blob(fd, MANIFEST_SLOTS[i]))
        mchunks = max(1, -(-manifest_size // 60))
        _readback_slot(fd, MANIFEST_SLOTS[i], num_chunks=mchunks)

        sub_size = len(read_blob(fd, first_subslots[i]))
        if sub_size > 0:
            schunks = max(1, -(-sub_size // 60))
            _readback_slot(fd, first_subslots[i], num_chunks=schunks)

        send(fd, b'\x02\x08\x0d\x01\x25\x00')
        recv(fd)

    # Final 4th commit trigger at the very end of the session
    # (WritingTarget=0 behavior). The 3-profile loop above covers the per-
    # profile commits.
    send(fd, b'\x02\x08\x0d\x01\x25\x00')
    recv(fd)


def cmd_dump_slot(fd: int, slot_hex: str):
    slot = bytes.fromhex(slot_hex)
    blob = read_blob(fd, slot)
    if not blob:
        print(f'slot {slot_hex}: empty (0 bytes)')
        return
    # Try to find zlib magic for header boundary
    zlib_off = next(
        (i for i in range(min(32, len(blob) - 1))
         if blob[i] == 0x78 and blob[i + 1] == 0xda),
        None
    )
    print(f'slot {slot_hex}: {len(blob)} bytes  zlib_off={zlib_off}')
    print(f'  header bytes (0-{(zlib_off or 8) - 1}): {blob[:(zlib_off or 8)].hex(" ")}')
    if zlib_off and zlib_off >= 7:
        xml_len = (blob[4] << 16) | (blob[5] << 8) | blob[6]
        print(f'  declared xml_len (bytes 4-6 BE): {xml_len}')
    print(f'  first 32 bytes: {blob[:32].hex(" ")}')


def cmd_restore_xml(fd: int, slot_hex: str, xml_file: str, header_from: str | None):
    """
    Restore a main-profile slot from a saved XML dump.
    xml_file may be:
      - A dump-xml output file (header line + XML on line 2)
      - A plain XML file
    header_from: slot hex to borrow blob header bytes from (e.g. '606d').
    If omitted, tries to read header from the target slot; if empty, reads from P1 (606d).
    """
    target_slot = bytes.fromhex(slot_hex)

    # Load XML
    with open(xml_file, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    # Support dump-xml format: line 0 = '--- Profile ...' header, line 1 = XML
    if lines and lines[0].startswith('---') and len(lines) >= 2:
        xml_str = lines[1].strip()
    else:
        xml_str = '\n'.join(lines).strip()

    xml_bytes = _strip_xml_whitespace(xml_str.encode('utf-8'))

    # Build blob header.
    # Bytes 0-3: magic/version constant 0b b1 02 00.
    # Bytes 4-6: 24-bit BE uncompressed XML length.
    # Byte 7+: zlib compressed XML (0x78 0xda magic).
    ZLIB_OFF = 7  # original blob has 7-byte header
    BLOB_MAGIC = bytes.fromhex('0bb10200')  # magic constant for XML profile blobs
    header = bytearray(ZLIB_OFF)
    header[0:4] = BLOB_MAGIC

    if header_from:
        ref_blob = read_blob(fd, bytes.fromhex(header_from))
        ref_zlib_off = next(
            (i for i in range(min(32, len(ref_blob) - 1))
             if len(ref_blob) > i + 1 and ref_blob[i] == 0x78 and ref_blob[i + 1] == 0xda),
            None
        )
        if ref_zlib_off and ref_zlib_off >= 4:
            header[0:4] = ref_blob[0:4]
            print(f'  Borrowed header bytes 0-3 from slot {header_from}: '
                  f'{bytes(header[0:4]).hex(" ")}')
        else:
            print(f'  Note: slot {header_from} no zlib; using default magic {BLOB_MAGIC.hex(" ")}')

    xml_len = len(xml_bytes)
    header[4] = (xml_len >> 16) & 0xFF
    header[5] = (xml_len >> 8) & 0xFF
    header[6] = xml_len & 0xFF
    new_blob = bytes(header) + zlib.compress(xml_bytes, level=9)

    print(f'Restoring slot {slot_hex}: xml={xml_len}B  blob={len(new_blob)}B  '
          f'zlib_off={ZLIB_OFF}')
    print(f'  Header bytes: {bytes(header).hex(" ")}')

    _write_profile_atomic(fd, target_slot, new_blob)
    print(f'Done. Verify with: python3 scuf_config.py dump-slot {slot_hex}')


def cmd_show_vibration(fd: int, profile_idx: int | None, as_json: bool = False):
    indices = list(range(len(MAIN_SLOTS))) if profile_idx is None else [profile_idx]
    results = []
    for idx in indices:
        slot = vib_slot(idx)
        blob = read_blob(fd, slot)
        vib  = parse_vib_blob(blob)
        results.append({'profile_number': idx + 1,
                         'left': vib.get('left', 0), 'right': vib.get('right', 0)})
    if as_json:
        print(json.dumps(results))
    else:
        for r in results:
            print(f"Profile {r['profile_number']}: left={r.get('left', '?')}%  right={r.get('right', '?')}%")


def cmd_set_vibration(fd: int, profile_idx: int | None,
                      left: int | None, right: int | None):
    indices = list(range(len(MAIN_SLOTS))) if profile_idx is None else [profile_idx]
    for idx in indices:
        slot = vib_slot(idx)
        blob = read_blob(fd, slot)
        cur  = parse_vib_blob(blob)
        new_left  = left  if left  is not None else cur.get('left',  0)
        new_right = right if right is not None else cur.get('right', 0)
        new_blob  = set_vib_values(blob, new_left, new_right)
        write_vib_blob(fd, slot, new_blob)
        print(f'Profile {idx + 1}: vibration left={new_left}%  right={new_right}%')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='SCUF Envision Pro config tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--hidraw', help='hidraw device path (auto-detected if omitted)')
    parser.add_argument('--profile', type=int, metavar='N',
                        help='physical profile number (1-based, default: 1)')
    parser.add_argument('--json', action='store_true', help='Output as JSON (show only)')

    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('show', help='Show current button mappings')

    remap_p = sub.add_parser('remap', help='Remap a source button to a target')
    remap_p.add_argument('source', help='P1 P2 P3 P4 S1 S2')
    remap_p.add_argument('target', help='A B X Y LB RB LT RT L3 R3 Up Down Left Right')

    unmap_p = sub.add_parser('unmap', help='Clear a source button mapping')
    unmap_p.add_argument('source', help='P1 P2 P3 P4 S1 S2')

    sub.add_parser('show-presets', help='Show thumbstick sensitivity presets')

    sp = sub.add_parser('set-preset', help='Update a thumbstick sensitivity preset')
    sp.add_argument('preset_name', metavar='name',
                    help='Preset name exactly as shown in show-presets (e.g. "HW Thumbstick Preset 1")')
    sp.add_argument('--left-curve',   metavar='TYPE',
                    choices=CURVE_NAMES, help='Left curve: dynamic linear exponential aggressive custom')
    sp.add_argument('--right-curve',  metavar='TYPE',
                    choices=CURVE_NAMES, help='Right curve')
    sp.add_argument('--left-dz',      metavar='PCT', type=int, help='Left min deadzone %%')
    sp.add_argument('--left-max-dz',  metavar='PCT', type=int, help='Left max deadzone %%')
    sp.add_argument('--right-dz',     metavar='PCT', type=int, help='Right min deadzone %%')
    sp.add_argument('--right-max-dz', metavar='PCT', type=int, help='Right max deadzone %%')

    sub.add_parser('show-trigger-presets', help='Show trigger sensitivity presets')

    tp = sub.add_parser('set-trigger-preset', help='Update a trigger sensitivity preset')
    tp.add_argument('preset_name', metavar='name',
                    help='Preset name exactly as shown in show-trigger-presets')
    tp.add_argument('--left-curve',   metavar='TYPE',
                    choices=CURVE_NAMES, help='Left trigger curve: dynamic linear exponential aggressive custom')
    tp.add_argument('--right-curve',  metavar='TYPE',
                    choices=CURVE_NAMES, help='Right trigger curve')
    tp.add_argument('--left-dz',      metavar='PCT', type=int, help='Left trigger min deadzone %%')
    tp.add_argument('--left-max-dz',  metavar='PCT', type=int, help='Left trigger max deadzone %%')
    tp.add_argument('--right-dz',     metavar='PCT', type=int, help='Right trigger min deadzone %%')
    tp.add_argument('--right-max-dz', metavar='PCT', type=int, help='Right trigger max deadzone %%')

    bp = sub.add_parser('brightness', help='Set LED brightness (0-100%%)')
    bp.add_argument('percent', type=int, metavar='PCT', help='Brightness 0-100')

    ep = sub.add_parser('eco-mode', help='Enable or disable eco mode')
    ep.add_argument('state', choices=['on', 'off'], help='on or off')

    asp = sub.add_parser('auto-shutoff', help='Enable or disable auto-shutoff')
    asp.add_argument('state', choices=['on', 'off'], help='on or off')
    asp.add_argument('--minutes', type=int, metavar='N',
                     help='Idle minutes before shutoff (required when on)')

    sub.add_parser('dump-xml', help='Dump raw decompressed profile XML (debug)')

    dsp = sub.add_parser('dump-slot', help='Read raw bytes from a device slot (debug)')
    dsp.add_argument('slot_hex', metavar='SLOT', help='Slot address as 4 hex chars, e.g. 606d')

    rp = sub.add_parser('restore-xml', help='Restore a profile main slot from a saved XML dump file')
    rp.add_argument('slot_hex', metavar='SLOT', help='Target slot address, e.g. 6a6d')
    rp.add_argument('xml_file', metavar='FILE', help='Path to file containing XML on line 2 (dump-xml format) or plain XML')
    rp.add_argument('--header-from', metavar='SLOT', default=None,
                    help='Copy 7-byte blob header from this slot (e.g. 606d). '
                         'Default: read from target slot if non-empty, else auto-detect.')

    bsp = sub.add_parser('bootstrap-p1', help='Bootstrap Profile 1 from captured bundle (factory-reset recovery)')
    bsp.add_argument('source', metavar='SOURCE',
                     help='Source paddle: P1 P2 P3 P4 S1 S2')
    bsp.add_argument('target', metavar='TARGET',
                     help='Target button (A B X Y LB RB)')

    sub.add_parser('bootstrap-p1-raw',
                   help='Write P1 bundle VERBATIM (no modification)')

    sub.add_parser('bootstrap-p1-replay',
                   help='Replay FULL 510-command sequence byte-for-byte.')

    sub.add_parser('dispatch-refresh',
                   help='Replay p1_X_to_X (180 commands) — minimal resave sequence that '
                        'refreshes dispatch RAM without requiring power-cycle.')

    sub.add_parser('usb-reset',
                   help='Issue USB bus reset on controller (equivalent to unplug/replug). '
                        'Forces dispatch RAM to refresh from NVRAM after a profile write.')

    sub.add_parser('show-vibration', help='Show per-profile vibration intensities')

    vsp = sub.add_parser('set-vibration', help='Set per-profile vibration intensities')
    vsp.add_argument('--left',  type=int, metavar='PCT', help='Left motor intensity 0-100')
    vsp.add_argument('--right', type=int, metavar='PCT', help='Right motor intensity 0-100')

    args = parser.parse_args()

    # Opt-in apply-transition dance: 4x SetConfiguration(1) before opening
    # hidraw. Set env var SCUF_APPLY_DANCE=1 to enable for the 'remap' command.
    if args.cmd == 'remap' and os.environ.get('SCUF_APPLY_DANCE') == '1':
        _pre_open_setconfig_dance()
    # Pre-open HID class init (mimic Windows hid.dll CreateFile auto-init).
    # Linux hidraw skips SET_IDLE / SET_PROTOCOL / GET_REPORT_DESCRIPTOR;
    # firmware may use them as a 'Windows HID host attached' signal.
    if args.cmd == 'remap' and os.environ.get('SCUF_HID_CLASS_INIT') == '1':
        _hid_class_init(intf=4)
        import time as _t
        # pyusb's ctrl_transfer auto-claimed interface 4 + released, but kernel
        # usbhid doesn't auto-rebind. Force re-bind via sysfs.
        intf_id = '1-5.1:1.4'  # SCUF config interface (adjust if different)
        bind_path = '/sys/bus/usb/drivers/usbhid/bind'
        try:
            with open(bind_path, 'w') as f:
                f.write(intf_id)
            print(f'  bound usbhid → {intf_id}')
        except (PermissionError, FileNotFoundError, OSError) as e:
            print(f'  usbhid rebind failed (need root or already bound): {e}')
        for _ in range(50):
            if find_config_hidraw():
                break
            _t.sleep(0.1)
        _t.sleep(0.5)
    if args.cmd == 'remap' and os.environ.get('SCUF_PIPE_RESET') == '1':
        _pipe_toggle_reset([0x02, 0x82])

    hidraw = args.hidraw or find_config_hidraw()
    if hidraw is None:
        print('SCUF config hidraw device not found. Use --hidraw /dev/hidrawX.')
        sys.exit(1)

    try:
        fd = os.open(hidraw, os.O_RDWR)
    except OSError as e:
        print(f'Cannot open {hidraw}: {e}')
        print('Try: sudo python3 scuf_config.py ...')
        sys.exit(1)

    try:
        if args.cmd == 'show':
            cmd_show(fd, args.profile, as_json=args.json)
        elif args.cmd == 'remap':
            cmd_remap(fd, args.source, args.target, args.profile)
        elif args.cmd == 'unmap':
            cmd_unmap(fd, args.source, args.profile)
        elif args.cmd == 'show-presets':
            cmd_show_presets(fd, args.profile, as_json=args.json, input_device=0)
        elif args.cmd == 'set-preset':
            cmd_set_preset(
                fd, args.preset_name, args.profile,
                left_curve=CURVE_NAMES[args.left_curve]   if args.left_curve   else None,
                right_curve=CURVE_NAMES[args.right_curve] if args.right_curve  else None,
                left_dz=args.left_dz,
                left_max_dz=args.left_max_dz,
                right_dz=args.right_dz,
                right_max_dz=args.right_max_dz,
                input_device=0,
            )
        elif args.cmd == 'show-trigger-presets':
            cmd_show_presets(fd, args.profile, as_json=args.json, input_device=1)
        elif args.cmd == 'set-trigger-preset':
            cmd_set_preset(
                fd, args.preset_name, args.profile,
                left_curve=CURVE_NAMES[args.left_curve]   if args.left_curve   else None,
                right_curve=CURVE_NAMES[args.right_curve] if args.right_curve  else None,
                left_dz=args.left_dz,
                left_max_dz=args.left_max_dz,
                right_dz=args.right_dz,
                right_max_dz=args.right_max_dz,
                input_device=1,
            )
        elif args.cmd == 'brightness':
            cmd_set_brightness(fd, args.percent)
        elif args.cmd == 'eco-mode':
            cmd_set_eco_mode(fd, args.state == 'on')
        elif args.cmd == 'auto-shutoff':
            cmd_set_auto_shutoff(fd, args.state == 'on', minutes=args.minutes)
        elif args.cmd == 'dump-xml':
            profiles = list_profiles(fd)
            targets = ([p for p in profiles if p[0] == args.profile - 1]
                       if args.profile else profiles)
            for pidx, slot, xml, blob, zlib_off in targets:
                if not xml:
                    print(f'--- Profile {pidx + 1}  slot={slot.hex()}  (no XML blob — default profile) ---')
                    continue
                compressed_len = len(blob) - zlib_off
                uncompressed_len = len(xml)
                zlib_data = blob[zlib_off:]
                trailing = len(zlib_data) - len(zlib_data.rstrip(b'\x00'))
                print(f'--- Profile {pidx + 1}  slot={slot.hex()}  '
                      f'blob={len(blob)}B  header={zlib_off}B  '
                      f'zlib={compressed_len}B  xml={uncompressed_len}B  '
                      f'trailing-pad={trailing}B ---')
                print(xml.decode('utf-8', errors='replace'))
        elif args.cmd == 'dump-slot':
            cmd_dump_slot(fd, args.slot_hex)
        elif args.cmd == 'restore-xml':
            cmd_restore_xml(fd, args.slot_hex, args.xml_file, args.header_from)
        elif args.cmd == 'bootstrap-p1':
            src_key = SOURCE_KEYS.get(args.source.upper(), args.source)
            tgt_kn = TARGET_KEYS.get(args.target.upper(), args.target)
            if src_key not in SOURCE_KEYS.values():
                print(f'Unknown source: {args.source!r}', file=sys.stderr)
                sys.exit(1)
            bundle = build_p1_bootstrap_bundle(src_key, tgt_kn)
            print(f'Bootstrapping P1 with {src_key} → {tgt_kn}')
            print(f'  Bundle: {len(bundle)} slots, sizes={[len(v) for v in bundle.values()]}')
            _write_bootstrap_bundle(fd, bundle)
            print(f'Bootstrap complete. Verify with: python3 scuf_config.py show')
        elif args.cmd == 'bootstrap-p1-raw':
            # Write the captured p1_to_A bundle VERBATIM without any mutation.
            # If this works, our recompression/byte-13-swap is the bug.
            # If it still bricks, timing or order is wrong.
            bundle = _load_p1_bootstrap_bundle()
            print(f'Writing P1 bundle VERBATIM (no modifications)')
            print(f'  Bundle: {len(bundle)} slots, sizes={[len(v) for v in bundle.values()]}')
            _write_bootstrap_bundle(fd, bundle)
            print(f'Raw bootstrap complete. Verify with: python3 scuf_config.py show')
        elif args.cmd == 'bootstrap-p1-replay':
            # Full 510-command sequence replay byte-for-byte.
            print(f'Replaying full p1_to_A sequence (510 commands)')
            _replay_p1_sequence(fd)
            print(f'Replay complete. Verify with: python3 scuf_config.py show')
        elif args.cmd == 'usb-reset':
            _notify_restart_required()
        elif args.cmd == 'dispatch-refresh':
            # Replay a minimal resave sequence that refreshes dispatch RAM
            # without requiring a controller power-cycle.
            path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'templates', 'p1_bootstrap', 'sequence_p1_X_to_X.bin',
            )
            import fcntl, time as _time
            with open(path, 'rb') as f:
                count = struct.unpack('<I', f.read(4))[0]
                seq = []
                for _ in range(count):
                    d = struct.unpack('<I', f.read(4))[0]
                    ln = struct.unpack('<H', f.read(2))[0]
                    seq.append((d, f.read(ln)))
            print(f'Dispatch-refresh replay: {len(seq)} commands')
            _send_write_unlock()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            try:
                for delay_us, pkt in seq:
                    if delay_us > 0:
                        _time.sleep(delay_us / 1_000_000.0)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl)
                    send(fd, pkt)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                    deadline = _time.monotonic() + 0.10
                    while _time.monotonic() < deadline:
                        try:
                            os.read(fd, 64)
                            break
                        except BlockingIOError:
                            _time.sleep(0.001)
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            print(f'Done. Test paddle press WITHOUT power-cycle.')
        elif args.cmd == 'show-vibration':
            cmd_show_vibration(fd, args.profile, as_json=args.json)
        elif args.cmd == 'set-vibration':
            cmd_set_vibration(fd, args.profile, left=args.left, right=args.right)
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
