# Changelog

## Unreleased

- Prevent installer migration from assigning GNOME's screen-lock shortcut to
  Input Lock X. Super+L remains available for the normal desktop lock screen.
- Skip conflicting shortcuts from existing settings, even if GNOME's
  screen-lock shortcut changes while Input Lock X is enabled.
- Keep the shortcut and PIN prompt available if the panel indicator can't load.

## 1.0.0 — 2026-08-30

Initial public release of Input Lock X:

- GNOME Shell panel toggle and configurable engage shortcut.
- Companion root daemon that grabs selected evdev devices.
- Auto device classification that leaves the touchscreen and external input
  alone by default.
- Ctrl+Esc emergency release path, including the lock screen.
- Installer, systemd service, polkit policy, SSH recovery, tests, and
  extension packaging.
- GNOME Shell 45–50 support on Wayland and X11.
