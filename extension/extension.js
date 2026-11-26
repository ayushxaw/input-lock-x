/* Input Lock X
 * Copyright (C) 2026 Isak Lindgren
 * SPDX-License-Identifier: GPL-2.0-or-later
 * PIN prompt and keep-awake behavior by ayushxaw, 2026-11-25.
 *
 * Locks the internal keyboard and touchpad using input-lock-x.service,
 * a root daemon that takes an exclusive EVIOCGRAB on those devices. The
 * extension starts the service and displays PIN progress reported by the
 * daemon. The daemon owns keyboard input and the release decision.
 *
 * The daemon's state file is the single source of truth for the UI. We never
 * assume we know the state -- the daemon can release itself when it sees
 * Ctrl+Esc in its own grabbed stream, and the scripts can be used over SSH.
 * Watching the file keeps the icon honest no matter who made the change.
 *
 * The daemon is not part of this extension and cannot be: it needs root. So
 * everything here has to behave sensibly when it is absent, which is exactly
 * what a fresh install from extensions.gnome.org looks like.
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {
    UNIT, STATE_PATH, KEYBINDING, REPO_URL,
    SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER,
    probeUnit, isAuthDenied, AUTH_DENIED_HINT,
    getScreenLockSettings, getScreenLockShortcuts, SCREEN_LOCK_KEYS, availableShortcuts,
} from './common.js';
import {KeepAwake} from './keepAwake.js';

// The file monitor is the primary signal; this is a cheap backstop in case an
// inotify event is ever missed and the icon is left showing the wrong state.
const POLL_INTERVAL_SECONDS = 10;

// Probing for the unit is a system-bus round trip, and unlike the state file
// the answer only changes when someone installs or removes the daemon -- which
// runs daemon-reload, which we hear about on the Reloading signal. So the poll
// probes a tenth as often, as a backstop for a unit file dropped in by hand.
const PROBE_EVERY_N_POLLS = 6;

// After asking systemd to start or stop the unit, how long to wait for the
// state file to agree before calling it a failure. The daemon can spend up to
// two seconds waiting for held modifiers to come up before it grabs.
const SETTLE_INTERVAL_MS = 300;
const SETTLE_ATTEMPTS = 20;

const ICON_UNLOCKED = 'input-keyboard-symbolic';
const ICON_LOCKED = 'changes-prevent-symbolic';
const ICON_MISSING = 'dialog-warning-symbolic';
const DAEMON_PATH = '/usr/local/bin/input-lock-x-daemon';
const DAEMON_PROTOCOL = '3';
const PROMPT_PATH = '/run/input-lock-x.auth';
const PIN_MAX_DIGITS = 12;

const InputLockButton = GObject.registerClass(
class InputLockButton extends PanelMenu.Button {
    _init(onActivate) {
        // dontCreateMenu: this is a plain toggle, a menu would just add a step
        // between a fingertip and the thing it wants to do.
        super._init(0.0, 'Input Lock X', true);

        this._onActivate = onActivate;
        this._locked = false;
        this._installed = true;

        this._icon = new St.Icon({
            icon_name: ICON_UNLOCKED,
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);
        this.add_style_class_name('input-lock-x-toggle');
    }

    // Show the recovery instructions from an available pointer or touchscreen.
    // A middle or right click leaves the status indicator alone.
    vfunc_event(event) {
        const type = event.type();
        if (type === Clutter.EventType.TOUCH_BEGIN ||
            (type === Clutter.EventType.BUTTON_PRESS &&
             event.get_button() === Clutter.BUTTON_PRIMARY)) {
            this._onActivate();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    setLocked(locked) {
        this._locked = locked;
        this.container.visible = locked;
        this._update();
    }

    setInstalled(installed) {
        this._installed = installed;
        this._update();
    }

    /* One place that turns (locked, installed) into what the panel shows.
     * "Not installed" outranks the lock state: without the daemon nothing can
     * be locked, so showing a normal keyboard icon would be a lie that only
     * comes apart when the user taps it. */
    _update() {
        if (!this._installed) {
            this._icon.icon_name = ICON_MISSING;
            this.remove_style_class_name('input-lock-x-locked');
            this.add_style_class_name('input-lock-x-missing');
            this.accessible_name = 'Input Lock X: daemon not installed';
            return;
        }

        this.remove_style_class_name('input-lock-x-missing');
        this._icon.icon_name = this._locked ? ICON_LOCKED : ICON_UNLOCKED;
        if (this._locked)
            this.add_style_class_name('input-lock-x-locked');
        else
            this.remove_style_class_name('input-lock-x-locked');

        this.accessible_name = this._locked
            ? 'Input Lock X: unlock instructions'
            : 'Keyboard and touchpad enabled';
    }
});

