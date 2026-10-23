"""通信点批量传输与绘图频率分离，覆盖丢包边界和确认状态更新。"""
import json
import struct
from types import SimpleNamespace
import unittest

from modbus_dev.application.live import LiveValues
from modbus_dev.domain.config import TaskConfig
from modbus_dev.domain.events import Event
from modbus_dev.domain.live_policy import CURVE_MAX_POINTS, LIVE_BATCH_MAX_SAMPLES
from modbus_dev.domain.trend import TrendHistory
from modbus_dev.worker import live_packets


def settings():
    return TaskConfig.from_mapping(dict(mode="rtu-slave", serial=dict(port="TEST")))


def value_event(at, value, *, direction="tx"):
    return Event("register_values", "value", "DEBUG", details=dict(
        area="input", direction=direction, address=0, function=4,
        registers=struct.unpack(">HH", struct.pack(">f", value)),
        sampled_s=at, quality="submitted" if direction == "tx" else "received"))


class BufferedLiveTests(unittest.TestCase):
    def test_ten_hz_points_keep_original_times_and_survive_latest_value_replacement(self):
        live, history = LiveValues(settings()), TrendHistory()
        for batch in range(50):
            for index in range(2):
                number = batch * 2 + index
                live.consume(value_event(number / 10, number))
            snapshot = live.snapshot((batch * 2 + 1) / 10, "running", drain=True)
            history.sample(snapshot, snapshot["sampled_s"])
        points = list(history.series["input:0:tx"])
        self.assertEqual(points, [(i / 10, float(i)) for i in range(100)])
        self.assertEqual(snapshot["rows"][0]["value"], 99.)
        history.sample(live.snapshot(9.95, "running", drain=True), 9.95)
        self.assertEqual(list(history.series["input:0:tx"]), points)
        self.assertEqual(history.dropped, 0)

    def test_worker_takes_points_at_send_time_and_flushes_final_batches(self):
        live = LiveValues(settings())
        total = 2 * LIVE_BATCH_MAX_SAMPLES + 7
        for i in range(total):
            live.consume(value_event(i / 1000, i))
        runner = SimpleNamespace(live=live, state="running", counters={},
                                 control=SimpleNamespace(now=lambda: 3.))
        # 多个旧状态可以被替换；构造状态不能提前取走通信点。
        statuses = [dict(schema_version=1, type="status", state="running", events=[]) for _ in range(4)]
        first = list(live_packets(statuses[-1], runner))
        runner.state = "stopped"
        final = list(live_packets(dict(schema_version=1, type="result", result=dict(state="stopped")), runner))
        packets = first + final
        self.assertEqual([p["type"] for p in packets], ["status", "status", "result"])
        self.assertEqual([s[0] for p in packets for s in p["live"]["samples"]], list(range(1, total + 1)))
        self.assertEqual(packets[-1]["live"]["samples_pending"], 0)
        for packet in packets:
            self.assertLess(len(json.dumps(packet, ensure_ascii=True).encode("ascii")), 262144)

    def test_overflow_is_counted_and_a_line_cannot_bridge_the_missing_points(self):
        live, history = LiveValues(settings(), capacity=3), TrendHistory()
        live.consume(value_event(0., 0.))
        history.sample(live.snapshot(0., "running", drain=True), 0.)
        for i in range(1, 6):
            live.consume(value_event(i / 10, i))
        snapshot = live.snapshot(.5, "running", drain=True)
        self.assertEqual(snapshot["samples_dropped"], 2)
        self.assertEqual([s[3] for s in snapshot["samples"]], [3., 4., 5.])
        history.sample(snapshot, .5)
        self.assertEqual(list(history.series["input:0:tx"]),
                         [(0., 0.), (.3, None), (.3, 3.), (.4, 4.), (.5, 5.)])
        self.assertEqual(history.dropped, 2)

    def test_confirmation_changes_existing_point_without_inventing_new_samples(self):
        live, history = LiveValues(settings()), TrendHistory()
        live.consume(value_event(0., 1.25))
        history.sample(live.snapshot(0., "running", drain=True), 0.)
        live.consume(Event("register_confirmation", "ack", "DEBUG", details=dict(
            area="input", direction="tx", address=0, count=2, quality="ambiguous")))
        history.sample(live.snapshot(.1, "running", drain=True), .1)
        self.assertEqual(list(history.series["input:0:tx"]), [(0., None)])
        live.consume(value_event(.2, 2.5))
        history.sample(live.snapshot(.2, "running", drain=True), .2)
        self.assertEqual(list(history.series["input:0:tx"]), [(0., None), (.2, 2.5)])

    def test_time_window_capacity_and_stopped_final_batch(self):
        live = LiveValues(settings())
        live.consume(value_event(0., 1.))
        live.consume(value_event(12., 2.))
        snapshot = live.snapshot(12., "stopped", drain=True)
        self.assertEqual(snapshot["samples_dropped"], 1)
        history = TrendHistory()
        history.sample(snapshot, 12.)
        self.assertEqual(list(history.series["input:0:tx"]), [(12., 2.), (12., None)])
        samples = [[i + 1, "input:0:tx", i / 10000, 1., "submitted"]
                   for i in range(CURVE_MAX_POINTS + 5)]
        history.clear()
        history.sample(dict(state="running", samples=samples), 1.1)
        self.assertEqual(len(history.series["input:0:tx"]), CURVE_MAX_POINTS)
        self.assertEqual(history.dropped, 5)
        history.advance(13., "running")
        self.assertFalse(history.series["input:0:tx"])

    def test_planned_source_values_never_enter_the_communication_buffer(self):
        live = LiveValues(settings())
        live.consume(Event("source_sample", "plan", "DEBUG", details=dict(values=[1.25])))
        snapshot = live.snapshot(0., "running", drain=True)
        self.assertEqual(snapshot["rows"], [])
        self.assertEqual(snapshot["samples"], [])
