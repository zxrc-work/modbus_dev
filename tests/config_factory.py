"""测试用当前格式构建器：把公共样例参数写入每个点，不读取或迁移旧文件。"""
from modbus_dev.domain.config import CONFIG_VERSION


def task_mapping(*, points=None, addresses=(0,), source=None, operation="write-multiple",
                 readback_every=0, byte_order=None, areas=None, **settings):
    defaults = dict(operation=operation, readback_every=readback_every)
    if source is not None:
        defaults["source"] = source
    if byte_order is not None:
        defaults["byte_order"] = byte_order
    points = [dict(address=address) for address in addresses] if points is None else points
    points = [dict(defaults, **point) for point in points]
    if areas is not None:
        points = [dict(point, area=area) for area in areas for point in points]
    return {"schema_version": CONFIG_VERSION, **settings, "points": points}
