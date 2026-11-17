from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class _Bridge(QObject):
    finished = Signal(object, object)  # (result, error)


def run_async(
    owner: QObject,
    work: Callable[[], Any],
    on_done: Callable[[Any, BaseException | None], None],
) -> None:
    """Run ``work`` on a daemon thread and deliver the result on the GUI thread.

    ``owner`` keeps the bridge alive and ties the callback's lifetime to the widget that asked
    for the work, so nothing fires after that widget is destroyed.
    """

    bridge = _Bridge(owner)

    def deliver(result: Any, error: object) -> None:
        bridge.deleteLater()
        on_done(result, error if isinstance(error, BaseException) else None)

    bridge.finished.connect(deliver)

    def runner() -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            result = work()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            error = exc
        try:
            bridge.finished.emit(result, error)
        except RuntimeError:
            # Owner was destroyed while the work was running.
            pass

    threading.Thread(target=runner, daemon=True).start()
