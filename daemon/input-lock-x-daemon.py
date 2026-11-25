#!/usr/bin/python3
# Copyright (C) 2026 Isak Lindgren
# SPDX-License-Identifier: GPL-2.0-or-later
# PIN unlock, device recovery, and watchdog support by ayushxaw, 2026-11-25.
"""Input Lock X daemon.

Takes an exclusive EVIOCGRAB on the internal keyboard and pointers so that no
input from them reaches the compositor or any application. Because the grab is
exclusive to this process, this daemon is the only thing that can still see the
keyboard -- so it handles Ctrl+Esc PIN entry and Ctrl+A+Esc recovery locally.

A grab lives and dies with the file descriptor: if this process is killed the
kernel releases it automatically. That is the failsafe, and the reason this is
preferred over /sys/class/input/inputN/inhibited (which persists).

Runs as root via input-lock-x.service. Stdlib only.
"""

import argparse
import configparser
import ctypes
import getpass
import hashlib
import hmac
import errno
import fcntl
import glob
import os
import select
import signal
import stat
import socket
import struct
import sys
import tempfile
import time
import warnings
from fnmatch import fnmatch

CONFIG_PATH = "/etc/input-lock-x.conf"
STATE_PATH = "/run/input-lock-x.state"
AUTH_PATH = "/run/input-lock-x.auth"
PIN_PATH = "/etc/input-lock-x.pin"
PIN_IDLE_SECONDS = 30
PIN_RETRY_SECONDS = 1
PIN_ITERATIONS = 600_000
PIN_SALT_BYTES = 16
PIN_HASH_BYTES = 32
MAX_PIN_DIGITS = 12
INPUT_GLOB = "/dev/input/event*"

# struct input_event: struct timeval (2x long) + type + code + value. Native
# sizes, so this is correct on 32- and 64-bit alike.
EVENT_STRUCT = struct.Struct("llHHi")
EVENT_SIZE = EVENT_STRUCT.size

EV_SYN = 0x00
SYN_REPORT = 0
SYN_DROPPED = 3
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

# Bus types from <linux/input.h>. This is how auto mode tells an internal
# keyboard from one the user plugged in -- an external keyboard is both
# something they probably want to keep using and one of the ways back.
BUS_USB = 0x03
BUS_VIRTUAL = 0x06
BUS_BLUETOOTH = 0x05
BUS_NAMES = {
    0x01: "pci", 0x03: "usb", 0x05: "bluetooth", 0x06: "virtual",
    0x10: "isa", 0x11: "i8042", 0x18: "i2c", 0x19: "host",
    0x1C: "spi", 0x1D: "rmi",
}

# INPUT_PROP_* from <linux/input-event-codes.h>.
INPUT_PROP_POINTER = 0x00
INPUT_PROP_DIRECT = 0x01           # touchscreen: coordinates map to the screen
INPUT_PROP_BUTTONPAD = 0x02
INPUT_PROP_POINTING_STICK = 0x05
INPUT_PROP_BYTES = 4               # covers INPUT_PROP_MAX (0x1f)
PROP_NAMES = {
    INPUT_PROP_POINTER: "pointer",
    INPUT_PROP_DIRECT: "direct",
    INPUT_PROP_BUTTONPAD: "buttonpad",
    0x03: "semi_mt",
    0x04: "topbuttonpad",
    INPUT_PROP_POINTING_STICK: "pointing_stick",
    0x06: "accelerometer",
}

REL_X = 0x00
REL_Y = 0x01
REL_BYTES = 2

ABS_MT_POSITION_X = 0x35
ABS_BYTES = 8                      # covers ABS_MAX (0x3f)

BTN_LEFT = 0x110
BTN_TOOL_FINGER = 0x145

KEY_ESC = 1
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_RIGHTSHIFT = 54
KEY_LEFTALT = 56
KEY_RIGHTCTRL = 97
KEY_RIGHTALT = 100
KEY_LEFTMETA = 125
KEY_RIGHTMETA = 126

# Probed against the *capability* bitmap to recognise a keyboard by what it can
# emit rather than by what it is called.
KEY_1 = 2
KEY_ENTER = 28
KEY_A = 30
KEY_Z = 44
KEY_SPACE = 57
KEYBOARD_PROBE_KEYS = (KEY_1, KEY_ENTER, KEY_A, KEY_Z, KEY_SPACE)

CTRL_KEYS = frozenset((KEY_LEFTCTRL, KEY_RIGHTCTRL))
KEY_BACKSPACE = 14
KEY_KPENTER = 96

# Linux evdev codes are physical key positions, independent of Num Lock.
DIGIT_KEYS = dict(zip(range(2, 12), "1234567890"))
DIGIT_KEYS.update(zip((82, 79, 80, 81, 75, 76, 77, 71, 72, 73), "0123456789"))

# Keys whose held state actually matters when we grab or ungrab: the modifiers,
# which the compositor latches, plus the Esc of the Ctrl+Esc that got us here.
#
# Waiting on *every* key would be wrong for this hardware. Folded into tablet
# mode the keyboard lies face down, so ordinary keys are held constantly --
# which is the entire reason this tool exists. Waiting for those to come up
# would time out on every single lock and quietly forfeit the protection below.
STICKY_KEYS = (
    KEY_ESC,
    KEY_LEFTCTRL, KEY_RIGHTCTRL,
    KEY_LEFTSHIFT, KEY_RIGHTSHIFT,
    KEY_LEFTALT, KEY_RIGHTALT,
    KEY_LEFTMETA, KEY_RIGHTMETA,
)

