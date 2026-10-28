"""在加载边界展开旧共享测点；新配置只保存逐点区域、操作与显式编码。"""
from dataclasses import asdict, replace

from .numeric import resolve_order

LEGACY_POINT_LIMIT = 4


def migrate_points(data, usage):
    from .config import CONFIG_VERSION, MAX_POINTS, NUMERIC_TYPES, PointConfig, SourceConfig, default_point, fail, number, choice
    raw_points = data.get('points', ())
    modern = isinstance(raw_points, (list, tuple)) and (len(raw_points) > MAX_POINTS // 2 or any(
        isinstance(point, dict) and any(key in point for key in ('area', 'operation', 'readback_every'))
        for point in raw_points))
    version = data.get("schema_version", CONFIG_VERSION if modern else 3 if "points" in data else 1)
    number(version, "schema_version", 1, CONFIG_VERSION, True)
    if version == 1:
        if "points" in data:
            fail("points", "independent points require schema_version>=2")
        addresses = data.pop("addresses", [0])
        if not isinstance(addresses, (list, tuple)) or not 1 <= len(addresses) <= LEGACY_POINT_LIMIT:
            fail("addresses", f"provide 1..{LEGACY_POINT_LIMIT} legacy point addresses")
        source = SourceConfig.from_mapping(data.pop("source", {}), active=usage.source)
        source.validate(data.get("period_ms", 100), len(addresses), active=usage.source)
        raw = [dict(address=address, source=asdict(replace(source, channels=(source.channels[i],))
                    if usage.source and source.kind == "dyn" else source)) for i, address in enumerate(addresses)]
    else:
        if "addresses" in data or "source" in data:
            fail("config", "schema_version>=2 uses points instead of addresses/source")
        raw = data.pop("points", [asdict(default_point(0))])
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= MAX_POINTS:
            fail("points", f"provide 1..{MAX_POINTS} point configurations", reason="point_count", maximum=MAX_POINTS)
    legacy_slave = version < CONFIG_VERSION and usage.mode in ("rtu-slave", "tcp-slave")
    if legacy_slave and len(raw) > LEGACY_POINT_LIMIT:
        fail("points", "legacy shared banks exceed the independent-point capacity")
    points = []
    areas = ("holding", "input") if legacy_slave else (None,)
    for area in areas:
        for item in raw:
            from .config import section
            item = dict(section(item, f"points[{len(points)}]", PointConfig.__dataclass_fields__))
            item.setdefault("operation", data.get("master_operation", "write-multiple"))
            item.setdefault("readback_every", data.get("readback_every", 0))
            if area:
                item["area"] = area
            if usage.master:
                item["area"] = "input" if item["operation"] == "read-input" else "holding"
            if item.get("enabled", True) or (item.get("byte_order") is None and item.get("data_type", "float32") in NUMERIC_TYPES):
                choice(item.get("data_type", "float32"), f"points[{len(points)}].data_type", tuple(NUMERIC_TYPES))
                try:
                    item["byte_order"] = resolve_order(item.get("data_type", "float32"), item.get("byte_order"),
                                                       data.get("byte_order", "B1234"))
                except (ValueError, TypeError) as error:
                    fail(f"points[{len(points)}].byte_order", str(error))
            source_used = not usage.master or item["operation"] in ("write-single", "write-multiple")
            point = PointConfig.from_mapping(item, f"points[{len(points)}]", source_used=source_used)
            if legacy_slave and area == "holding" and point.enabled:
                # 旧波形在时刻零的快照成为保持区初始值；后续仅输入区推进。
                initial = point.source.lower if point.source.kind == "triangle" else point.source.value
                point = replace(point, source=replace(point.source, kind="constant", value=initial))
            points.append(point)
    return points


def migrate_draft_values(base, values, config):
    """迁移旧界面草稿中的共享字段；同一原始点的编辑值映射到两个新区域。"""
    from .config import CONFIG_VERSION
    if base.get('schema_version', CONFIG_VERSION) >= CONFIG_VERSION:
        return values
    result = dict(values)
    old_points = base.get('points', ())
    count = len(old_points)
    slave = base.get('mode') in ('rtu-slave', 'tcp-slave')
    shared_order = result.pop('byte_order', base.get('byte_order', 'B1234'))
    operation = result.pop('master_operation', base.get('master_operation', 'write-multiple'))
    readback = result.pop('readback_every', base.get('readback_every', 0))
    result.pop('read_function', None)
    for index, original in enumerate(old_points):
        prefix = f'points.{index}.'
        target_indices = (index, count + index) if slave else (index,)
        fields = {key[len(prefix):]: value for key, value in values.items() if key.startswith(prefix)}
        data_type = fields.get('data_type', original.get('data_type', 'float32'))
        order = fields.get('byte_order')
        if order is None:
            order = resolve_order(data_type, None, shared_order)
        for target in target_indices:
            destination = f'points.{target}.'
            for key, value in fields.items():
                result[destination + key] = value
            result[destination + 'byte_order'] = order
            result[destination + 'operation'] = operation
            result[destination + 'readback_every'] = int(readback) if isinstance(readback, (str, int)) else 0
            if slave:
                area = config.points[target].area
                result[destination + 'area'] = area
                if area == 'holding' and fields.get('enabled', original.get('enabled', True)):
                    kind = fields.get('source.kind', original.get('source', {}).get('kind', 'constant'))
                    initial = fields.get('source.lower' if kind == 'triangle' else 'source.value')
                    result[destination + 'source.kind'] = 'constant'
                    if initial is not None:
                        result[destination + 'source.value'] = initial
    return result
