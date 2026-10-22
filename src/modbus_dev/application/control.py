import threading
import time

from ..domain.transactions import StopRequested


class Control:
    """Cooperative cancellation also observes asynchronous recording failure."""
    def __init__(self, stop=None, health_check=lambda: None):
        self.stop = stop if stop is not None else threading.Event()
        self.health_check = health_check

    def now(self):
        return time.perf_counter()

    def check(self):
        self.health_check()
        if self.stop.is_set():
            raise StopRequested()

    def wait_until(self, deadline):
        while self.now() < deadline:
            self.check()
            self.stop.wait(min(.02, max(0, deadline - self.now())))
        self.check()
