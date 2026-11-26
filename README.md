# Input Lock X

Input Lock X keeps your screen visible while it temporarily blocks your
configured keyboard, touchpad, and mouse. It runs on GNOME Shell and keeps the
display awake while input is locked.

## When to use it

- **Step away in a library.** Leave a lecture, video, or set of notes visible for
  a friend while blocking clicks and keystrokes from the devices you locked.
- **Show something without handing over the controls.** Keep a slide, demo, or
  document visible on a shared table without people accidentally navigating away.
- **Clean the keyboard or touchpad.** Wipe the controls while your work stays on
  screen.
- **Use a convertible in tablet mode.** Block the folded keyboard and touchpad
  while using a touchscreen that remains available in auto mode.

Use [Focus Lock](https://github.com/ayushxaw/focus-lock) when someone should
still be able to use a single app, for example to play a game while the window
stays in focus.

## Quick reference

[Install and choose your first PIN](#installation) ·
[Change or reset your PIN](#change-or-reset-your-pin) ·
[Emergency release](#recovery) · [Command list](HELPER.txt)

### Shortcuts

| Action | Shortcut |
| --- | --- |
| Lock configured devices | Press **Ctrl+Esc**. |
| Unlock with your PIN | Press **Ctrl+Esc** on a locked keyboard, type your Lock X PIN on that keyboard, and press **Enter**. |
| Dismiss the PIN prompt | Press **Esc**. Input stays locked. |
| Emergency release | Hold **Ctrl+A**, then press **Esc** on the same locked keyboard. |

Anyone who knows the emergency shortcut can release Input Lock X. Use GNOME's
screen lock when you need to protect access to your desktop.

## Requirements

- GNOME Shell 45–50, on Wayland or X11
- systemd
- polkit 0.106 or newer
- Python 3 (standard library only)
- `git`, `make`, and `glib-compile-schemas` for installation

The installer supports Linux systems with access to `/dev/input` and a working
systemd and polkit setup.

Install the GNOME extension and system daemon together from this repository.

## Recovery

If the lock does not release normally:

1. On the same locked keyboard, hold **Ctrl** and **A**, then press **Esc**.
   The daemon releases all of its input grabs, including during a PIN prompt.
   This shortcut works without GNOME and without a PIN. Anyone who knows it
   can release the lock.
2. From another machine, an administrator can run the following command.
   Replace `user@laptop` with the SSH login for your laptop.

   ```sh
   ssh user@laptop input-lock-x-emergency-unlock
   ```

3. An administrator can also run `sudo systemctl stop input-lock-x.service`.
   The kernel releases the input grabs if the daemon stops or crashes.
   Systemd stops the daemon if its watchdog stops receiving updates.

The recovery shortcut releases Input Lock X. Your normal sign-in is still
required if the GNOME lock screen is active.

## Installation

Run the installer from a terminal as your normal user while input is unlocked.
It uses `sudo` to install a system service and asks you to choose a 4–12 digit
Lock X PIN if you have not set one. Keep a second device with an SSH session
open during the first install and hardware test so that you can run the
[recovery command](#recovery) if needed.

### 1. Install prerequisites

For example:

```sh
sudo dnf install git make glib2-devel        # Fedora/RHEL
sudo apt install git make libglib2.0-bin     # Debian/Ubuntu
sudo pacman -S git make glib2                # Arch
sudo zypper install git make glib2-devel     # openSUSE
```

### 2. Clone the repository

Keep the checkout in a permanent location. The installer symlinks the extension
directory to this checkout.

```sh
git clone https://github.com/ayushxaw/input-lock-x.git
cd input-lock-x
```

### 3. Check device detection

This reads `/proc/bus/input/devices` and checks default device selection
without root.

```sh
make test
```

Your internal keyboard and touchpad should be listed as `GRAB`. The touchscreen,
external keyboard, and other devices you need for recovery should be listed as
`skip`. If detection is wrong, use the configuration described below.

### 4. Install

```sh
./install.sh
```

The installer:

- compiles the GSettings schema;
- installs the daemon, service, polkit rule, and command-line tools;
- leaves an existing `/etc/input-lock-x.conf` unchanged;
- starts the service to verify that it can actually grab the matched devices;
- enables the extension when this checkout owns the extension directory.

It refuses to continue if the service cannot be verified. The first install
normally requires a logout and login before GNOME loads the extension.

After logging in, check the installation:

```sh
gnome-extensions info input-lock-x@isalin.github.io
input-lock-x status
```

If you have an SSH session open, test the complete round trip:

```sh
ssh user@laptop input-lock-x on
# On the laptop, press Ctrl+Esc, type the PIN, and press Enter.
ssh user@laptop input-lock-x-emergency-unlock  # if needed
```

### Installer options

```text
--no-sudoers       Do not install passwordless SSH recovery rules.
--daemon-only      Install only the system daemon and command-line tools.
--link-extension   Replace another extension install with this checkout.
```

Use `--daemon-only` to update the system files while keeping an existing
extension installation.

`--link-extension` is intended for development or when you explicitly want this
checkout to replace another copy. The installer asks before deleting a foreign
extension directory or repointing another symlink.

Without the sudoers file, use a TTY for SSH recovery:

```sh
ssh -t user@laptop sudo input-lock-x-emergency-unlock
```

### Updating

With the lock released, open a terminal in your Input Lock X checkout and run:

```sh
git pull
make install-daemon
```

The installer preserves `/etc/input-lock-x.conf` and your PIN. Sign out of
GNOME and back in to load the updated extension code. GNOME loads that code
through a symlink, so keep the checkout in place while using it.

### Uninstalling

```sh
./uninstall.sh
```

It releases any active lock before removing the service and tools. Edited config
files are left in `/etc` so they can be reviewed or removed manually. An
extension directory that this installer did not create is left alone.

Older installations named `minibookx-hid-lock` are migrated automatically by
`install.sh`. Their config and a non-conflicting shortcut are carried over. If
the old shortcut is also GNOME's screen-lock shortcut, Input Lock X uses its
safe default, **Ctrl+Esc**, so **Super+L** continues to lock the desktop.

## Usage

The command-line tool can be used locally or over SSH:

```sh
input-lock-x on       # engage the lock
input-lock-x off      # release the lock
input-lock-x toggle   # switch between the two states
input-lock-x status   # show state and matched devices
```

**Ctrl+Esc** engages Input Lock X. **Super+L** (Win+L) opens GNOME's normal
lock screen when Input Lock X is off. If a saved Input Lock X shortcut overlaps
with GNOME's screen lock, Input Lock X skips the conflicting binding and uses
Ctrl+Esc when no other configured binding is available.

To change the shortcut that engages Input Lock X, use
**Extensions → Input Lock X → Settings**, or restore Ctrl+Esc from a terminal:

```sh
gsettings --schemadir \
  ~/.local/share/gnome-shell/extensions/input-lock-x@isalin.github.io/schemas \
  set org.gnome.shell.extensions.input-lock-x toggle-hid "['<Control>Escape']"
```

On a grabbed keyboard, **Ctrl+Esc** always opens the PIN prompt. Type the PIN
on that keyboard and press **Enter**. Use **Backspace** to correct digits or
**Esc** to dismiss the prompt. A wrong PIN, a cancelled prompt, or a prompt
timeout leaves input locked.

## Change or reset your PIN

While input is unlocked, open Terminal and run:

```sh
sudo /usr/local/bin/input-lock-x-daemon --set-pin
```

If `sudo` asks for a password, enter your Linux account password first.
Then choose a separate 4–12 digit Lock X PIN and enter it twice. The new PIN
takes effect the next time you lock. Lock X stores a salted hash on your machine.

If you forget your PIN, hold **Ctrl+A** and press **Esc** on the locked
keyboard to release input, then run the same command to set a new PIN.

## Device selection

Auto mode examines every `/dev/input/event*` device:

| Device | Auto mode |
| --- | --- |
| Touchscreen | Never grabbed |
| Internal keyboard | Grabbed |
| Internal touchpad or pointing stick | Grabbed |
| USB or Bluetooth keyboard/pointer | Skipped by default |
| Virtual/uinput device | Never grabbed |
| Other devices | Skipped |

The daemon uses Linux evdev capabilities and input properties to select
devices. Enable `include_usb` or `include_bluetooth` to lock matching external
keyboards and pointing devices. A matching device connected during a lock can
briefly accept input before the next scan.

### Configuration

The configuration file is `/etc/input-lock-x.conf`. It is installed once and
never overwritten by a reinstall.

```ini
[devices]
mode = auto              # auto or patterns
include_usb = no
include_bluetooth = no
# patterns = AT Translated Set 2 keyboard
#            *Touchpad
# exclude = *Fingerprint*

[behaviour]
max_lock_seconds = 0     # 0 disables automatic release
```

`patterns` are fnmatch globs against the evdev device name. Setting `patterns`
without setting `mode` also selects patterns mode for compatibility with older
configs. `exclude` applies in both modes.

In patterns mode, matching is explicit: touchscreen, USB/Bluetooth, and virtual
device safeguards do not apply. Do not match your touchscreen unless you have
another reliable way back in. Check the result before enabling the lock:

```sh
sudo input-lock-x-daemon --list
```

After editing the file, start the service once and check the journal. On SELinux
systems, a successful `--list` command does not necessarily mean the systemd
service can open the devices.

## Permissions and security

The polkit rule allows members of `wheel`, `sudo`, or `admin` to start
this service from an active local session. By default, the installer also adds
`/etc/sudoers.d/input-lock-x`, allowing those groups to run only the two
installed control scripts without a password. This makes non-interactive SSH
recovery possible.

Review these files before installation if that privilege is not appropriate for
your machine. Use `./install.sh --no-sudoers` to omit the sudoers file.

## Troubleshooting

Show the current state and matched devices:

```sh
input-lock-x status
sudo input-lock-x-daemon --list
journalctl -u input-lock-x.service -b
```

**The shortcut reports that the daemon needs an update:** run
`./install.sh --daemon-only` from a regular terminal.

**The panel button or shortcut is denied:** check that the polkit rule exists
and that your user is in `wheel`, `sudo`, or `admin`:

```sh
id -nG
ls /etc/polkit-1/rules.d/49-input-lock-x.rules
```

**The daemon finds no devices:** compare its output with `--list`. An internal
device connected through USB may require `include_usb = yes` or a pattern.

**Permission denied in the service journal:** on SELinux systems, check the
daemon label and recent denials:

```sh
ls -Z /usr/local/bin/input-lock-x-daemon
sudo restorecon -F /usr/local/bin/input-lock-x-daemon
sudo ausearch -m avc -ts recent
```

## Development

```sh
make check    # syntax, schema, and available static checks
make test     # offline tests plus device classification
make pack     # build the GNOME extension ZIP
```

The GitHub Actions workflow runs `make check` and `make test` on pushes and pull
requests. `shellcheck` and `node` are optional for a local `make check`, but CI
requires them.

See [CHANGELOG.md](CHANGELOG.md) for the inherited release history.

The main components are:

| Path | Purpose |
| --- | --- |
| `extension/` | GNOME Shell extension |
| `daemon/` | Root daemon, service, configuration, and policy files |
| `scripts/` | User-facing control and emergency recovery commands |
| `lib/` | Installer and uninstaller helpers |
| `tests/` | Offline unit tests and device classifier |

## Credits and license

Isak Lindgren developed Input Lock X for the Chuwi Minibook X. Ayush (ayushxaw)
adapted it on 2026-11-25 with PIN unlock, automatic keep-awake, and a separate
emergency shortcut. [Upstream Input Lock X](https://github.com/isalin/input-lock-x)

Copyright (C) 2026 Isak Lindgren.

This project is licensed under the GNU General Public License, version 2 or
later. See [LICENSE](LICENSE).
