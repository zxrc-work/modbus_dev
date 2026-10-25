"""单站 RTU 读写适配器；有界等待 USB 分批交付，坏帧整段丢弃。"""
import os

from pymodbus.framer.rtu import FramerRTU
from pymodbus.pdu.decoders import DecodePDU

from .rtu_registers import RtuRegisters
from .rtu import open_serial


class RtuSlaveEngine(RtuRegisters):
    MAX_FRAME = 256
    DELIVERY_GRACE_S = .01 if os.name == "nt" else 0

    def __init__(self, config, control, emit, port_factory=open_serial):
        super().__init__(config, control, emit)
        self.port_factory = port_factory
        self.port = None
        self.bound_address = config.serial.port
        self.framer = FramerRTU(DecodePDU(True))
        self.buffer = bytearray()
        self.overflow = False
        self.last_rx = None
        self.first_rx = None
        self.quiet_until = 0.0

    def connect(self):
        self.control.check()
        self.port = self.port_factory(self.config.serial)
        self.quiet_until = self.control.now() + self.config.serial.gap_ms / 1000

    def close(self):
        port, self.port = self.port, None
        self.buffer.clear()
        self.overflow = False
        self.last_rx = None
        self.first_rx = None
        if port is not None:
            port.close()

    def _quiet_deadline(self, gap):
        quiet = self.last_rx + gap
        if not self.DELIVERY_GRACE_S or not self.buffer or self.overflow:
            return quiet
        packet = self.buffer
        if packet[0] > 247:
            return quiet
        if len(packet) == 1:
            incomplete = True
        elif packet[1] in (3, 4, 6):
            incomplete = len(packet) < 8
        elif packet[1] == 16:
            incomplete = len(packet) < 7 or len(packet) < 9 + packet[6]
        else:
            incomplete = False
        # 有效 CRC 的畸形 PDU 仍由寄存器协议层返回原有异常，不能等待虚构的后续字节。
        if (incomplete and len(packet) >= 4
                and FramerRTU.check_CRC(packet[:-2], int.from_bytes(packet[-2:], "big"))):
            incomplete = False
        if incomplete:
            # 从首次交付计时，不能用持续到来的字节无限延长等待，也不能拼入下一次请求。
            quiet = max(quiet, self.first_rx + min(self.DELIVERY_GRACE_S,
                                                 self.config.timeout_ms / 1000))
        return quiet

    def poll(self, deadline):
        self.control.check()
        now = self.control.now()
        if now >= deadline:
            return
        gap = self.config.serial.gap_ms / 1000
        # Keep source-clock and cancellation latency bounded even under noise.
        until = min(deadline, now + .01)
        if self.last_rx is not None:
            until = min(until, max(now, self._quiet_deadline(gap)))
        self.port.timeout = max(0, until - now)
        data = self.port.read(min(self.MAX_FRAME, max(1, self.port.in_waiting)))
        received = self.control.now()
        self.control.check()
        if data:
            self._event("rx", port=self.bound_address, data=data.hex(), received_s=received)
            if self.last_rx is None:
                self.first_rx = received
            self.last_rx = received
            if len(self.buffer) + len(data) > self.MAX_FRAME:
                self.overflow = True
                self.buffer.clear()
            if not self.overflow:
                self.buffer.extend(data)
            return
        if self.last_rx is None or received + 1e-9 < self._quiet_deadline(gap):
            return
        packet = bytes(self.buffer)
        overflow = self.overflow
        self.buffer.clear()
        self.last_rx = None
        self.first_rx = None
        self.overflow = False
        if overflow:
            self._event("rtu_frame_rejected", reason="frame exceeds 256 bytes")
            return
        if len(packet) < 4 or not FramerRTU.check_CRC(packet[:-2], int.from_bytes(packet[-2:], "big")):
            self._event("rtu_frame_rejected", reason="short frame or invalid CRC", data=packet.hex())
            return
        # A request with an exception function code is not a master request.
        if packet[1] & 0x80:
            self._event("rtu_frame_rejected", reason="response function code", data=packet.hex())
            return
        response = self.response(packet[0], packet[1:-2])
        if response is None:
            return
        self.control.wait_until(self.quiet_until)
        self.control.check()
        frame = self.framer.encode(response, self.config.unit, 0)
        self.port.write_timeout = min(.05, self.config.timeout_ms / 1000)
        sent = self.control.now()
        count = self.port.write(frame)
        finished = self.control.now()
        self._event("tx", port=self.bound_address, data=frame[:count].hex(),
                    attempted_data=frame.hex(), sent_s=sent, returned_s=finished)
        if count != len(frame):
            raise OSError("Short RTU response write")
        # 只有驱动完整接收发送缓冲区后，实时区才显示该次 TX 数值。
        self.response_submitted(sent)
        # write() returning is not proof of physical transmission completion.
        self.quiet_until = max(finished, sent + len(frame) *
                               self.config.serial.bits_per_character / self.config.serial.baud) + gap
        self.control.check()