CHORD_KEYS = frozenset(STICKY_KEYS) | {KEY_A}
OTHER_MODIFIERS = frozenset(STICKY_KEYS) - CTRL_KEYS - {KEY_ESC}

# Covers key codes 0..255, i.e. the KEY_* range. BTN_* codes start at 0x100, so
# this also keeps BTN_TOUCH and BTN_LEFT out of the picture.
KEY_BITMAP_BYTES = 32

# Capability probing has to reach BTN_TOOL_FINGER at 0x145, so unlike the
# pressed-key check above it needs the whole KEY_MAX (0x2ff) range.
KEY_CAP_BYTES = 96

# _drain() outcomes.
DRAIN_OK = "ok"
DRAIN_DEAD = "dead"
DRAIN_RELEASE = "release"
DRAIN_FORCE_RELEASE = "force-release"
DEVICE_FAULT = select.POLLERR | select.POLLHUP | select.POLLNVAL
SCAN_SECONDS = 1

# _IOC encoding, from <asm-generic/ioctl.h>
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, typ, nr, size):
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(typ) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def EVIOCGNAME(length):
    return _IOC(_IOC_READ, "E", 0x06, length)


def EVIOCGKEY(length):
    return _IOC(_IOC_READ, "E", 0x18, length)


def EVIOCGPROP(length):
    return _IOC(_IOC_READ, "E", 0x09, length)


def EVIOCGBIT(ev, length):
    return _IOC(_IOC_READ, "E", 0x20 + ev, length)


EVIOCGID = _IOC(_IOC_READ, "E", 0x02, 8)


EVIOCGRAB = _IOC(_IOC_WRITE, "E", 0x90, ctypes.sizeof(ctypes.c_int))


def log(msg):
    """Log to stderr, which systemd routes to the journal."""
    print(msg, file=sys.stderr, flush=True)