export default class InputLockXExtension extends Extension {
    /* The shell does not call disable() when enable() throws -- it only
     * unloads the stylesheet and logs -- so everything registered before the
     * throw leaks. The panel button is the one that bites: the shell tracks it
     * by role, and a leaked one makes the next reload die on "Extension point
     * conflict" instead of the original error, which is a far harder thing to
     * diagnose. Tear our own state down, then re-raise so the shell still
     * records the real cause. disable() is null-safe at every step, so it
     * copes with however far setup got.
     *
     * getSettings() is the realistic thrower: schemas/gschemas.compiled is
     * gitignored and install.sh symlinks this repo into the extensions dir,
     * so a tree that never ran `make schemas` has no compiled schema. */
    enable() {
        try {
            this._setUp();
        } catch (e) {
            this.disable();
            throw e;
        }
    }

    _setUp() {
        this._stateFile = Gio.File.new_for_path(STATE_PATH);
        this._promptFile = Gio.File.new_for_path(PROMPT_PATH);
        this._locked = this._readState();
        // Assume installed until the probe says otherwise, so a slow bus does
        // not flash a warning icon on every login.
        this._installed = true;
        this._pending = null;
        this._pendingId = 0;
        this._compatible = null;
        this._probeGeneration = 0;
        this._versionProcess = null;
        this._promptDialog = null;
        this._promptStatus = null;
        this._keybindingWanted = false;
        this._keybindingAdded = false;
        this._pollTick = 0;
        this._reloadId = 0;
        this._cancellable = new Gio.Cancellable();
        this._keepAwake = new KeepAwake();

        this._settings = this.getSettings();
        this._bindingSettings = new Gio.Settings({
            settings_schema: this._settings.settings_schema,
            backend: Gio.memory_settings_backend_new(),
        });
        this._screenLockSettings = getScreenLockSettings();
        this._settingsChangedId = this._settings.connect(
            `changed::${KEYBINDING}`, () => this._updateShortcuts());
        this._screenLockChangedId = this._screenLockSettings?.connect('changed', (_settings, key) => {
            if (SCREEN_LOCK_KEYS.includes(key))
                this._updateShortcuts();
        });
        this._updateShortcuts();
        // _installed is still optimistically true here, so a normal install
        // binds immediately; the probe below unbinds within milliseconds if
        // the daemon turns out to be missing.
        this._syncKeybinding();

        // The daemon replaces the PIN marker atomically on each keystroke.
        // Watch its directory so the monitor follows replacements immediately.
        try {
            this._monitor = this._stateFile.get_parent()
                .monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
            this._monitor.rate_limit = 0;
            this._monitorId = this._monitor.connect('changed', (_monitor, file, otherFile) => {
                if (file.equal(this._stateFile) || file.equal(this._promptFile) ||
                    otherFile?.equal(this._stateFile) || otherFile?.equal(this._promptFile)) {
                    this._refresh();
                }
            });
        } catch (error) {
            console.warn(`[input-lock-x] Could not monitor input state: ${error.message}`);
        }

        // Both install.sh and uninstall.sh end in daemon-reload, and that is
        // the only way the unit can appear or disappear underneath a running
        // shell. Reloading(false) means that reload has finished, so it is the
        // moment to look again. Not UnitFilesChanged: that only tracks the
        // enabled and masked lists, and this unit is deliberately static.
        this._reloadId = Gio.DBus.system.signal_subscribe(
            SYSTEMD_NAME, SYSTEMD_MANAGER, 'Reloading', SYSTEMD_PATH,
            null, Gio.DBusSignalFlags.NONE,
            (_conn, _sender, _path, _iface, _signal, params) => {
                const [active] = params.deepUnpack();
                if (active) {
                    this._compatible = null;
                    ++this._probeGeneration;
                } else {
                    this._probeInstall();
                }
            });

        const pollInterval = this._monitorId ? POLL_INTERVAL_SECONDS : 2;
        this._pollId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT_IDLE, pollInterval, () => {
                this._refresh();
                // Installing the daemon after the extension is the documented
                // path, and it does not restart the shell. The Reloading
                // subscription catches that within a second; this is only the
                // backstop for a unit file that appeared without a reload.
                // Only while missing, so the normal case costs no bus traffic.
                if ((!this._installed || this._compatible !== true) &&
                    ++this._pollTick >= PROBE_EVERY_N_POLLS) {
                    this._pollTick = 0;
                    this._probeInstall();
                }
                return GLib.SOURCE_CONTINUE;
            });

