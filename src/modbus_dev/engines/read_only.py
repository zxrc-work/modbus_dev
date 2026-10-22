"""Shared read-only register snapshot and protocol policy for TCP and RTU."""
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.register_message import (
    ReadHoldingRegistersRequest, ReadHoldingRegistersResponse,
    ReadInputRegistersResponse,
)

from ..domain.events import Event


class ReadOnlyRegisters:
    def __init__(self, config, control, emit):
        self.config, self.control, self.emit = config, control, emit
        self.registers = {}

    def publish(self, registers):
        # Copy on publication: a whole response always uses one snapshot.
        self.registers = dict(registers)

    def _event(self, code, **details):
        self.emit(Event(code, code, "DEBUG", "communication", details))

    def response(self, unit, pdu):
        if unit != self.config.unit:
            self._event("ignored_unit", unit=unit)
            return None
        function = pdu[0]
        exception = None
        if function not in (3, 4):
            exception = 1
        elif len(pdu) != 5:
            exception = 3
        else:
            request = ReadHoldingRegistersRequest()
            request.decode(pdu[1:])
            address, count = request.address, request.count
            snapshot = self.registers
            if not 1 <= count <= 125:
                exception = 3
            elif (count % 2 or address not in self.config.addresses
                  or any(i not in snapshot for i in range(address, address + count))):
                exception = 2
            else:
                cls = ReadHoldingRegistersResponse if function == 3 else ReadInputRegistersResponse
                response = cls(registers=[snapshot[i] for i in range(address, address + count)])
        if exception is not None:
            response = ExceptionResponse(function, exception)
        self._event("slave_request", unit=unit, function=function,
                    exception_code=exception, address=int.from_bytes(pdu[1:3], "big") if len(pdu) >= 3 else None)
        return bytes((response.function_code,)) + response.encode()

