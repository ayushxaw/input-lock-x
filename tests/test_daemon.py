# Copyright (C) 2026 Isak Lindgren
# SPDX-License-Identifier: GPL-2.0-or-later
# Tests for PIN unlock and device recovery by ayushxaw, 2026-11-25.
"""Offline tests for the daemon's device classification and config parsing."""

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


DAEMON = Path(__file__).resolve().parents[1] / "daemon" / "input-lock-x-daemon.py"
spec = importlib.util.spec_from_file_location("input_lock_x_daemon", DAEMON)
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


def bitmap(codes, size):
    """Build the byte bitmap returned by an evdev capability ioctl."""
    result = bytearray(size)
    for code in codes:
        result[code // 8] |= 1 << (code % 8)
    return result


def device(name="device", bustype=0, props=(), keys=(), rels=(), abss=()):
    """Build a DeviceInfo without opening a real /dev/input node."""
    info = daemon.DeviceInfo.__new__(daemon.DeviceInfo)
    info.path = "/dev/input/event0"
    info.fd = -1
    info.name = name
    info.identity = (1, 1)
    info.bustype = bustype
    info.props = bitmap(props, daemon.INPUT_PROP_BYTES)
    info.keys = bitmap(keys, daemon.KEY_CAP_BYTES)
    info.rels = bitmap(rels, daemon.REL_BYTES)
    info.abss = bitmap(abss, daemon.ABS_BYTES)
    info.kind = info._classify()
    return info


def auto_config(**overrides):
    values = {
        "mode": daemon.MODE_AUTO,
        "patterns": [],
        "exclude": [],
        "include_usb": False,
        "include_bluetooth": False,
        "max_lock_seconds": 0,
    }
    values.update(overrides)
    return daemon.Config(**values)


class ClassificationTests(unittest.TestCase):
    def test_direct_touchscreen_wins_over_other_capabilities(self):
        info = device(
            props=[daemon.INPUT_PROP_DIRECT, daemon.INPUT_PROP_POINTER],
            keys=[daemon.BTN_LEFT, *daemon.KEYBOARD_PROBE_KEYS],
            rels=[daemon.REL_X, daemon.REL_Y],
        )
        self.assertEqual(info.kind, daemon.CLASS_TOUCHSCREEN)

    def test_generic_multitouch_without_a_button_is_touchscreen(self):
        info = device(
            keys=[daemon.BTN_TOOL_FINGER],
            abss=[daemon.ABS_MT_POSITION_X],
        )
        self.assertEqual(info.kind, daemon.CLASS_TOUCHSCREEN)

    def test_pointer_and_buttonpad_are_pointers(self):
        pointer = device(
            props=[daemon.INPUT_PROP_POINTER],
            keys=[daemon.BTN_LEFT],
            rels=[daemon.REL_X, daemon.REL_Y],
        )
        buttonpad = device(props=[daemon.INPUT_PROP_BUTTONPAD])
        self.assertEqual(pointer.kind, daemon.CLASS_POINTER)
        self.assertEqual(buttonpad.kind, daemon.CLASS_POINTER)

    def test_keyboard_requires_all_probe_keys(self):
        keyboard = device(keys=daemon.KEYBOARD_PROBE_KEYS)
        sparse_keymap = device(keys=daemon.KEYBOARD_PROBE_KEYS[:-1])
        self.assertEqual(keyboard.kind, daemon.CLASS_KEYBOARD)
        self.assertEqual(sparse_keymap.kind, daemon.CLASS_OTHER)


class DecisionTests(unittest.TestCase):
    def test_auto_mode_grabs_internal_keyboard_and_pointer_only(self):
        config = auto_config()
        cases = (
            (device("keyboard", keys=daemon.KEYBOARD_PROBE_KEYS), True),
            (device("touchscreen", props=[daemon.INPUT_PROP_DIRECT]), False),
            (device("other"), False),
            (
                device("usb keyboard", daemon.BUS_USB,
                       keys=daemon.KEYBOARD_PROBE_KEYS),
                False,
            ),
            (
                device("virtual keyboard", daemon.BUS_VIRTUAL,
                       keys=daemon.KEYBOARD_PROBE_KEYS),
                False,
            ),
        )
        for info, expected in cases:
            with self.subTest(info=info.name):
                self.assertEqual(daemon.decide(info, config)[0], expected)

    def test_auto_mode_can_include_usb(self):
        info = device("internal usb keyboard", daemon.BUS_USB,
                      keys=daemon.KEYBOARD_PROBE_KEYS)
        grab, reason = daemon.decide(info, auto_config(include_usb=True))
        self.assertTrue(grab)
        self.assertIn("internal keyboard", reason)

    def test_patterns_override_safety_exemptions_but_not_exclude(self):
        config = auto_config(mode=daemon.MODE_PATTERNS, patterns=["*"],
                             exclude=["never"])
        touchscreen = device("touchscreen", props=[daemon.INPUT_PROP_DIRECT])
        usb = device("usb", daemon.BUS_USB, keys=daemon.KEYBOARD_PROBE_KEYS)
        virtual = device("virtual", daemon.BUS_VIRTUAL,
                         keys=daemon.KEYBOARD_PROBE_KEYS)
        excluded = device("never", keys=daemon.KEYBOARD_PROBE_KEYS)

        self.assertTrue(daemon.decide(touchscreen, config)[0])
        self.assertTrue(daemon.decide(usb, config)[0])
        self.assertTrue(daemon.decide(virtual, config)[0])
        self.assertFalse(daemon.decide(excluded, config)[0])

    def test_escape_routes_exclude_grabbed_and_virtual_devices(self):
        touchscreen = device("panel", props=[daemon.INPUT_PROP_DIRECT])
        pointer = device("mouse", props=[daemon.INPUT_PROP_POINTER])
        keyboard = device("spare keyboard", keys=daemon.KEYBOARD_PROBE_KEYS)
        virtual = device("virtual pointer", daemon.BUS_VIRTUAL,
                         props=[daemon.INPUT_PROP_POINTER])
        decisions = [
            (touchscreen, False, "kept"),
            (pointer, False, "kept"),
            (keyboard, True, "grabbed"),
            (virtual, False, "kept"),
        ]

        self.assertEqual(
            daemon.escape_routes(decisions),
            [
                "the touchscreen",
                "an ungrabbed pointer",
            ],
        )


class ConfigTests(unittest.TestCase):
    def test_config_parses_multiline_patterns_and_scalar_comments(self):
        contents = """\
[devices]
mode = patterns  # use the explicit list
patterns = First Device
  Second Device
include_usb = yes  # an annotated boolean
include_bluetooth = no

[behaviour]
max_lock_seconds = 60  # release after one minute
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-lock-x.conf"
            path.write_text(contents, encoding="utf-8")
            config = daemon.read_config(path)

        self.assertEqual(config.mode, daemon.MODE_PATTERNS)
        self.assertEqual(config.patterns, ["First Device", "Second Device"])
        self.assertTrue(config.include_usb)
        self.assertFalse(config.include_bluetooth)
        self.assertEqual(config.max_lock_seconds, 60)

    def test_negative_timeout_is_disabled(self):
        contents = "[behaviour]\nmax_lock_seconds = -60\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-lock-x.conf"
            path.write_text(contents, encoding="utf-8")
            config = daemon.read_config(path)

        self.assertEqual(config.max_lock_seconds, 0)


class PinTests(unittest.TestCase):
    def test_pin_structure_is_numeric_ascii_and_bounded(self):
        for pin in ("0028", "012345678901"):
            self.assertTrue(daemon.valid_pin(pin))
        for pin in ("123", "1234567890123", "١٢٣٤", "１２３４", "123x"):
            self.assertFalse(daemon.valid_pin(pin))

    def test_pin_file_requires_private_mode_regular_file_and_valid_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pin"
            salt = bytes(daemon.PIN_SALT_BYTES)
            digest = bytes(daemon.PIN_HASH_BYTES)
            record = f"pbkdf2-sha256 {salt.hex()} {digest.hex()}\n"
            daemon.atomic_write(path, record, 0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(daemon.read_pin(path, owner=os.getuid()), (salt, digest))
            self.assertIsNone(daemon.read_pin(path, owner=os.getuid() + 1))

            symlink = Path(directory) / "pin-link"
            symlink.symlink_to(path)
            self.assertIsNone(daemon.read_pin(symlink, owner=os.getuid()))

            path.chmod(0o644)
            self.assertIsNone(daemon.read_pin(path, owner=os.getuid()))
            daemon.atomic_write(path, record.replace("pbkdf2-sha256", "pbkdf2-sha1"), 0o600)
            self.assertIsNone(daemon.read_pin(path, owner=os.getuid()))

    @patch.object(daemon.getpass, "getpass")
    @patch.object(daemon.sys.stdin, "isatty", return_value=False)
    def test_pin_setup_refuses_piped_input(self, _isatty, getpass):
        self.assertEqual(daemon.set_pin(), 1)
        getpass.assert_not_called()

    @patch.object(daemon.os, "geteuid", return_value=0)
    @patch.object(daemon.sys.stdin, "isatty", return_value=True)
    @patch.object(daemon.getpass, "getpass", return_value="6280")
    def test_pin_setup_stores_only_salted_hashes(self, _getpass, _isatty, _geteuid):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pin"
            self.assertEqual(daemon.set_pin(path), 0)
            first = daemon.read_pin(path, owner=os.getuid())
            self.assertEqual(first[1], daemon.hash_pin("6280", first[0]))
            self.assertNotIn("6280", path.read_text().split())
            self.assertEqual(daemon.set_pin(path), 0)
            self.assertNotEqual(daemon.read_pin(path, owner=os.getuid()), first)


class LockerTests(unittest.TestCase):
    def setUp(self):
        self.keyboard = ("/dev/input/event1", "Keyboard", 1)
        self.mouse = ("/dev/input/event2", "Mouse", 2)
        self.config = auto_config(include_usb=True)
        self.salt = bytes(daemon.PIN_SALT_BYTES)
        keys = [*daemon.KEYBOARD_PROBE_KEYS, daemon.KEY_LEFTCTRL, daemon.KEY_ESC]
        keyboard_info = device(name="Keyboard", keys=keys)
        keyboard_info.fd, keyboard_info.path = 1, self.keyboard[0]
        self.locker = daemon.Locker(
            [self.keyboard, self.mouse], self.config, (self.salt, b"match"),
            [(keyboard_info, True, "")],
        )
        self.locker._grabbed = [self.keyboard, self.mouse]
        self.locker._keys_down = {1: set(), 2: set()}
        self.status = self.start_patch("write_auth_state", return_value=True)
        self.clear_status = self.start_patch("clear_auth_state")
        self.start_patch("hash_pin", side_effect=lambda pin, _salt: (
            b"match" if pin == "0280" else b"wrong"
        ))

    def start_patch(self, name, **kwargs):
        mock = patch.object(daemon, name, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def key(self, code, value=1, fd=1):
        return self.locker._handle_key(fd, code, value)

    def open_prompt(self):
        self.key(daemon.KEY_LEFTCTRL)
        self.key(daemon.KEY_ESC)
        self.key(daemon.KEY_ESC, 0)
        self.key(daemon.KEY_LEFTCTRL, 0)

    def enter_pin(self):
        for digit in (11, 3, 9, 82):
            self.key(digit)

    def test_correct_pin_uses_same_keyboard_and_accepts_keypad(self):
        self.open_prompt()
        self.key(2, fd=2)
        self.enter_pin()
        self.status.assert_called_with("enter:4")
        self.assertEqual(self.key(daemon.KEY_KPENTER), daemon.DRAIN_RELEASE)

    def test_wrong_pin_stays_locked_and_cancel_clears_status(self):
        self.open_prompt()
        self.key(2)
        self.assertEqual(self.key(daemon.KEY_ENTER), daemon.DRAIN_OK)
        self.status.assert_called_with("wrong")
        self.key(3)
        self.assertEqual(self.locker._digits, "")
        self.key(daemon.KEY_ESC)
        self.clear_status.assert_called_once()
        self.assertEqual(self.locker._grabbed, [self.keyboard, self.mouse])

    def test_reopening_prompt_preserves_wrong_pin_cooldown(self):
        with patch.object(daemon.time, "monotonic", return_value=10):
            self.open_prompt()
            self.key(daemon.KEY_ENTER)
            self.key(daemon.KEY_ESC)
            self.open_prompt()
            self.status.assert_called_with("wrong")
            self.enter_pin()
            self.assertEqual(self.locker._digits, "")

        poller = Mock()
        poller.poll.side_effect = OSError("test complete")
        with patch.object(daemon.time, "monotonic", return_value=12), \
            patch.object(daemon.select, "poll", return_value=poller):
            self.locker.run()

        self.status.assert_called_with("enter:0")
        self.enter_pin()
        self.assertEqual(self.key(daemon.KEY_ENTER), daemon.DRAIN_RELEASE)

    def test_retry_expiring_does_not_reopen_dismissed_prompt(self):
        with patch.object(daemon.time, "monotonic", return_value=10):
            self.open_prompt()
            self.key(daemon.KEY_ENTER)
            self.key(daemon.KEY_ESC)

        poller = Mock()
        poller.poll.side_effect = OSError("test complete")
        with patch.object(daemon.time, "monotonic", return_value=12), \
            patch.object(daemon.select, "poll", return_value=poller):
            self.locker.run()

        self.status.assert_called_with("wrong")
        self.assertIsNone(self.locker._pin_fd)
        self.assertIsNone(self.locker._retry_at)

    def test_cross_device_chord_and_repeated_escape_are_ignored(self):
        self.key(daemon.KEY_LEFTCTRL)
        self.assertEqual(self.key(daemon.KEY_ESC, fd=2), daemon.DRAIN_OK)
        self.assertEqual(self.key(daemon.KEY_ESC, value=2), daemon.DRAIN_OK)
        self.status.assert_not_called()
        self.key(daemon.KEY_A, fd=2)
        self.assertEqual(self.key(daemon.KEY_ESC), daemon.DRAIN_OK)
        self.status.assert_called_once_with("enter:0")

    def test_emergency_chord_works_in_prompt_and_skips_release_wait(self):
        self.open_prompt()
        self.key(daemon.KEY_LEFTCTRL)
        self.key(daemon.KEY_A)
        outcome = self.key(daemon.KEY_ESC)
        self.assertEqual(outcome, daemon.DRAIN_FORCE_RELEASE)
        self.locker.force_release = True
        set_grab = self.start_patch("set_grab", return_value=True)
        wait = self.start_patch("wait_for_sticky_keys_released")
        self.locker.release_all()
        self.assertEqual(set_grab.call_count, 2)
        wait.assert_not_called()
        self.assertEqual(self.locker._grabbed, [])

    def test_prompt_timeout_only_clears_pin_state(self):
        self.open_prompt()
        self.locker._prompt_deadline = 10
        poller = Mock()
        poller.poll.side_effect = OSError("test complete")
        with patch.object(daemon.time, "monotonic", return_value=11), \
            patch.object(daemon.select, "poll", return_value=poller):
            self.locker.run()
        self.clear_status.assert_called_once()
        self.assertEqual(self.locker._grabbed, [self.keyboard, self.mouse])

    def test_overflow_cancels_prompt_and_recovers_chord_key_state(self):
        self.open_prompt()
        self.enter_pin()
        pack = daemon.EVENT_STRUCT.pack
        batch = (
            pack(0, 0, daemon.EV_SYN, daemon.SYN_DROPPED, 0)
            + pack(0, 0, daemon.EV_KEY, daemon.KEY_ENTER, 1)
            + pack(0, 0, daemon.EV_SYN, daemon.SYN_REPORT, 0)
            + pack(0, 0, daemon.EV_KEY, daemon.KEY_ESC, 1)
        )
        keys = bitmap((daemon.KEY_LEFTCTRL, daemon.KEY_A), daemon.KEY_BITMAP_BYTES)
        with patch.object(daemon.os, "read", return_value=batch), \
            patch.object(daemon, "read_key_bitmap", return_value=keys):
            self.assertEqual(self.locker._drain(1), daemon.DRAIN_FORCE_RELEASE)
        self.clear_status.assert_called_once()

    def test_release_on_last_recovery_keyboard_loss(self):
        with patch.object(daemon.os, "close") as close:
            self.locker._drop_device(2)
            self.assertTrue(self.locker._running)
            self.locker._drop_device(1)
            self.assertFalse(self.locker._running)
            self.assertEqual(close.call_count, 2)

    def test_no_lock_when_selected_device_has_no_emergency_chord(self):
        self.locker._recovery_fds.clear()
        set_grab = self.start_patch("set_grab", return_value=True)
        self.assertFalse(self.locker.grab_all())
        set_grab.assert_not_called()

    def test_hotplug_retries_a_busy_device_without_disabling_other_grabs(self):
        mouse = device(name="USB mouse", bustype=daemon.BUS_USB,
                       props=[daemon.INPUT_PROP_POINTER])
        mouse.path, mouse.fd, mouse.identity = "/dev/input/event3", 3, (1, 3)
        poller = Mock()
        self.start_patch("open_device", return_value=mouse)
        set_grab = self.start_patch("set_grab", side_effect=[False, True])
        with patch.object(daemon.glob, "glob", return_value=[mouse.path]), \
            patch.object(daemon.os, "stat", return_value=SimpleNamespace(st_dev=1, st_ino=3)), \
            patch.object(daemon.os, "close"), \
            patch.object(daemon.os, "read", return_value=b""), \
            patch.object(daemon, "read_key_bitmap", return_value=None):
            self.locker._scan_new_devices(poller)
            self.assertTrue(self.locker._running)
            self.assertEqual(self.locker._grabbed, [self.keyboard, self.mouse])
            self.locker._scan_new_devices(poller)
            self.locker._scan_new_devices(poller)

        poller.register.assert_called_once_with(3, daemon.select.POLLIN)
        self.assertEqual(set_grab.call_count, 2)
        self.assertEqual(self.locker._grabbed[-1], (mouse.path, mouse.name, mouse.fd))

    def test_watchdog_obeys_systemd_process_and_interval(self):
        notify = self.start_patch("sd_notify")
        poller = Mock()
        poller.poll.side_effect = OSError("test complete")
        with patch.dict(daemon.os.environ, {"WATCHDOG_PID": str(os.getpid()),
                                          "WATCHDOG_USEC": "15000000"}), \
            patch.object(daemon.select, "poll", return_value=poller):
            self.assertEqual(daemon.watchdog_interval(), 7.5)
            self.locker.run()
        notify.assert_called_once_with("WATCHDOG=1")
        with patch.dict(daemon.os.environ, {"WATCHDOG_PID": "0",
                                          "WATCHDOG_USEC": "15000000"}):
            self.assertIsNone(daemon.watchdog_interval())


if __name__ == "__main__":
    unittest.main()
