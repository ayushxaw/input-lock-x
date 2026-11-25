/* Input Lock X
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const SESSION_MANAGER = 'org.gnome.SessionManager';
const SESSION_PATH = '/org/gnome/SessionManager';
const INHIBIT_SUSPEND = 4;
const INHIBIT_IDLE = 8;

export class KeepAwake {
    _opening = null;
    _connection = null;
    _managerWatch = 0;
    _managerOwner = null;

    setLocked(locked) {
        if (!locked) {
            this._disconnect();
            return;
        }

        if (this._connection?.is_closed())
            this._disconnect();
        if (this._connection || this._opening)
            return;

        let address;
        try {
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, null);
        } catch (error) {
            console.warn(`[input-lock-x] Could not find the session bus: ${error.message}`);
            return;
        }

        const opening = new Gio.Cancellable();
        this._opening = opening;
        const flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION;
        Gio.DBusConnection.new_for_address(address, flags, null, opening, (_source, result) => {
            let connection;
            try {
                connection = Gio.DBusConnection.new_for_address_finish(result);
            } catch (error) {
                if (this._opening === opening) {
                    this._opening = null;
                    console.warn(`[input-lock-x] Could not open the session bus: ${error.message}`);
                }
                return;
            }

            if (this._opening !== opening) {
                connection.close(null, null);
                return;
            }

            this._opening = null;
            this._connection = connection;
            this._managerWatch = Gio.bus_watch_name_on_connection(
                connection, SESSION_MANAGER, Gio.BusNameWatcherFlags.NONE,
                (_bus, _name, owner) => this._inhibit(connection, owner),
                () => {
                    if (this._connection === connection)
                        this._managerOwner = null;
                });
        });
    }

    destroy() {
        this._disconnect();
    }

    _inhibit(connection, owner) {
        if (this._connection !== connection)
            return;

        this._managerOwner = owner;
        const parameters = new GLib.Variant('(susu)', [
            'input-lock-x', 0, 'Keyboard and pointer input are locked',
            INHIBIT_IDLE | INHIBIT_SUSPEND,
        ]);
        connection.call(owner, SESSION_PATH, SESSION_MANAGER, 'Inhibit',
            parameters, new GLib.VariantType('(u)'), Gio.DBusCallFlags.NONE, 5000,
            null, (bus, result) => {
                try {
                    bus.call_finish(result);
                } catch (error) {
                    if (this._connection !== connection || this._managerOwner !== owner)
                        return;
                    console.warn(`[input-lock-x] Could not keep the display awake: ${error.message}`);
                    this._disconnect();
                }
            });
    }

    _disconnect() {
        this._opening?.cancel();
        this._opening = null;
        if (this._managerWatch) {
            Gio.bus_unwatch_name(this._managerWatch);
            this._managerWatch = 0;
        }

        // GNOME removes inhibitors when their sender disconnects. Closing a
        // dedicated connection covers an unlock before Inhibit replies, too.
        const connection = this._connection;
        this._connection = null;
        this._managerOwner = null;
        if (connection && !connection.is_closed())
            connection.close(null, null);
    }
}
