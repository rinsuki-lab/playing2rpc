import sys

from dbus_next import Message, MessageType
from dbus_next.aio import MessageBus


NOTIFICATIONS_SERVICE = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
NOTIFICATION_APP_NAME = "playing2rpc"
NOTIFICATION_EXPIRE_TIMEOUT_MS = 5000


class DesktopNotifier:
    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus

    async def notify(self, *, summary: str, body: str) -> None:
        try:
            reply = await self._bus.call(
                Message(
                    destination=NOTIFICATIONS_SERVICE,
                    path=NOTIFICATIONS_PATH,
                    interface=NOTIFICATIONS_INTERFACE,
                    member="Notify",
                    signature="susssasa{sv}i",
                    body=[
                        NOTIFICATION_APP_NAME,
                        0,
                        "",
                        summary,
                        body,
                        [],
                        {},
                        NOTIFICATION_EXPIRE_TIMEOUT_MS,
                    ],
                )
            )
        except Exception as exc:
            print(f"failed to send desktop notification: {exc}", file=sys.stderr, flush=True)
            return

        if reply.message_type == MessageType.ERROR:
            print(
                f"failed to send desktop notification: {reply.error_name}: {reply.body!r}",
                file=sys.stderr,
                flush=True,
            )
