/* Run with dbus-run-session -- gjs -m tests/test-keep-awake.js.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {KeepAwake} from '../extension/keepAwake.js';

const NAME = 'org.gnome.SessionManager';
const PATH = '/org/gnome/SessionManager';
const DBUS = 'org.freedesktop.DBus';
const DBUS_PATH = '/org/freedesktop/DBus';

const sessionBus = Gio.DBus.session;
const requests = [];
const disconnected = new Set();

function assert(condition, message) {
    if (!condition)
        throw new Error(message);
}

function waitFor(predicate, message) {
    const deadline = GLib.get_monotonic_time() + 2 * GLib.USEC_PER_SEC;
    return new Promise((resolve, reject) => {
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 10, () => {
            if (predicate()) {
                resolve();
                return GLib.SOURCE_REMOVE;
            }
            if (GLib.get_monotonic_time() < deadline)
                return GLib.SOURCE_CONTINUE;
            reject(new Error(message));
            return GLib.SOURCE_REMOVE;
        });
    });
}

const managerInterface = `
    <node>
        <interface name="${NAME}">
            <method name="Inhibit">
                <arg type="s" direction="in"/>
                <arg type="u" direction="in"/>
                <arg type="s" direction="in"/>
                <arg type="u" direction="in"/>
                <arg type="u" direction="out"/>
            </method>
        </interface>
    </node>`;

function exportManager(connection) {
    const result = connection.call_sync(DBUS, DBUS_PATH, DBUS, 'RequestName',
        new GLib.Variant('(su)', [NAME, 4]), // DO_NOT_QUEUE
        new GLib.VariantType('(u)'), Gio.DBusCallFlags.NONE, 1000, null);
    assert(result.deepUnpack()[0] === 1, 'Run this test on a private session bus.');

    const manager = Gio.DBusExportedObject.wrapJSObject(managerInterface, {
        InhibitAsync([appId, _unused, reason, flags], invocation) {
            requests.push({appId, reason, flags, sender: invocation.get_sender(), invocation});
        },
    });
    manager.export(connection, PATH);
    return manager;
}

const manager = exportManager(sessionBus);

const watcher = sessionBus.signal_subscribe(DBUS, DBUS, 'NameOwnerChanged',
    DBUS_PATH, null, Gio.DBusSignalFlags.NONE,
    (_bus, _sender, _path, _iface, _signal, params) => {
        const [name, _oldOwner, newOwner] = params.deepUnpack();
        if (!newOwner)
            disconnected.add(name);
    });

const keepAwake = new KeepAwake();
let replacementBus;
let replacementManager;
try {
    keepAwake.setLocked(true);
    keepAwake.setLocked(false);
    keepAwake.setLocked(true);
    keepAwake.setLocked(true);
    await waitFor(() => requests.length > 0, 'No idle inhibitor request arrived.');

    const first = requests[0];
    assert(first.appId === 'input-lock-x', 'Unexpected inhibitor application.');
    assert(first.reason.length > 0, 'Inhibitor description is missing.');
    assert(first.flags === 12, 'Expected both idle and suspend inhibition.');
    assert(first.sender !== sessionBus.get_unique_name(), 'Inhibitor uses the shared bus.');

    keepAwake.setLocked(false);
    await waitFor(() => disconnected.has(first.sender),
        'Unlock did not disconnect while Inhibit was pending.');

    // The old request can finish even after the user has locked again.
    keepAwake.setLocked(true);
    first.invocation.return_value(new GLib.Variant('(u)', [1]));
    await waitFor(() => requests.length > 1, 'Relock did not request a new inhibitor.');
    const second = requests[1];
    assert(second.sender !== first.sender, 'Relock reused the closed inhibitor bus.');

    sessionBus.call_sync(DBUS, DBUS_PATH, DBUS, 'ReleaseName',
        new GLib.Variant('(s)', [NAME]), new GLib.VariantType('(u)'),
        Gio.DBusCallFlags.NONE, 1000, null);
    replacementBus = Gio.DBusConnection.new_for_address_sync(
        Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, null),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
        Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, null, null);
    replacementManager = exportManager(replacementBus);
    await waitFor(() => requests.length > 2, 'The replacement manager was not inhibited.');

    const third = requests[2];
    assert(third.sender === second.sender, 'A manager change reopened the lock connection.');
    second.invocation.return_dbus_error('org.gnome.SessionManager.Error.Failed', 'Old manager');
    third.invocation.return_value(new GLib.Variant('(u)', [3]));
    const connection = keepAwake._connection;
    await new Promise((resolve, reject) => {
        connection.call(DBUS, DBUS_PATH, DBUS, 'GetId', null, null,
            Gio.DBusCallFlags.NONE, 1000, null, (bus, result) => {
                try {
                    bus.call_finish(result);
                    resolve();
                } catch (error) {
                    reject(error);
                }
            });
    });
    assert(keepAwake._connection === connection,
        'A late failure from the previous manager tore down the new inhibitor.');

    keepAwake.destroy();
    await waitFor(() => disconnected.has(third.sender),
        'Extension disable did not disconnect the inhibitor bus.');
    assert(requests.length === 3, 'Repeated state refresh sent duplicate inhibitor requests.');
    print('Keep awake lifecycle: ok');
} finally {
    keepAwake.destroy();
    replacementManager?.unexport();
    replacementBus?.close_sync(null);
    sessionBus.signal_unsubscribe(watcher);
    manager.unexport();
}
