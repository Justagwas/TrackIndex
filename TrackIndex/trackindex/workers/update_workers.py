from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from trackindex.core import self_updater
from trackindex.core.diagnostics import diagnostics_logger
from trackindex.core.threading_contract import assert_qt_thread_affinity

_LOG = diagnostics_logger("workers.update")


class UpdateCheckWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = Event()

    @Slot()
    def run(self) -> None:
        assert_qt_thread_affinity(self)
        try:
            self.completed.emit(self_updater.check_for_updates(stop_event=self._stop_event))
        except InterruptedError:
            pass
        except Exception as exc:
            _LOG.exception("Update check failed")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def stop(self) -> None:
        self._stop_event.set()


class UpdateInstallWorker(QObject):
    progress = Signal(float, str)
    completed = Signal(object)
    failed = Signal(str)
    canceled = Signal()
    finished = Signal()

    def __init__(self, check_data: self_updater.UpdateCheckData) -> None:
        super().__init__()
        self._check_data = check_data
        self._stop_event = Event()

    @Slot()
    def run(self) -> None:
        assert_qt_thread_affinity(self)
        try:
            prepared = self_updater.prepare_update(
                self._check_data,
                stop_event=self._stop_event,
                progress_callback=lambda value, message: self.progress.emit(value, message),
            )
            self.completed.emit(prepared)
        except InterruptedError:
            self.canceled.emit()
        except Exception as exc:
            _LOG.exception("Update preparation failed")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def stop(self) -> None:
        self._stop_event.set()
