/* Input Lock X -- shared between extension.js and prefs.js
 * Copyright (C) 2026 Isak Lindgren
 * SPDX-License-Identifier: GPL-2.0-or-later
 * Project links by ayushxaw, 2026-11-25.
 *
 * The panel button and the preferences page both need to know what the unit is
 * called and whether it exists. Keeping one copy is not tidiness: the first
 * version of this had the probe written out twice, and the same wrong D-Bus
 * error name was wrong in both places.
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

export const UNIT = 'input-lock-x.service';
export const STATE_PATH = '/run/input-lock-x.state';
export const KEYBINDING = 'toggle-hid';
export const REPO_URL = 'https://github.com/ayushxaw/input-lock-x';

const SCREEN_LOCK_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys';
export const SCREEN_LOCK_KEYS = ['screensaver', 'screensaver-static'];
const RESERVED_SHORTCUTS = ['<Super>l', 'XF86ScreenSaver'];
const MODIFIERS = new Map([
    ['control', 'control'], ['primary', 'control'], ['ctrl', 'control'], ['ctl', 'control'],
    ['shift', 'shift'], ['shft', 'shift'],
    ['alt', 'alt'], ['mod1', 'alt'],
    ['super', 'super'], ['mod4', 'super'],
    ['meta', 'meta'], ['hyper', 'hyper'], ['mod2', 'mod2'],
    ['mod3', 'mod3'], ['mod5', 'mod5'],
]);

export function getScreenLockSettings() {
    const schema = Gio.SettingsSchemaSource.get_default()?.lookup(SCREEN_LOCK_SCHEMA, true);
    return schema ? new Gio.Settings({settings_schema: schema}) : null;
}

export function getScreenLockShortcuts(settings) {
    const shortcuts = [...RESERVED_SHORTCUTS];
    for (const key of SCREEN_LOCK_KEYS) {
        if (settings?.settings_schema.has_key(key))
            shortcuts.push(...settings.get_strv(key));
    }
    return shortcuts.filter(shortcut => shortcut && shortcut !== 'disabled');
}

function shortcutParts(shortcut) {
    const [prefix] = shortcut.toLowerCase().match(/^(?:<[^<>]+>)*/);
    const modifiers = new Set();
    for (const [, modifier] of prefix.matchAll(/<([^<>]+)>/g)) {
        // Mutter ignores unknown modifier tags.
        if (MODIFIERS.has(modifier))
            modifiers.add(MODIFIERS.get(modifier));
    }
    let key = shortcut.slice(prefix.length).toLowerCase();
    if (key === 'screensaver')
        key = 'xf86screensaver';
    return [[...modifiers].sort().join('+'), key];
}

export function conflictsWithScreenLock(shortcut, reserved) {
    const [mods, key] = shortcutParts(shortcut);
    return reserved.some(binding => {
        const [otherMods, otherKey] = shortcutParts(binding);
        // Raw keycodes depend on the layout. Keep the chord reserved if either
        // side uses a keycode with the same modifiers.
        return mods === otherMods &&
            (key === otherKey || key.startsWith('0x') || otherKey.startsWith('0x'));
    });
}

export function availableShortcuts(configured, defaults, reserved) {
    const allowed = configured.filter(shortcut => !conflictsWithScreenLock(shortcut, reserved));
    if (configured.length && !allowed.length)
        return defaults.filter(shortcut => !conflictsWithScreenLock(shortcut, reserved));
    return allowed;
}

export const SYSTEMD_NAME = 'org.freedesktop.systemd1';
export const SYSTEMD_PATH = '/org/freedesktop/systemd1';
export const SYSTEMD_MANAGER = 'org.freedesktop.systemd1.Manager';

/* What systemd answers GetUnitFileState with when the unit does not exist.
 * It is FileNotFound, not NoSuchUnit -- the lookup fails at the filesystem
 * before systemd has a unit to complain about; NoSuchUnit comes back from
 * StartUnit/StopUnit instead. Both are listed because that is a systemd
 * implementation detail, not a documented part of the interface, and getting
 * it wrong silently disables everything downstream of this probe. */
const NOT_INSTALLED_ERRORS = [
    'org.freedesktop.DBus.Error.FileNotFound',
    'org.freedesktop.systemd1.NoSuchUnit',
];

/* Ask systemd whether the unit is installed.
 *
 * GetUnitFileState is read-only and needs no polkit authorisation, so this
 * answers the question without a password prompt. Calls back with
 * (state, error): exactly one is non-null. `state` is systemd's enablement
 * word ("static", "disabled", ...) -- any of them means installed, which is
 * the whole question, since the unit is started on demand and must never be
 * enabled at boot.
 *
 * Only a definitive "no such unit file" counts as not installed. Any other
 * error -- bus unavailable, systemd busy -- is reported as an error rather
 * than a diagnosis, so a transient failure cannot masquerade as a missing
 * daemon. Cancelling calls back with neither.
 */
export function probeUnit(cancellable, callback) {
    Gio.DBus.system.call(
        SYSTEMD_NAME, SYSTEMD_PATH, SYSTEMD_MANAGER,
        'GetUnitFileState',
        new GLib.Variant('(s)', [UNIT]),
        null,
        Gio.DBusCallFlags.NONE,
        -1,
        cancellable,
        (connection, result) => {
            // The callback is deliberately outside the try. Calling it inside
            // would let an exception from the *consumer* -- a widget touched
            // after its window was finalized, say -- land in this catch, where
            // e.matches() on a non-GError throws, or worse, a second callback
            // fires reporting a failure that never happened.
            let state = null;
            let error = null;
            try {
                [state] = connection.call_finish(result).deepUnpack();
            } catch (e) {
                if (e.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    return;
                if (!NOT_INSTALLED_ERRORS.includes(Gio.DBusError.get_remote_error(e)))
                    error = e;
            }
            callback(state, error);
        });
}

/* True when a D-Bus failure was polkit refusing, rather than anything else.
 * On a machine where the rule was never installed -- or whose admin group is
 * not one of the three it names -- this is the failure, and its raw message
 * mentions neither polkit nor groups. */
export function isAuthDenied(e) {
    const remote = Gio.DBusError.get_remote_error(e);
    return remote === 'org.freedesktop.DBus.Error.AccessDenied' ||
        remote === 'org.freedesktop.DBus.Error.InteractiveAuthorizationRequired';
}

export const AUTH_DENIED_HINT =
    'The polkit rule is missing, or your user is not in an administrator ' +
    'group (wheel, sudo or admin). Re-running the installer usually fixes this.';
