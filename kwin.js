const DBUS_SERVICE = "net.rinsuki.lab.Playing2RPC";
const DBUS_PATH = "/net/rinsuki/lab/Playing2RPC/KWinWindowEvents";
const DBUS_INTERFACE = "net.rinsuki.lab.Playing2RPC.KWinWindowEvents";
const DBUS_METHOD_WINDOW_ADDED = "WindowAdded";
const DBUS_METHOD_WINDOW_CLOSED = "WindowClosed";

function pidFromWindow(window) {
    if (!window) {
        return null;
    }

    const pid = Number(window.pid);
    if (!Number.isInteger(pid) || pid <= 0) {
        return null;
    }

    return pid;
}

function managedWindowPid(window) {
    if (!window || window.deleted || !window.managed) {
        return null;
    }

    return pidFromWindow(window);
}

function windowKey(window) {
    if (!window) {
        return null;
    }

    if (window.internalId !== undefined && window.internalId !== null) {
        return String(window.internalId);
    }

    return null;
}

function hasManagedWindowForPid(pid, removedWindow) {
    const removedWindowKey = windowKey(removedWindow);
    const windows = workspace.stackingOrder;

    for (let index = 0; index < windows.length; index += 1) {
        const window = windows[index];
        const candidatePid = managedWindowPid(window);
        if (candidatePid !== pid) {
            continue;
        }

        if (window === removedWindow) {
            continue;
        }

        const candidateWindowKey = windowKey(window);
        if (removedWindowKey !== null && candidateWindowKey === removedWindowKey) {
            continue;
        }

        return true;
    }

    return false;
}

function sendWindowEvent(method, pid) {
    callDBus(
        DBUS_SERVICE,
        DBUS_PATH,
        DBUS_INTERFACE,
        method,
        JSON.stringify({ pid: pid })
    );
}

function notifyWindowAdded(window) {
    const pid = managedWindowPid(window);
    if (pid === null) {
        return;
    }

    sendWindowEvent(DBUS_METHOD_WINDOW_ADDED, pid);
}

function notifyWindowRemoved(window) {
    const pid = pidFromWindow(window);
    if (pid === null || hasManagedWindowForPid(pid, window)) {
        return;
    }

    sendWindowEvent(DBUS_METHOD_WINDOW_CLOSED, pid);
}

workspace.windowAdded.connect(notifyWindowAdded);
workspace.windowRemoved.connect(notifyWindowRemoved);
