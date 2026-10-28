"""验证独立测点、旧版配置迁移、禁用语义与每点波形校验。"""
import json
import unittest

from modbus_dev.application.session import check_capabilities
from modbus_dev.domain.config import MAX_POINTS, TaskConfig
from modbus_dev.domain.events import ConfigurationError
from modbus_dev.domain.mapping import write_requests
from modbus_dev.domain.wire import minimum_cycle_ms
from modbus_dev.sources.synthetic import SyntheticSource


def configuration(points, **options):
    return TaskConfig.from_mapping(dict(schema_version=4, mode="tcp-slave", points=points, **options))


class PointTests(unittest.TestCase):
    def test_all_eight_points_can_be_enabled_with_distinct_values(self):
        values = (1.25, -3.5, 12.5, 25, 30, 40, 50, 60)
        config = configuration([dict(address=i * 2, source=dict(value=value))
                                for i, value in enumerate(values)])
        self.assertEqual(len(config.enabled_points), MAX_POINTS)
        self.assertEqual(SyntheticSource(config).sample(0), values)
        self.assertEqual(SyntheticSource(config).sample(100), values)

    def test_legacy_config_expands_shared_source_and_saves_only_new_schema(self):
        config = TaskConfig.from_mapping(dict(schema_version=1, mode="tcp-slave", addresses=[6, 0],
                                             source=dict(kind="sine", value=10, amplitude=2, frequency_hz=1)))
        self.assertEqual(config.schema_version, 4)
        self.assertEqual(len(config.points), MAX_POINTS)
        self.assertEqual(config.addresses, (6, 0, 6, 0))
        self.assertEqual(config.points[0].source, config.points[1].source)
        self.assertEqual(config.points[2].area, "input")
        self.assertFalse(config.points[4].enabled)
        data = json.loads(json.dumps(config.to_mapping()))
        self.assertNotIn("addresses", data)
        self.assertNotIn("source", data)
        self.assertEqual(TaskConfig.from_mapping(data), config)

    def test_unsorted_points_independently_sample_the_same_clock(self):
        config = configuration([
            dict(address=6, source=dict(value=-3.5)),
            dict(address=0, source=dict(kind="sine", value=10, amplitude=2, frequency_hz=1)),
            dict(enabled=False, address=0, source=dict(value=999)),
            dict(address=2, source=dict(kind="sine", value=-1, amplitude=4, frequency_hz=.5))])
        source = SyntheticSource(config)
        self.assertEqual(source.sample(0), (-3.5, 10, -1))
        values = source.sample(5)
        self.assertAlmostEqual(values[0], -3.5)
        self.assertAlmostEqual(values[1], 10)
        self.assertAlmostEqual(values[2], 3)
        requests = write_requests(config.addresses, source.sample(0), config.byte_order)
        self.assertEqual([(r.address, r.count) for r in requests], [(0, 4), (6, 2)])
        self.assertEqual(requests[0].registers, (0x4120, 0, 0xbf80, 0))
        self.assertEqual(requests[1].registers, (0xc060, 0))

    def test_disabled_point_does_not_overlap_sample_or_affect_wire_budget(self):
        config = configuration([dict(address=0), dict(enabled=False, address=0,
            source=dict(kind="sine", frequency_hz=100))])
        self.assertEqual(config.addresses, (0,))
        self.assertEqual(SyntheticSource(config).sample(1), (0,))
        self.assertEqual(minimum_cycle_ms(config), minimum_cycle_ms(configuration([dict(address=0)])))
        # 未实现的数据源只在启用时拒绝，不阻塞预留的禁用槽位。
        check_capabilities(configuration([dict(address=0), dict(enabled=False, source=dict(kind="dyn"))]))

    def test_point_count_activation_types_ranges_and_overlaps_are_checked(self):
        cases = [[], [{}] * (MAX_POINTS + 1), [dict(enabled=False)], [dict(enabled=1)],
                 [dict(address=True)], [dict(address=65535)], [dict(address=-1)], [dict(name=1)],
                 [dict(address=1), dict(address=2)], [dict(address=4), dict(address=4)],
                 [dict(adress=0)], [dict(source=dict(amplitdue=1))], [None], "0,2"]
        for points in cases:
            with self.subTest(points=points), self.assertRaises(ConfigurationError):
                configuration(points)
        self.assertEqual(configuration([dict(address=65534)]).addresses, (65534,))
        with self.assertRaisesRegex(ConfigurationError, "测点 1 与测点 3"):
            configuration([dict(address=8), dict(enabled=False), dict(address=9)])

    def test_signal_validation_reports_the_specific_point(self):
        for source in (dict(kind="sine", frequency_hz=5),
                       dict(kind="sine", value=3e38, amplitude=3e38),
                       dict(value=float("nan"))):
            with self.subTest(source=source), self.assertRaisesRegex(ConfigurationError, r"points\[1\].source"):
                configuration([dict(address=0), dict(address=4, source=source)])

    def test_schema_mixing_and_unsupported_versions_are_rejected(self):
        cases = [dict(schema_version=1, points=[{}]), dict(schema_version=2, addresses=[0]),
                 dict(schema_version=2, source={}), dict(points=[{}], source={}),
                 dict(schema_version=5), dict(period_ms="100"), dict(period_ms=0)]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ConfigurationError):
                TaskConfig.from_mapping(dict(mode="tcp-slave", **data))

    def test_each_enabled_source_is_capability_checked_before_opening_devices(self):
        config = configuration([dict(address=0), dict(address=2, source=dict(
            kind="dyn", path="not-opened.dyn", channels=["P"]))])
        with self.assertRaisesRegex(ValueError, "constant/sine"):
            check_capabilities(config)


if __name__ == "__main__":
    unittest.main()
