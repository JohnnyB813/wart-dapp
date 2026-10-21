"""Run blocking work outside Qt and deliver only current results to the GUI."""

from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt


class _Signals(QObject):
    finished = Signal(str, int, object, object)


class _Worker(QRunnable):
    def __init__(self, key, generation, work, cancel):
        super().__init__()
        self.key, self.generation = key, generation
        self.work, self.cancel = work, cancel
        self.signals = _Signals()

    @Slot()
    def run(self):
        try:
            result, error = self.work(self.cancel), None
        except Exception as exc:
            result, error = None, exc
        self.signals.finished.emit(self.key, self.generation, result, error)


class GuiJobs(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.current = {}
        self.workers = {}
        self.generation = 0

    def running(self, key):
        return key in self.current

    def submit(self, key, work, success, failure):
        self.cancel(key)
        self.generation += 1
        generation, cancel = self.generation, Event()
        worker = _Worker(key, generation, work, cancel)
        worker.signals.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        self.current[key] = (generation, cancel, success, failure)
        self.workers[key, generation] = worker
        self.pool.start(worker)

    def cancel(self, key):
        item = self.current.pop(key, None)
        if item:
            item[1].set()

    def cancel_all(self):
        for key in list(self.current):
            self.cancel(key)

    @Slot(str, int, object, object)
    def _finished(self, key, generation, result, error):
        self.workers.pop((key, generation), None)
        item = self.current.get(key)
        if item is None or item[0] != generation:
            return
        self.current.pop(key)
        if not item[1].is_set():
            item[3](error) if error is not None else item[2](result)