        this._addIndicator();
        this._refresh();
        this._probeInstall();
    }

    _addIndicator() {
        const button = new InputLockButton(() => this._toggle());
        try {
            Main.panel.addToStatusArea(this.uuid, button, 0, 'right');
            button.setLocked(this._locked);
            this._button = button;
        } catch (error) {
            // Another extension can break panel insertion after GNOME has
            // already stored the role. Clean up our button and keep Ctrl+Esc.
            if (Main.panel.statusArea[this.uuid] === button)
                delete Main.panel.statusArea[this.uuid];
            button.destroy();
            console.warn(`[input-lock-x] Could not add panel indicator: ${error.message}`);
        }
    }

    disable() {
        // Deliberately does not stop the daemon. A lock that survives a shell
        // restart remains recoverable through the daemon's keyboard shortcut.
        // Stopping it here would silently re-enable input on every lock screen.

        // The shell disables us on the lock screen, so an in-flight call or a
        // pending timeout would otherwise come back to a torn-down extension
        // and try to raise a notification from it.
        this._cancellable?.cancel();
        this._cancellable = null;
        this._versionProcess?.force_exit();
        this._versionProcess = null;
        this._keepAwake?.destroy();
        this._keepAwake = null;
        this._pending = null;
        this._closePrompt();

        if (this._reloadId) {
            Gio.DBus.system.signal_unsubscribe(this._reloadId);
            this._reloadId = 0;
        }

        this._clearPendingTimeout();

        if (this._pollId) {
            GLib.Source.remove(this._pollId);
            this._pollId = null;
        }

        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
            this._monitorId = 0;
        }
        this._stateFile = null;
        this._promptFile = null;

        if (this._keybindingAdded) {
            Main.wm.removeKeybinding(KEYBINDING);
            this._keybindingAdded = false;
        }
        if (this._settingsChangedId)
            this._settings.disconnect(this._settingsChangedId);
        if (this._screenLockChangedId)
            this._screenLockSettings.disconnect(this._screenLockChangedId);
        this._settingsChangedId = 0;
        this._screenLockChangedId = 0;
        this._bindingSettings = null;
        this._screenLockSettings = null;
        this._settings = null;

        this._button?.destroy();
        this._button = null;
    }

    _readState() {
        return this._stateFile.query_exists(null);
    }

    _updateShortcuts() {
        // GNOME sees just the safe bindings. Keep the user's choices on disk
        // so a custom shortcut can work again if the conflict goes away.
        const configured = this._settings.get_strv(KEYBINDING);
        const defaults = this._settings.get_default_value(KEYBINDING).deepUnpack();
        const reserved = getScreenLockShortcuts(this._screenLockSettings);
        this._bindingSettings.set_strv(KEYBINDING, availableShortcuts(configured, defaults, reserved));
    }

    /* Hold the shortcut only while there is a daemon to answer it. Ctrl+Esc is
     * a chord real applications use -- editors, IDEs, VM and RDP windows -- and
     * mutter's grab is system-wide, so binding it when the daemon is absent
     * takes the key away from everything else in exchange for a notification
     * saying we cannot help.
     *
     * Two flags, not one. _keybindingWanted is the state we last acted on, and
     * it makes this idempotent: this runs on every probe, and neither call is
     * free -- mutter warns on a duplicate add ("Trying to re-add keybinding")
     * and on a remove of a name it does not hold ("Trying to remove
     * non-existent keybinding"). _keybindingAdded is whether mutter actually
     * holds the chord, which differs from wanted when the add is refused, and
     * is what disable() needs so it does not retract a binding it never got. */
    _syncKeybinding() {
        const wanted = this._installed || this._locked;
        if (!this._bindingSettings || wanted === this._keybindingWanted)
            return;
        this._keybindingWanted = wanted;

        if (!wanted) {
            if (this._keybindingAdded) {
                Main.wm.removeKeybinding(KEYBINDING);
                this._keybindingAdded = false;
            }
            return;
        }

        const action = Main.wm.addKeybinding(
            KEYBINDING,
            this._bindingSettings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW,
            () => this._toggle());
        // Not a contended accelerator: mutter refuses on a duplicate *name*,
        // and is happy to let two bindings want the same keys. So this means
        // our own bookkeeping slipped, or a second copy of the extension is
        // loaded. Worth a line, because the symptom is a shortcut that
        // silently does nothing while the panel button keeps working.
        if (action === Meta.KeyBindingAction.NONE) {
            console.warn(`[input-lock-x] ${KEYBINDING} is already registered; the shortcut will not work`);
            return;
        }
        this._keybindingAdded = true;
    }

    /* Nothing this extension does works without the daemon, and installing
     * only the extension from extensions.gnome.org is a completely ordinary
     * way to arrive here. Say so on the icon rather than letting the first tap
     * fail with a D-Bus error nobody outside this project can read. */
    _probeInstall() {
        const cancellable = this._cancellable;
        if (!cancellable)
            return;

        const generation = ++this._probeGeneration;
        const isCurrent = () =>
            this._cancellable === cancellable && this._probeGeneration === generation;
        this._versionProcess?.force_exit();
        this._versionProcess = null;

        probeUnit(cancellable, (state, error) => {
            if (!isCurrent())
                return;

            if (error) {
                // Not a diagnosis -- leave the button usable so the real
                // failure surfaces on tap instead of a confident wrong answer.
                console.warn(`[input-lock-x] could not probe ${UNIT}: ${error.message}`);
                return;
            }

            // Only on the edge into missing. This runs on a timer, and a
            // warning repeated every minute for the life of the session is
            // noise in someone else's journal, not a diagnostic.
            const wasInstalled = this._installed;
            this._installed = state !== null;
            if (!this._installed && wasInstalled)
                console.warn(`[input-lock-x] ${UNIT} is not installed`);

            this._button?.setInstalled(this._installed);
            this._syncKeybinding();
            if (this._installed)
                this._probeProtocol(cancellable, isCurrent);
            else
                this._compatible = null;
        });
    }

    _probeProtocol(cancellable, isCurrent) {
        let process;
        try {
            process = Gio.Subprocess.new(
                [DAEMON_PATH, '--protocol-version'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            );
        } catch (error) {
            this._compatible = false;
            console.warn(`[input-lock-x] Could not run the daemon version check: ${error.message}`);
            return;
        }
        this._versionProcess = process;
        process.communicate_utf8_async(null, cancellable, (_process, result) => {
            if (!isCurrent())
                return;

            this._versionProcess = null;
            try {
                const [, version] = process.communicate_utf8_finish(result);
                this._compatible = process.get_successful() && version?.trim() === DAEMON_PROTOCOL;
            } catch (error) {
                this._compatible = false;
                console.warn(`[input-lock-x] Could not verify the daemon version: ${error.message}`);
            }
        });
    }

    /* Re-read the state file and reconcile the UI with it. Notifies only on an
     * actual transition, which also debounces the burst of events a file
     * monitor emits for a single create or delete. */
    _refresh() {
        const locked = this._readState();
        this._keepAwake?.setLocked(locked);
        this._syncPrompt(locked ? this._readPromptStatus() : null);
        if (this._pending && locked) {
            this._pending = null;
            this._clearPendingTimeout();
        }
        if (locked === this._locked)
            return;

        this._locked = locked;
        this._syncKeybinding();
        this._button?.setLocked(locked);
        Main.notify(locked ? 'Input Disabled' : 'Input Enabled');
    }

    _readPromptStatus() {
        try {
            const [, contents] = this._promptFile.load_contents(null);
            const status = new TextDecoder().decode(contents).trim();
            if (status === 'wrong')
                return status;
            const match = /^enter:([0-9]{1,2})$/.exec(status);
            return match && Number(match[1]) <= PIN_MAX_DIGITS ? Number(match[1]) : null;
        } catch (error) {
            if (!error.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.NOT_FOUND))
                console.warn(`[input-lock-x] Could not read PIN progress: ${error.message}`);
            return null;
        }
    }

    _syncPrompt(status) {
        if (status === null) {
            this._closePrompt();
            return;
        }
        if (status === this._promptStatus)
            return;

        const instruction = 'Enter your Lock X PIN, then press Enter. Press Esc to cancel.';
        const description = status === 'wrong' ? 'Incorrect PIN. Try again.' : instruction;
        if (this._promptDialog) {
            this._promptMessage.description = description;
        } else {
            const dialog = new ModalDialog.ModalDialog({
                destroyOnClose: false,
                shouldFadeIn: false,
                shouldFadeOut: false,
            });
            this._promptDialog = dialog;
            this._promptMessage = new Dialog.MessageDialogContent({
                title: 'Unlock Input Lock X',
                description,
            });
            this._promptProgress = new St.Label({
                y_align: Clutter.ActorAlign.CENTER,
            });
            dialog.contentLayout.add_child(this._promptMessage);
            dialog.contentLayout.add_child(this._promptProgress);
            try {
                if (!dialog.open()) {
                    this._closePrompt();
                    return;
                }
            } catch (error) {
                this._closePrompt();
                console.warn(`[input-lock-x] Could not open the PIN prompt: ${error.message}`);
                return;
            }
        }

        this._promptStatus = status;
        this._promptProgress.visible = typeof status === 'number' && status > 0;
        this._promptProgress.text = typeof status === 'number' ? '●'.repeat(status) : '';
        this._promptProgress.accessible_name = typeof status === 'number'
            ? `PIN digits entered: ${status}`
            : '';
    }

    _closePrompt() {
        if (this._promptDialog) {
            // Also release a native grab if opening stopped before the dialog
            // was fully shown and close() would otherwise be a no-op.
            this._promptDialog.popModal();
            this._promptDialog.close();
            this._promptDialog.destroy();
            this._promptDialog = null;
        }
        this._promptMessage = null;
        this._promptProgress = null;
        this._promptStatus = null;
    }

    _toggle() {
        const cancellable = this._cancellable;
        if (!cancellable)
            return;

        this._refresh();
        if (this._pending)
            return;

        if (this._locked) {
            if (!this._promptDialog) {
                Main.notify('Input Lock X',
                    'Press Ctrl+Esc on the locked keyboard, then enter your Lock X PIN.');
            }
            return;
        }

        if (!this._installed) {
            Main.notify('Input Lock X is not set up',
                `${UNIT} is not installed. The extension needs its companion ` +
                `daemon, which is installed separately: ${REPO_URL}`);
            return;
        }
        if (this._compatible !== true) {
            const message = this._compatible === null
                ? 'Checking the installed daemon version. Try again in a moment.'
                : 'Run make install-daemon from the Input Lock X project to update the daemon.';
            Main.notify('Input Lock X cannot start yet', message);
            return;
        }

        const request = {};
        this._pending = request;

        Gio.DBus.system.call(
            SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER,
            'StartUnit',
            new GLib.Variant('(ss)', [UNIT, 'replace']),
            null,
            Gio.DBusCallFlags.NONE,
            -1,
            cancellable,
            (connection, result) => {
                // A reply already in flight when disable() ran still lands
                // here; cancelling cannot recall it. Without this the success
                // path below would arm a timeout nothing will ever remove.
                if (this._cancellable !== cancellable || this._pending !== request)
                    return;

                try {
                    connection.call_finish(result);
                } catch (e) {
                    if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                        return;
                    this._pending = null;
                    console.error(`[input-lock-x] StartUnit failed: ${e.message}`);
                    Main.notify(...this._failureMessage(e));
                    return;
                }

                // StartUnit returns once the job is *enqueued*, not
                // once it has run -- so this success says nothing about
                // whether the daemon grabbed anything. A unit that fails to
                // start still replies successfully here. The state file is
                // the only thing that actually answers the question.
                this._refresh();
                if (this._pending === request)
                    this._awaitState(request);
            });
    }

    /* Turn a D-Bus error into something a user can act on. */
    _failureMessage(e) {
        if (isAuthDenied(e))
            return ['Not allowed to toggle the input lock', AUTH_DENIED_HINT];
        return ['Input lock toggle failed', e.message];
    }

    /* Poll until the state file agrees with what we asked for, or give up and
     * say so. Complements the file monitor: the monitor keeps the icon honest,
     * this turns a lock that never engaged into a visible failure rather than
     * a tap that silently did nothing. */
    _awaitState(request) {
        let attempts = 0;
        this._pendingId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT, SETTLE_INTERVAL_MS, () => {
                // _refresh() can complete the request and clear this source.
                this._refresh();
                if (this._pending !== request)
                    return GLib.SOURCE_REMOVE;
                if (++attempts < SETTLE_ATTEMPTS)
                    return GLib.SOURCE_CONTINUE;

                this._pendingId = 0;
                this._pending = null;
                console.error(`[input-lock-x] ${UNIT} did not engage`);
                Main.notify('Input lock failed',
                    `${UNIT} did not engage. Check journalctl -u ${UNIT}.`);
                return GLib.SOURCE_REMOVE;
            });
    }

    _clearPendingTimeout() {
        if (this._pendingId) {
            GLib.Source.remove(this._pendingId);
            this._pendingId = 0;
        }
    }
}
