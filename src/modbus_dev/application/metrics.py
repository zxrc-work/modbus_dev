"""Bounded 0.1 ms histogram; quantiles are bucket upper bounds, not exact samples."""
from collections import Counter
import math


class Metric:
    def __init__(self):
        self.count, self.mean, self.m2 = 0, 0., 0.
        self.minimum = self.maximum = None
        self.histogram = Counter()

    def add(self, value):
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        # At most 10001 buckets: 0..1000ms, plus a labelled overflow bucket.
        self.histogram[min(10000, max(0, math.floor(value * 10)))] += 1

    def report(self):
        result = dict(count=self.count, mean=self.mean if self.count else None,
                      min=self.minimum, max=self.maximum,
                      stddev=math.sqrt(max(0, self.m2 / self.count)) if self.count else None)
        for label, ratio in (("p95_upper_ms", .95), ("p99_upper_ms", .99)):
            seen = 0
            result[label] = None
            for bucket, count in sorted(self.histogram.items()):
                seen += count
                if seen >= math.ceil(self.count * ratio):
                    result[label] = (bucket + 1) / 10 if bucket < 10000 else ">=1000"
                    break
        return result
