"""离屏验证实时表、权限操作以及曲线隐藏后的定时器停绘。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "需要可选 Qt 依赖")
class LiveGuiTests(unittest.TestCase):
    def test_values_permissions_visibility_and_stop_state(self):
        script = '''
import time
from modbus_dev.gui import Workbench, create_application
from modbus_dev.domain.config import TaskConfig
app = create_application()
window = Workbench()
window.resize(1280, 900)
window.apply_config(TaskConfig.from_mapping(dict(mode='rtu-master', access_mode='read-only',
    master_operation='read-input', serial=dict(port='TEST', baud=115200), duration_s=0)))
window.show()
app.processEvents()
assert window.configuration().master_operation == 'read-input'
operation = window.fields['master_operation']
assert not operation.model().item(operation.findData('write-single')).isEnabled()
panel = window.live_panel
now = time.monotonic()
row = dict(key='input:0:rx', point=0, name='压力', area='input', address=0,
    direction='rx', value=1.25, registers=[0x3fa0, 0], function=4,
    quality='received', sampled_s=now)
panel.ingest(dict(state='running', sampled_s=now, stale_after_s=1., rows=[row]))
assert panel.value_timer.interval() == 200
assert panel.plot.timer.interval() == 500
panel.refresh_values()
assert panel.table.item(0, 2).text() == '—'
assert panel.table.item(0, 3).text() == '1.25'
assert not panel.plot.timer.isActive()
window.monitor_tabs.setCurrentIndex(1)
app.processEvents()
assert panel.plot.timer.isActive()
panel.plot.tick()
assert len(panel.history.series['input:0:rx']) == 1
panel.selectors['input:0:rx'].setChecked(False)
assert not panel.selected('input:0:rx')
window.monitor_tabs.setCurrentIndex(0)
app.processEvents()
assert not panel.plot.timer.isActive()
panel.set_state('stopped')
assert not panel.value_timer.isActive()
assert '停止' in panel.table.item(0, 5).text()
assert panel.table.item(0, 3).text() == '1.25'
window.close()
'''
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