def sd_notify(state):
    """Send a readiness message to systemd (Type=notify).

    Lets `systemctl start` -- and therefore the extension's StartUnit call and
    `input-lock-x on` -- fail honestly when the grab never happened, instead of
    reporting success at fork time and leaving the user with a lock they think
    is engaged.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):  # abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as exc:
        log(f"sd_notify({state!r}) failed: {exc}")


def watchdog_interval():
    """Seconds between systemd watchdog notifications, or None."""
    try:
        target = int(os.environ.get("WATCHDOG_PID") or os.getpid())
        period = int(os.environ.get("WATCHDOG_USEC", "0"))
        if target == os.getpid() and period > 0:
            return period / 2_000_000
    except (ValueError, OverflowError):
        pass
    return None


def device_name(fd):
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(len(buf)), buf)
    except OSError:
        return None
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


MODE_AUTO = "auto"
MODE_PATTERNS = "patterns"

CLASS_KEYBOARD = "keyboard"
CLASS_POINTER = "pointer"
CLASS_TOUCHSCREEN = "touchscreen"
CLASS_OTHER = "other"


class Config:
    """Parsed input-lock-x.conf."""

    def __init__(self, mode, patterns, exclude, include_usb, include_bluetooth,
                 max_lock_seconds):
        self.mode = mode
        self.patterns = patterns
        self.exclude = exclude
        self.include_usb = include_usb
        self.include_bluetooth = include_bluetooth
        self.max_lock_seconds = max_lock_seconds


def _lines(parser, section, option):
    raw = parser.get(section, option, fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _scalar(parser, section, option):
    """Read a single-value option, dropping any trailing `# ...` comment.

    ConfigParser is built without inline_comment_prefixes on purpose: a
    pattern is an evdev device name, an arbitrary vendor string that may
    legitimately contain a '#', and silently truncating one would grab the
    wrong device. But mode/include_usb/max_lock_seconds are keywords, numbers
    and booleans -- a '#' in those is always a comment, and treating it as
    part of the value made an annotated `max_lock_seconds = 60  # ...` fall
    back to 0, quietly disabling the auto-release safety net.
    """
    raw = parser.get(section, option, fallback="")
    for prefix in ("#", ";"):
        cut = raw.find(prefix)
        if cut != -1:
            raw = raw[:cut]
    return raw.strip()


BOOLEAN_WORDS = {
    "1": True, "yes": True, "true": True, "on": True,
    "0": False, "no": False, "false": False, "off": False,
}


def _flag(parser, section, option):
    raw = _scalar(parser, section, option)
    if not raw:
        return False
    value = BOOLEAN_WORDS.get(raw.lower())
    if value is None:
        log(f"{option}={raw!r} is not a yes/no value, using no")
        return False
    return value


def read_config(config_path):
    """Read the config, falling back to auto detection on anything unusable.

    Compatibility rule: a file that sets `patterns` but not `mode` is patterns
    mode. That is what every config written before auto detection existed looks
    like, and silently moving those machines onto a different set of devices
    would be the rudest possible upgrade.
    """
    # interpolation=None: device names are arbitrary strings and a bare '%' in
    # one would otherwise raise InterpolationSyntaxError out of get(), i.e.
    # outside the fallback handling below.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        # open(), not ConfigParser.read(): read() catches OSError per file and
        # moves on, so an unreadable config -- an SELinux denial, mode 000, a
        # directory left in its place -- arrived here as the same empty result
        # as "no config at all" and silently moved a patterns-mode machine onto
        # auto detection. Opening it ourselves keeps absent and unreadable
        # distinct, and only one of the two is allowed to be quiet.
        try:
            with open(config_path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            log(f"config {config_path} not found, using auto detection")
            text = ""
        parser.read_string(text, source=str(config_path))
    # UnicodeDecodeError is a ValueError, not a configparser.Error, so a
    # config saved in a single-byte encoding used to escape this handler and
    # kill the daemon with a traceback before it logged anything at all.
    except (configparser.Error, UnicodeDecodeError, OSError) as exc:
        log(f"config {config_path} is unreadable ({exc}), using auto detection")
        # Reset: read_string can fail partway and leave half a config behind.
        parser = configparser.ConfigParser(interpolation=None)

    patterns = _lines(parser, "devices", "patterns")
    exclude = _lines(parser, "devices", "exclude")

    mode = _scalar(parser, "devices", "mode").lower()
    if not mode:
        mode = MODE_PATTERNS if patterns else MODE_AUTO
    elif mode not in (MODE_AUTO, MODE_PATTERNS):
        log(f"mode={mode!r} is neither 'auto' nor 'patterns', using auto")
        mode = MODE_AUTO

    if mode == MODE_PATTERNS and not patterns:
        log("mode=patterns but no patterns are listed, using auto detection")
        mode = MODE_AUTO

    # The mirror case, and the easier one to walk into: the shipped config sets
    # mode = auto, so someone who only uncomments `patterns` gets auto
    # detection and no sign that the lines they added do nothing. Warn, but
    # leave the mode alone -- unlike patterns-with-no-patterns above, auto is a
    # working configuration, and overriding an explicit setting is worse than
    # saying what it means.
    if mode == MODE_AUTO and patterns:
        log("mode=auto, so the patterns in the config are ignored; "
            "set mode=patterns to use them")

    raw_max = _scalar(parser, "behaviour", "max_lock_seconds")
    try:
        max_lock = int(raw_max) if raw_max else 0
    except ValueError:
        log(f"max_lock_seconds={raw_max!r} is not an integer, using 0")
        max_lock = 0
    # int("-60") parses fine, and a negative deadline is silently treated as
    # "no limit" below -- indistinguishable from the 0 default. Someone who
    # means "off" gets what they wanted either way; someone who typed -60 for
    # 60 gets an unbounded lock and no hint. Say so.
    if max_lock < 0:
        log(f"max_lock_seconds={max_lock} is negative, using 0 (no limit)")
        max_lock = 0

    return Config(
        mode,
        patterns,
        exclude,
        _flag(parser, "devices", "include_usb"),
        _flag(parser, "devices", "include_bluetooth"),
        max_lock,
    )


def read_bitmap(fd, request, nbytes):
    """Read one evdev bitmap ioctl, or None if the device will not answer."""
    buf = bytearray(nbytes)
    try:
        fcntl.ioctl(fd, request, buf)
    except OSError:
        return None
    return buf


def bit_is_set(bitmap, code):
    index = code // 8
    return (bitmap is not None and index < len(bitmap)
            and bool(bitmap[index] & (1 << (code % 8))))


class DeviceInfo:
    """What the kernel says a device *is*, which is the part that generalises.

    Device names do not: they are whatever the vendor felt like, so the same
    touchpad is "SynPS/2 Synaptics TouchPad" on one machine and
    "MSFT0001:00 06CB:CE2D Touchpad" on the next. Capabilities and properties
    are the same everywhere, which is what makes auto mode work on hardware
    nobody has tested.
    """

    def __init__(self, path, fd, name):
        self.path = path
        self.fd = fd
        self.name = name

        # struct input_id is four __u16; the first is the bus type.
        ident = read_bitmap(fd, EVIOCGID, 8)
        self.bustype = struct.unpack_from("H", ident)[0] if ident else 0

        self.props = read_bitmap(fd, EVIOCGPROP(INPUT_PROP_BYTES), INPUT_PROP_BYTES)
        self.keys = read_bitmap(fd, EVIOCGBIT(EV_KEY, KEY_CAP_BYTES), KEY_CAP_BYTES)
        self.rels = read_bitmap(fd, EVIOCGBIT(EV_REL, REL_BYTES), REL_BYTES)
        self.abss = read_bitmap(fd, EVIOCGBIT(EV_ABS, ABS_BYTES), ABS_BYTES)
        self.kind = self._classify()

    def has_prop(self, prop):
        return bit_is_set(self.props, prop)

    def _classify(self):
        # Order matters. A touchscreen also reports keys (BTN_TOUCH) and
        # absolute axes, so INPUT_PROP_DIRECT has to be checked before anything
        # else gets the chance to call it a pointer and grab it.
        if self.has_prop(INPUT_PROP_DIRECT):
            return CLASS_TOUCHSCREEN

        has_pointer_prop = (self.has_prop(INPUT_PROP_POINTER)
                            or self.has_prop(INPUT_PROP_BUTTONPAD)
                            or self.has_prop(INPUT_PROP_POINTING_STICK))
        multitouch = (bit_is_set(self.abss, ABS_MT_POSITION_X)
                      and bit_is_set(self.keys, BTN_TOOL_FINGER))

        # A panel driven through a generic HID path can report multitouch
        # without ever setting INPUT_PROP_DIRECT, and would then fall through
        # to the pointer branch and be grabbed -- silently destroying the
        # escape hatch in exactly the tablet-mode case this tool is for.
        # BTN_LEFT is the discriminator: a touchpad has a physical button (or
        # is a BUTTONPAD, which is one), a touchscreen has nothing to press.
        # Guessing "touchscreen" here is the safe direction to be wrong in --
        # it under-locks, which is visible, rather than locking the way back.
        if multitouch and not has_pointer_prop and not bit_is_set(self.keys, BTN_LEFT):
            return CLASS_TOUCHSCREEN

        if (has_pointer_prop
                or multitouch
                or (bit_is_set(self.rels, REL_X)
                    and bit_is_set(self.rels, REL_Y)
                    and bit_is_set(self.keys, BTN_LEFT))):
            return CLASS_POINTER

        # Every one of these, not any: the point is to separate a real keyboard
        # from the sparse-keymap devices every laptop has -- power button, lid
        # switch, "Intel HID events", the volume rocker -- which all advertise
        # EV_KEY with a handful of codes and would otherwise look like one.
        if all(bit_is_set(self.keys, code) for code in KEYBOARD_PROBE_KEYS):
            return CLASS_KEYBOARD

        return CLASS_OTHER

    @property
    def bus_name(self):
        return BUS_NAMES.get(self.bustype, f"0x{self.bustype:02x}")

    @property
    def prop_names(self):
        names = sorted(label for prop, label in PROP_NAMES.items()
                       if self.has_prop(prop))
        return ",".join(names) or "-"


def decide(info, config):
    """Should this device be grabbed? Returns (grab, reason).

    The reason is not decoration. On hardware nobody has tested, `--list` and
    the startup log are the only way a user finds out why their keyboard was or
    was not covered.
    """
    if any(fnmatch(info.name, pattern) for pattern in config.exclude):
        return False, "excluded by config"

    if config.mode == MODE_PATTERNS:
        if not any(fnmatch(info.name, pattern) for pattern in config.patterns):
            return False, "no pattern matches"
        if info.kind == CLASS_TOUCHSCREEN:
            # Not overridden: an explicit pattern is an explicit choice. But it
            # is the escape hatch, so it does not happen quietly.
            return True, "matches a pattern -- WARNING, this is a touchscreen"
        return True, "matches a pattern"

    if info.kind == CLASS_TOUCHSCREEN:
        # The one rule auto mode will not break. Tapping the panel icon is the
        # only way back that needs neither a keyboard nor a second machine.
        return False, "touchscreen -- kept live as the way back"
    if info.kind == CLASS_OTHER:
        return False, "not a keyboard or pointer"
    if info.bustype == BUS_VIRTUAL:
        # uinput: input-remapper, keyd, kmonad, Steam Input, on-screen
        # keyboards, remote-desktop agents. Grabbing one blocks the events
        # another tool is injecting -- including, on a remapper setup, the
        # keyboard the user actually types on. The physical device underneath
        # it is a separate node and is still grabbed on its own merits.
        return False, "virtual device -- created by software, not locked"
    if info.bustype == BUS_USB and not config.include_usb:
        return False, "USB -- set include_usb = yes to grab it"
    if info.bustype == BUS_BLUETOOTH and not config.include_bluetooth:
        return False, "Bluetooth -- set include_bluetooth = yes to grab it"
    return True, f"internal {info.kind}"


def device_identity(file_stat):
    return file_stat.st_dev, file_stat.st_ino


def can_recover(info):
    return (bit_is_set(info.keys, KEY_ESC) and bit_is_set(info.keys, KEY_A)
            and any(bit_is_set(info.keys, code) for code in CTRL_KEYS))


def open_device(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        name = device_name(fd)
        if name is not None:
            info = DeviceInfo(path, fd, name)
            info.identity = device_identity(os.fstat(fd))
            return info
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    return None


def inspect_devices(config):
    """Return device decisions with fds open, plus the permission-denied count."""
    decisions = []
    denied = 0
    for path in sorted(glob.glob(INPUT_GLOB)):
        try:
            info = open_device(path)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                denied += 1
            log(f"cannot open {path}: {exc}")
            continue

        if info is not None:
            grab, reason = decide(info, config)
            decisions.append((info, grab, reason))
    return decisions, denied


def find_devices(config):
    """The devices to grab, as [(path, name, fd)]. Everything else is closed.

    Also returns the full decision list, so the caller can report on what it is
    leaving live without opening every device a second time.
    """
    decisions, denied = inspect_devices(config)
    matched = []
    for info, grab, _reason in decisions:
        if grab:
            matched.append((info.path, info.name, info.fd))
        else:
            os.close(info.fd)
    return matched, denied, decisions


def escape_routes(decisions):
    """Report physical input devices that will remain usable during a grab."""
    # A uinput node has nothing physical behind it, so it is never a way back
    # in -- and auto mode leaves every one of them ungrabbed, which means
    # without this filter a machine running input-remapper or a remote-desktop
    # agent would be told it has a route it cannot touch. This applies to all
    # three kinds: virtual pointers and virtual touchscreens exist too.
    def live(kind):
        return any(i.kind == kind and not g and i.bustype != BUS_VIRTUAL
                   for i, g, _ in decisions)

    routes = []
    if live(CLASS_TOUCHSCREEN):
        routes.append("the touchscreen")
    if live(CLASS_POINTER):
        routes.append("an ungrabbed pointer")
    if live(CLASS_KEYBOARD):
        routes.append("an ungrabbed keyboard")
    return routes


def log_safety_notes(decisions):
    """Say what will still work once the grab is on -- before taking it."""
    for info, grab, _reason in decisions:
        if grab and info.kind == CLASS_TOUCHSCREEN:
            log(f"WARNING: {info.name} is a touchscreen and a pattern matched "
                "it; touch input will be disabled while locked")

    routes = escape_routes(decisions)
    if routes:
        log("live input during the lock: " + ", ".join(routes))
    else:
        log("WARNING: nothing usable stays live -- no touchscreen, pointer or "
            "spare keyboard")
        log("WARNING: the only ways back will be Ctrl+A+Esc and SSH from another "
            "machine")


def read_key_bitmap(fd):
    """Current pressed-key bitmap for a device, or None if it cannot be read."""
    buf = bytearray(KEY_BITMAP_BYTES)
    try:
        fcntl.ioctl(fd, EVIOCGKEY(len(buf)), buf)
    except OSError:
        return None
    return buf


def held_sticky_keys(fds, keys=STICKY_KEYS):
    """Which of the given keys are held down across the devices."""
    held = set()
    for fd in fds:
        bitmap = read_key_bitmap(fd)
        if bitmap is None:
            continue
        held.update(code for code in keys if bit_is_set(bitmap, code))
    return held


def wait_for_sticky_keys_released(fds, timeout=2.0, keys=STICKY_KEYS):
    """Block until the specified keys are up or the timeout elapses.

    This matters at both ends of a lock. We are normally invoked *from* the
    Ctrl+Esc binding, so both keys are still physically down and the compositor
    has already seen their press. Grabbing at that moment means the compositor
    never sees the release and is left with a stuck Ctrl modifier. Same in
    reverse when we ungrab.

    The wait checks modifiers and keys consumed by a shortcut or PIN
    submission, since unrelated held keys need not delay an unlock.
    """
    deadline = time.monotonic() + timeout
    while True:
        held = held_sticky_keys(fds, keys)
        if not held:
            return True
        if time.monotonic() >= deadline:
            log(f"timed out waiting for keys {sorted(held)} to be released, "
                f"continuing anyway")
            return False
        time.sleep(0.02)


def set_grab(fd, name, grab, quiet=False):
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1 if grab else 0)
        return True
    except OSError as exc:
        if not quiet:
            verb = "grab" if grab else "release"
            log(f"failed to {verb} {name}: {exc}")
        return False


def write_state():
    """Publish the locked state. Returns False if it could not be written."""
    try:
        with open(STATE_PATH, "w") as handle:
            handle.write("locked\n")
        return True
    except OSError as exc:
        log(f"could not write {STATE_PATH}: {exc}")
        return False


def clear_state():
    try:
        os.unlink(STATE_PATH)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            log(f"could not remove {STATE_PATH}: {exc}")


def atomic_write(path, content, mode, durable=False):
    """Replace a runtime/config file without exposing a partially written value."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                     dir=os.path.dirname(path), delete=False) as handle:
        temp_path = handle.name
        try:
            handle.write(content)
            os.fchmod(handle.fileno(), mode)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
            handle.close()
            os.replace(temp_path, path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def write_auth_state(state):
    try:
        atomic_write(AUTH_PATH, state + "\n", 0o644)
        return True
    except OSError as exc:
        log(f"could not write {AUTH_PATH}: {exc}")
        return False


def clear_auth_state():
    try:
        os.unlink(AUTH_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"could not remove {AUTH_PATH}: {exc}")


def valid_pin(pin):
    return 4 <= len(pin) <= MAX_PIN_DIGITS and pin.isascii() and pin.isdecimal()


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, PIN_ITERATIONS)


