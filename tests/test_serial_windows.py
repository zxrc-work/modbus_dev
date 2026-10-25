"""Windows 超时更新不能再次重配 DCB；线路变化与重新打开必须仍然生效。"""
import ctypes
import os
import unittest
from unittest.mock import patch

import serial

from modbus_dev.engines.serial_windows import TimeoutSerial


@unittest.skipUnless(os.name == "nt", "使用 Windows pySerial 后端")
class WindowsTimeoutTests(unittest.TestCase):
    def setUp(self):
        from serial import win32
        self.port = TimeoutSerial(port=None)
        self.port._port_handle = 123
        self.port.is_open = True
        self.applied = []

        def apply(handle, pointer):
            value = ctypes.cast(pointer, ctypes.POINTER(win32.COMMTIMEOUTS)).contents
            self.applied.append(tuple(getattr(value, name) for name, _ in value._fields_))
            return 1

        self.base_patch = patch.object(serial.Serial, "_reconfigure_port", autospec=True)
        self.base = self.base_patch.start()
        self.timeout_patch = patch("serial.win32.SetCommTimeouts", side_effect=apply)
        self.native = self.timeout_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.addCleanup(self.timeout_patch.stop)
        self.port._reconfigure_port()

    def tearDown(self):
        self.port.is_open = False
        self.port._port_handle = None
        self.port.close()

    def test_read_write_and_inter_byte_timeouts_only_update_timeouts(self):
        self.port.timeout = .0085
        self.port.write_timeout = .02
        self.port.inter_byte_timeout = .002
        self.assertEqual(self.base.call_count, 1)
        self.assertEqual(self.applied[-1], (2, 0, 8, 0, 20))
        self.port.timeout = 0
        self.assertEqual(self.applied[-1], (0xffffffff, 0, 0, 0, 20))
        self.port.timeout = None
        self.port.inter_byte_timeout = None
        self.port.write_timeout = None
        self.assertEqual(self.applied[-1], (0, 0, 0, 0, 0))
        self.assertEqual(self.base.call_count, 1)

    def test_line_and_rs485_changes_still_reconfigure_uart(self):
        from serial.rs485 import RS485Settings
        self.port.baudrate = 57600
        self.assertEqual(self.base.call_count, 2)
        self.port.rs485_mode = RS485Settings()
        self.assertEqual(self.base.call_count, 3)
        self.port.timeout = .001
        self.assertEqual(self.base.call_count, 3)

    def test_reopening_cannot_reuse_the_previous_handle_configuration(self):
        self.port.is_open = False
        self.port.close()
        self.port.is_open = True
        self.port._reconfigure_port()
        self.assertEqual(self.base.call_count, 2)

    def test_timeout_api_failure_is_not_silently_ignored(self):
        self.native.side_effect = None
        self.native.return_value = 0
        with self.assertRaisesRegex(serial.SerialException, "SetCommTimeouts failed"):
            self.port.timeout = .01
