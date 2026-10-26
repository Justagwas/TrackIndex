from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PySide6.QtCore import QEvent, QObject, QTimer

from trackindex.core.diagnostics import diagnostics_logger
from trackindex.core.process_exit import emergency_exit

_LOG = diagnostics_logger("shutdown")


class ShutdownState(Enum):
    RUNNING = "running"
    STOP_REQUESTED = "stop requested"
    DRAINING = "draining"
    FINALIZING = "finalizing"
    COMPLETING = "completing exit"
    READY = "ready to exit"


class ShutdownCoordinator(QObject):
    def __init__(self, app, window, controller, playback) -> None:
        super().__init__(app)
        self.app, self.window, self.controller, self.playback = app, window, controller, playback
        self.state = ShutdownState.RUNNING
        self._handoff: Callable[[], None] | None = None
        self._discard: Callable[[], None] | None = None
        self._handoff_failed: Callable[[str], None] | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._poll)
        self._closing_notice_timer = QTimer(self)
        self._closing_notice_timer.setSingleShot(True)
        self._closing_notice_timer.setInterval(30_000)
        self._closing_notice_timer.timeout.connect(self._show_closing_notice)
        app.setQuitOnLastWindowClosed(False)
        app.installEventFilter(self)
        app.aboutToQuit.connect(self._emergency_teardown)
        app.shutdown_coordinator = self

    def eventFilter(self, watched, event) -> bool:
        if self.state is ShutdownState.READY:
            return False
        if ((watched is self.app and event.type() == QEvent.Type.Quit)
                or (watched is self.window and event.type() == QEvent.Type.Close)):
            event.ignore()
            self.request()
            return True
        return False

    def request(self, handoff=None, discard=None, *, handoff_failed=None) -> None:
        if self.state is not ShutdownState.RUNNING:
            if discard is not None:
                discard()
            return
        self.state = ShutdownState.STOP_REQUESTED
        self._handoff, self._discard = handoff, discard
        self._handoff_failed = handoff_failed
        self.window.set_closing_status()
        self._closing_notice_timer.start()
        self.controller.shutdown()
        self.playback.shutdown()
        self.state = ShutdownState.DRAINING
        self.timer.start()

    def _show_closing_notice(self) -> None:
        if self.state in {ShutdownState.STOP_REQUESTED, ShutdownState.DRAINING, ShutdownState.FINALIZING}:
            self.window.set_closing_status(
                "Still closing. If TrackIndex does not finish, you can end it in Task Manager.",
            )

    def _poll(self) -> None:
        if self.state is ShutdownState.DRAINING:
            if not self.controller.shutdown_drained() or not self.playback.shutdown_drained():
                return
            self.state = ShutdownState.FINALIZING
            self.controller.begin_shutdown_finalization()
        if self.state is ShutdownState.FINALIZING:
            if not self.controller.shutdown_finalized():
                return
            self._complete_exit()

    def _complete_exit(self) -> None:
        self.state = ShutdownState.COMPLETING
        self.timer.stop()
        self._closing_notice_timer.stop()
        handoff, discard, failed = self._handoff, self._discard, self._handoff_failed
        self._handoff = self._discard = self._handoff_failed = None
        if not self.playback.persist_shutdown_state():
            self.controller._shutdown_settled = False
        error = ""
        try:
            if handoff is not None and self.controller._shutdown_settled:
                try:
                    handoff()
                except Exception as exc:
                    _LOG.exception("Installer handoff failed after shutdown settlement")
                    error = str(exc) or "The update installer could not be started."
            elif handoff is not None:
                error = "TrackIndex could not finish closing safely. The update installer was not started."
            if discard is not None and (handoff is None or error):
                try:
                    discard()
                except Exception:
                    _LOG.exception("Prepared update cleanup failed")
            if error and handoff is not None:
                from trackindex.update_handoff import abort_pending_update
                abort_pending_update()
            if error and failed is not None:
                try:
                    failed(error)
                except Exception:
                    _LOG.exception("Installer handoff failure could not be displayed")
        finally:
            self.state = ShutdownState.READY
            self.app.quit()

    def _emergency_teardown(self) -> None:
        if self.state is not ShutdownState.READY:
            emergency_exit()
