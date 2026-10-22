"""Platform timing policy, borrowed from the standalone RTU sender."""
import os


class WindowsTimer:
    def __init__(self):
        self.api = None
        self.status = "not_windows"

    def __enter__(self):
        if os.name == "nt":
            import ctypes
            try:
                api = ctypes.WinDLL("winmm")
                code = api.timeBeginPeriod(1)
                self.status = "requested_1ms" if code == 0 else f"request_failed_{code}"
                if code == 0:
                    self.api = api
            except OSError as error:
                self.status = f"unavailable: {error}"
        return self

    def __exit__(self, *_):
        if self.api is not None:
            self.api.timeEndPeriod(1)
