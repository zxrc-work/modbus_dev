import math


class SyntheticSource:
    def __init__(self, config):
        if config.source.kind not in ("constant", "sine"):
            raise ValueError("Offline sources are deferred until after GUI; no device has been opened")
        self.config = config

    def sample(self, slot):
        config = self.config
        value = config.source.value
        if config.source.kind == "sine":
            phase = 2 * math.pi * config.source.frequency_hz * slot * config.period_ms / 1000
            value += config.source.amplitude * math.sin(phase)
        return tuple(value for _ in config.addresses)
