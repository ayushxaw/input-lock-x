import Gio from 'gi://Gio';
import {availableShortcuts, getScreenLockShortcuts} from '../extension/common.js';

const defaults = ['<Control>Escape'];
const reserved = getScreenLockShortcuts(null);
const cases = [
    ['Win+L from an existing install', ['<Super>l'], reserved, defaults],
    ['alternate spelling and case', ['<Mod4>L'], reserved, defaults],
    ['raw keycode', ['<Super>0x2e'], reserved, defaults],
    ['dedicated lock key', ['ScreenSaver'], reserved, defaults],
    ['keep a second binding', ['<Super>l', '<Alt>F8'], reserved, ['<Alt>F8']],
    ['disabled shortcut', [], reserved, []],
    ['custom GNOME binding', ['<Shift><Control>F8'], [...reserved, '<Ctrl><Shft>f8'], defaults],
    ['GNOME also owns the fallback', defaults, [...reserved, '<Primary>Escape'], []],
];

for (const [description, configured, screenLock, expected] of cases) {
    const actual = availableShortcuts(configured, defaults, screenLock);
    if (JSON.stringify(actual) !== JSON.stringify(expected))
        throw new Error(`${description}: expected [${expected}], got [${actual}]`);
}

const original = new Gio.Settings({schema_id: 'org.gnome.desktop.session'});
const isolated = new Gio.Settings({
    settings_schema: original.settings_schema,
    backend: Gio.memory_settings_backend_new(),
});
const before = original.get_uint('idle-delay');
const replacement = before === 1 ? 2 : 1;
isolated.set_uint('idle-delay', replacement);
if (isolated.get_uint('idle-delay') !== replacement || original.get_uint('idle-delay') !== before)
    throw new Error('Effective shortcuts need a separate settings backend');

print('Screen lock shortcut protection: ok');
