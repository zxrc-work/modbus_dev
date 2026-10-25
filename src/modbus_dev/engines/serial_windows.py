"""Windows 串口仅在线路参数改变时重配 DCB，读写超时单独更新。"""
import ctypes

import serial


class TimeoutSerial(serial.Serial):
    def _uart_settings(self):
        settings = self.get_settings()
        values = tuple((key, value) for key, value in sorted(settings.items())
                       if key not in {"timeout", "write_timeout", "inter_byte_timeout"})
        mode = self.rs485_mode
        rs485 = None if mode is None else (
            mode.rts_level_for_tx, mode.rts_level_for_rx,
            mode.delay_before_tx, mode.delay_before_rx, mode.loopback)
        return values, self.rts, self.dtr, rs485

    def _reconfigure_port(self):
        settings = self._uart_settings()
        if getattr(self, "_applied_uart_settings", None) != settings:
            super()._reconfigure_port()
            self._applied_uart_settings = settings
            return
        # pySerial 3.5 的 timeout setter 会调用整个 _reconfigure_port。
        # 重复 SetCommState 可能干扰 USB 串口的在途数据；只提交 COMMTIMEOUTS。
        from serial import win32
        timeouts = win32.COMMTIMEOUTS()
        if self._timeout is None:
            pass
        elif self._timeout == 0:
            timeouts.ReadIntervalTimeout = win32.MAXDWORD
        else:
            timeouts.ReadTotalTimeoutConstant = max(int(self._timeout * 1000), 1)
        if self._timeout != 0 and self._inter_byte_timeout is not None:
            timeouts.ReadIntervalTimeout = max(int(self._inter_byte_timeout * 1000), 1)
        if self._write_timeout is None:
            pass
        elif self._write_timeout == 0:
            timeouts.WriteTotalTimeoutConstant = win32.MAXDWORD
        else:
            timeouts.WriteTotalTimeoutConstant = max(int(self._write_timeout * 1000), 1)
        if not win32.SetCommTimeouts(self._port_handle, ctypes.byref(timeouts)):
            raise serial.SerialException(f"SetCommTimeouts failed: {ctypes.WinError()}")

    def close(self):
        try:
            super().close()
        finally:
            # 下次重新打开必须重新应用线路参数，不能沿用上一个句柄的缓存。
            self._applied_uart_settings = None
