"""TCP 与 RTU 共用独立寄存器区；保持区仅初始化，输入区持续更新。"""
import struct

from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.register_message import (
    ReadHoldingRegistersResponse, ReadInputRegistersResponse,
    WriteSingleRegisterResponse, WriteMultipleRegistersResponse,
)

from .read_only import ReadOnlyRegisters


class RegisterBanks(ReadOnlyRegisters):
    def __init__(self, config, control, emit):
        super().__init__(config, control, emit)
        self.holding = {}
        self._holding_initialized = False
        self._pending_values = None

    def publish(self, registers):
        if "holding" in registers or "input" in registers:
            banks = registers
        else:
            # 兼容旧适配器测试的扁平快照，但只发布到实际配置的区域。
            banks = {area: {address: value for point in self.config.enabled_points if point.area == area
                           for address in range(point.address, point.address + point.register_count)
                           if (value := registers.get(address)) is not None}
                     for area in ("holding", "input")}
        self.registers = dict(banks.get("input", {}))
        if not self._holding_initialized:
            self.holding = dict(banks.get("holding", {}))
            self._holding_initialized = True

    def response(self, unit, pdu):
        self._pending_values = None
        broadcast = unit == 0 and self.config.mode == "rtu-slave"
        if unit != self.config.unit and not broadcast:
            self._event("ignored_unit", unit=unit)
            return None
        function = pdu[0]
        if broadcast and function not in (6, 16):
            self._event("ignored_unit", unit=unit)
            return None
        exception, address, count, words = None, None, None, ()
        area = "input" if function == 4 else "holding"
        if function not in (3, 4, 6, 16):
            exception = 1
        elif function in (6, 16) and self.config.access_mode == "read-only":
            exception = 1
        elif function in (3, 4, 6):
            if len(pdu) != 5:
                exception = 3
            else:
                address, value = struct.unpack(">HH", pdu[1:])
                count = 1 if function == 6 else value
                words = (value,) if function == 6 else ()
                if function in (3, 4) and not 1 <= count <= 125:
                    exception = 3
        elif len(pdu) < 6:
            exception = 3
        else:
            address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= 123 or byte_count != count * 2 or len(pdu) != 6 + byte_count:
                exception = 3
            else:
                words = struct.unpack(">" + "H" * count, pdu[6:])
        bank = self.registers if area == "input" else self.holding
        if exception is None and (address + count > 65536 or
                                  any(index not in bank for index in range(address, address + count))):
            exception = 2
        if exception is None and function in (3, 4) and self.config.mode == "tcp-slave":
            points = [point for point in self.config.enabled_points if point.area == area]
            if (address not in {point.address for point in points}
                    or address + count not in {point.address + point.register_count for point in points}):
                exception = 2
        if exception is None:
            if function in (3, 4):
                words = tuple(bank[index] for index in range(address, address + count))
                cls = ReadInputRegistersResponse if function == 4 else ReadHoldingRegistersResponse
                response = cls(registers=list(words))
                self._pending_values = dict(area=area, address=address, registers=words, function=function)
            else:
                # 先验证整个范围再一次性替换，非法 FC16 不能造成部分写入。
                updated = dict(self.holding)
                updated.update({address + index: word for index, word in enumerate(words)})
                self.holding = updated
                self._event("register_values", area="holding", direction="rx", address=address,
                            registers=words, function=function, quality="received",
                            sampled_s=self.control.now())
                response = (WriteSingleRegisterResponse(address=address, registers=list(words)) if function == 6 else
                            WriteMultipleRegistersResponse(address=address, count=count))
        else:
            response = ExceptionResponse(function, exception)
        self._event("slave_request", unit=unit, function=function, exception_code=exception,
                    address=address, count=count, broadcast=broadcast)
        if broadcast:
            # 广播可执行合法写入，但正常或异常都不得返回报文。
            self._event("broadcast_processed", function=function, exception_code=exception)
            return None
        return bytes((response.function_code,)) + response.encode()

    def response_submitted(self, sampled_s):
        if self._pending_values is not None:
            self._event("register_values", direction="tx", quality="submitted", sampled_s=sampled_s,
                        **self._pending_values)
        self._pending_values = None