def read_pin(path=PIN_PATH, owner=0):
    """Return the salt and hash from a readable, private PIN file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="ascii") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != owner or info.st_mode & 0o077:
                return None
            parts = handle.read(192)
        if len(parts) == 192:
            return None
        parts = parts.split()
        if len(parts) != 3 or parts[0] != "pbkdf2-sha256":
            return None
        salt, digest = bytes.fromhex(parts[1]), bytes.fromhex(parts[2])
        if len(salt) == PIN_SALT_BYTES and len(digest) == PIN_HASH_BYTES:
            return salt, digest
    except (OSError, UnicodeError, ValueError):
        pass
    return None


def set_pin(path=PIN_PATH):
    if not sys.stdin.isatty():
        log("--set-pin requires an interactive terminal")
        return 1
    if os.geteuid() != 0:
        log("--set-pin must run as root")
        return 1

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            pin = getpass.getpass("New Input Lock X PIN (4–12 digits): ")
            if not valid_pin(pin):
                log("Choose a PIN with 4–12 ASCII digits")
                return 1
            if pin != getpass.getpass("Repeat Input Lock X PIN: "):
                log("The PINs did not match")
                return 1
        salt = os.urandom(PIN_SALT_BYTES)
        digest = hash_pin(pin, salt)
        atomic_write(path, f"pbkdf2-sha256 {salt.hex()} {digest.hex()}\n", 0o600, durable=True)
    except getpass.GetPassWarning:
        log("The terminal could not read a PIN without echoing it")
        return 1
    except (EOFError, KeyboardInterrupt):
        log("PIN setup cancelled")
        return 1
    except OSError as exc:
        log(f"could not save the PIN: {exc}")
        return 1

    print("Input Lock X PIN saved")
    return 0


def list_devices(config):
    """Print every device, the verdict on it, and why. Grabs nothing."""
    decisions, denied = inspect_devices(config)

    print(f"mode: {config.mode}")
    # Printed in both modes. --list is where someone goes to find out why
    # their patterns did nothing, so this is the last place to omit them.
    for pattern in config.patterns:
        inert = "" if config.mode == MODE_PATTERNS else "  (ignored: mode is auto)"
        print(f"  pattern: {pattern}{inert}")
    for pattern in config.exclude:
        print(f"  exclude: {pattern}")
    if config.include_usb:
        print("  include_usb: yes")
    if config.include_bluetooth:
        print("  include_bluetooth: yes")

    print("\nDevices:")
    for info, grab, reason in decisions:
        print(f"  {'GRAB' if grab else 'skip'}  {info.name}")
        print(f"        {info.path}  bus={info.bus_name}  "
              f"kind={info.kind}  props={info.prop_names}")
        print(f"        -> {reason}")
        os.close(info.fd)

    grabbed = sum(1 for _, grab, _ in decisions if grab)
    print(f"\n{grabbed} device(s) would be grabbed.")

    routes = escape_routes(decisions)
    if routes:
        print("Live input during the lock: " + ", ".join(routes))
    else:
        print("WARNING: nothing usable would stay live. The only ways back "
              "would be\n         Ctrl+A+Esc and SSH from another machine.")

    if denied:
        print(f"\n{denied} device(s) could not be opened -- run this as root.")
    return 0


class Locker:
    def __init__(self, devices, config, pin, decisions):
        self._devices = devices
        self._config = config
        self._pin_salt, self._pin_hash = pin
        self._grabbed = []
        self._known_nodes = {info.path: info.identity for info, _, _ in decisions}
        self._hotplug_retries = {}
        self._recovery_fds = {info.fd for info, grab, _ in decisions
                              if grab and can_recover(info)}
        self._keys_down = {}
        self._resync = set()
        self._pin_fd = None
        self._digits = ""
        self._prompt_deadline = None
        self._retry_at = None
        self._running = True
        self.force_release = False

    @property
    def _fds(self):
        return [fd for _, _, fd in self._devices]

    def stop(self, _signum=None, _frame=None):
        self._running = False

    def _add_grab(self, entry, quiet=False):
        path, name, fd = entry
        if not set_grab(fd, name, True, quiet):
            return False
        self._grabbed.append(entry)
        self._discard_backlog(fd)
        self._seed_key_state(fd)
        log(f"grabbed {path} ({name})")
        return True

    def grab_all(self):
        if not self._recovery_fds:
            log("no selected keyboard supports Ctrl+A+Esc; refusing to lock")
            return False
        wait_for_sticky_keys_released(self._fds)
        for entry in self._devices:
            if not self._running or not self._add_grab(entry):
                self.release_all()
                return False
        return True

    def _discard_backlog(self, fd):
        # Events already passed to the compositor must not trigger a chord
        # after locking. Bound the drain so a noisy device cannot delay others.
        for _ in range(16):
            try:
                if len(os.read(fd, EVENT_SIZE * 64)) < EVENT_SIZE * 64:
                    break
            except OSError:
                break

    def _seed_key_state(self, fd):
        bitmap = read_key_bitmap(fd)
        self._keys_down[fd] = {code for code in CHORD_KEYS if bit_is_set(bitmap, code)}

    def _dismiss_prompt(self):
        self._pin_fd = None
        self._digits = ""
        self._prompt_deadline = None
        clear_auth_state()

    def _publish_prompt(self, state):
        if not write_auth_state(state):
            self._dismiss_prompt()

    def _begin_prompt(self, fd):
        self._pin_fd = fd
        self._digits = ""
        self._prompt_deadline = time.monotonic() + PIN_IDLE_SECONDS
        self._publish_prompt("wrong" if self._retry_at is not None else "enter:0")

    def _handle_key(self, fd, code, value):
        keys = self._keys_down[fd]
        if code in CHORD_KEYS:
            if value == 0:
                keys.discard(code)
            else:
                keys.add(code)
        if value != 1:
            return DRAIN_OK

        if code == KEY_ESC and keys & CTRL_KEYS:
            if KEY_A in keys:
                return DRAIN_FORCE_RELEASE
            if not keys & OTHER_MODIFIERS and self._pin_fd is None:
                self._begin_prompt(fd)
            return DRAIN_OK

        if fd != self._pin_fd:
            return DRAIN_OK
        if code == KEY_ESC:
            self._dismiss_prompt()
            return DRAIN_OK
        if self._retry_at is not None or keys & (CTRL_KEYS | OTHER_MODIFIERS):
            return DRAIN_OK

        if code in (KEY_ENTER, KEY_KPENTER):
            if valid_pin(self._digits):
                candidate = hash_pin(self._digits, self._pin_salt)
                if hmac.compare_digest(candidate, self._pin_hash):
                    return DRAIN_RELEASE
            self._digits = ""
            self._retry_at = time.monotonic() + PIN_RETRY_SECONDS
            self._prompt_deadline = self._retry_at + PIN_IDLE_SECONDS
            self._publish_prompt("wrong")
            return DRAIN_OK
        if code == KEY_BACKSPACE:
            self._digits = self._digits[:-1]
        elif code in DIGIT_KEYS and len(self._digits) < MAX_PIN_DIGITS:
            self._digits += DIGIT_KEYS[code]
        else:
            return DRAIN_OK

        self._prompt_deadline = time.monotonic() + PIN_IDLE_SECONDS
        self._publish_prompt(f"enter:{len(self._digits)}")
        return DRAIN_OK

    def _drain(self, fd):
        """Read one batch so other devices and deadlines get a turn."""
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return DRAIN_OK
        except OSError as exc:
            log(f"read error on fd {fd}: {exc}")
            return DRAIN_DEAD
        if not data:
            return DRAIN_DEAD

        for _, _, etype, code, value in EVENT_STRUCT.iter_unpack(data):
            if etype == EV_SYN and code == SYN_DROPPED:
                self._resync.add(fd)
                if fd == self._pin_fd:
                    self._dismiss_prompt()
            elif fd in self._resync:
                if etype == EV_SYN and code == SYN_REPORT:
                    self._resync.remove(fd)
                    self._seed_key_state(fd)
            elif etype == EV_KEY:
                outcome = self._handle_key(fd, code, value)
                if outcome != DRAIN_OK:
                    return outcome
        return DRAIN_OK

    def release_all(self):
        # The emergency shortcut explicitly skips the key-up delay.
        if self._grabbed and not self.force_release:
            wait_for_sticky_keys_released(
                self._fds, timeout=0.25, keys=STICKY_KEYS + (KEY_ENTER, KEY_KPENTER)
            )
        for path, name, fd in self._grabbed:
            if set_grab(fd, name, False):
                log(f"released {path} ({name})")
        self._grabbed.clear()

    def _drop_device(self, fd):
        entry = next(entry for entry in self._devices if entry[2] == fd)
        self._devices.remove(entry)
        self._grabbed.remove(entry)
        self._keys_down.pop(fd, None)
        self._resync.discard(fd)
        self._known_nodes.pop(entry[0], None)
        was_recovery = fd in self._recovery_fds
        self._recovery_fds.discard(fd)
        if fd == self._pin_fd:
            self._dismiss_prompt()
        os.close(fd)
        log(f"device disconnected: {entry[1]}")
        if not self._grabbed or (was_recovery and not self._recovery_fds):
            log("no grabbed input or recovery keyboard remains; releasing the lock")
            self.stop()

    def _scan_new_devices(self, poller):
        current_nodes = {}
        retry_nodes = {}
        for path in glob.glob(INPUT_GLOB):
            try:
                identity = device_identity(os.stat(path))
            except OSError:
                continue
            if self._known_nodes.get(path) == identity:
                current_nodes[path] = identity
                continue

            retry = self._hotplug_retries.get(path) == identity
            try:
                info = open_device(path)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENODEV):
                    if not retry:
                        log(f"will retry inspecting new input device {path}: {exc}")
                    retry_nodes[path] = identity
                continue
            if info is None:
                continue

            grab, _ = decide(info, self._config)
            if not grab:
                current_nodes[path] = info.identity
                os.close(info.fd)
                continue
            entry = info.path, info.name, info.fd
            if not self._add_grab(entry, quiet=retry):
                os.close(info.fd)
                if not retry:
                    log(f"will retry grabbing newly connected input: {info.name}")
                retry_nodes[path] = info.identity
                continue
            current_nodes[path] = info.identity
            self._devices.append(entry)
            if can_recover(info):
                self._recovery_fds.add(info.fd)
            poller.register(info.fd, select.POLLIN)
        self._known_nodes = current_nodes
        self._hotplug_retries = retry_nodes

    def run(self):
        poller = select.poll()
        for fd in self._fds:
            poller.register(fd, select.POLLIN)
        now = time.monotonic()
        max_lock = self._config.max_lock_seconds
        lock_deadline = now + max_lock if max_lock > 0 else None
        next_scan = now + SCAN_SECONDS
        interval = watchdog_interval()
        next_ping = now if interval is not None else None

        while self._running:
            now = time.monotonic()
            if lock_deadline is not None and now >= lock_deadline:
                log(f"max_lock_seconds ({max_lock}) reached, unlocking")
                return
            if next_ping is not None and now >= next_ping:
                sd_notify("WATCHDOG=1")
                next_ping = now + interval
            if self._prompt_deadline is not None and now >= self._prompt_deadline:
                self._dismiss_prompt()
            if self._retry_at is not None and now >= self._retry_at:
                self._retry_at = None
                if self._pin_fd is not None:
                    self._publish_prompt("enter:0")

            wake_at = min(t for t in (next_scan, next_ping, lock_deadline,
                                     self._prompt_deadline, self._retry_at)
                          if t is not None)
            try:
                ready = poller.poll(max(0, int((wake_at - now) * 1000 + 0.999)))
            except OSError as exc:
                log(f"poll failed: {exc}")
                return
            if not self._running:
                return
            if self._prompt_deadline is not None and time.monotonic() >= self._prompt_deadline:
                self._dismiss_prompt()
            for fd, events in ready:
                outcome = DRAIN_DEAD if events & DEVICE_FAULT else self._drain(fd)
                if outcome in (DRAIN_FORCE_RELEASE, DRAIN_RELEASE):
                    self.force_release = outcome == DRAIN_FORCE_RELEASE
                    log("emergency shortcut released input" if self.force_release
                        else "PIN accepted; releasing input")
                    return
                if outcome == DRAIN_DEAD:
                    poller.unregister(fd)
                    self._drop_device(fd)
                    if not self._running:
                        return

            now = time.monotonic()
            if now >= next_scan and self._running:
                self._scan_new_devices(poller)
                next_scan = now + SCAN_SECONDS


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocol-version", action="version", version="3")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--set-pin", action="store_true", help="set the unlock PIN")
    modes.add_argument("--pin-configured", action="store_true", help="check the PIN file")
    modes.add_argument(
        "--list",
        action="store_true",
        help="list input devices and which ones would be grabbed, then exit",
    )
    parser.add_argument(
        "--config", default=CONFIG_PATH, help=f"config file (default: {CONFIG_PATH})"
    )
    args = parser.parse_args()

    if args.set_pin:
        return set_pin()
    if args.pin_configured:
        return 0 if read_pin() is not None else 1

    config = read_config(args.config)
    if args.list:
        return list_devices(config)

    pin = read_pin()
    if pin is None:
        log(f"no valid private PIN in {PIN_PATH}; run the daemon with --set-pin first")
        return 1

    devices, denied, decisions = find_devices(config)
    if not devices:
        if denied:
            # Being root is not sufficient here: SELinux decides separately.
            # This is what a service running in the wrong domain looks like.
            log(f"permission denied opening {denied} input device(s)")
            log("running as root is not enough under SELinux -- the service "
                "must run in a domain allowed to read input devices")
            log("check: ls -Z on this daemon (expect bin_t, not lib_t), and "
                "sudo ausearch -m avc -ts recent")
        elif config.mode == MODE_PATTERNS:
            log("no input devices matched; refusing to start")
            log(f"patterns were: {config.patterns}")
            log("compare against --list, or drop `patterns` from the config "
                "to use auto detection instead")
        else:
            log("auto detection found no internal keyboard or pointer; "
                "refusing to start")
            log("see --list for what is there; an internal device on USB needs "
                "include_usb = yes")
        return 1

    log_safety_notes(decisions)

    locker = Locker(devices, config, pin, decisions)
    signal.signal(signal.SIGTERM, locker.stop)
    signal.signal(signal.SIGINT, locker.stop)

    grabbed = False
    published = False
    try:
        if not locker.grab_all():
            log("did not get an exclusive grab on every matched device; "
                "refusing to stay running")
            return 1
        grabbed = True
        clear_auth_state()

        # The state file is the single source of truth for every consumer. A
        # grab nobody can observe is worse than no grab at all -- input-lock-x
        # would report "NOT engaged" and the panel icon would show unlocked
        # while the keyboard was dead -- so treat this as fatal.
        if not write_state():
            log("refusing to hold a lock that nothing can observe")
            return 1
        published = True

        # Only now is the lock genuinely engaged, so this is where readiness
        # is reported. Anything that fails above fails `systemctl start` too.
        sd_notify("READY=1")

        locker.run()
    finally:
        # Only if READY=1 went out. Both returns above leave without ever
        # having reported readiness, and STOPPING=1 from a service that never
        # started is a protocol lie; the non-zero exit is what systemd should
        # be reading there.
        if published:
            sd_notify("STOPPING=1")
        if grabbed:
            locker.release_all()
        # Only remove a state file this process created. A second instance
        # started by hand gets EBUSY on every grab and exits -- if it cleared
        # the file on the way out it would tell the world "unlocked" while the
        # real daemon still held the keyboard, and StartUnit on an
        # already-active unit would not recreate it.
        for _, _, fd in devices:
            try:
                os.close(fd)
            except OSError:
                pass
        if published:
            clear_state()
            clear_auth_state()
        if grabbed:
            log("input lock ended, input restored")

    return 0


if __name__ == "__main__":
    sys.exit(main())
