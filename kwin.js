const DBUS_SERVICE = "net.rinsuki.lab.Playing2RPC";
const DBUS_PATH = "/net/rinsuki/lab/Playing2RPC/KWinWindowEvents";
const DBUS_INTERFACE = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents";
const DBUS_METHOD = "WindowAdded";

function notifyWindowAdded(window) {
    if (!window || window.deleted || !window.managed) {
        return;
    }

    const pid = Number(window.pid);
    if (!Number.isInteger(pid) || pid <= 0) {
        return;
    }

    callDBus(
        DBUS_SERVICE,
        DBUS_PATH,
        DBUS_INTERFACE,
        DBUS_METHOD,
        JSON.stringify({ pid: pid })
    );
}

workspace.windowAdded.connect(notifyWindowAdded);
