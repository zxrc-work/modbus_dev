"""Test bridge: drive the real Qt workbench and archive its communication result."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('config', type=Path)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from modbus_dev.domain.config import load_config
from modbus_dev.gui import Workbench, create_application

config = load_config(args.config)
if config.duration_s <= 0:
    parser.error('GUI bridge requires a finite test duration')
app = create_application()
window = Workbench()
window.apply_config(config)
window.output.setText(str(args.output.parent / 'gui-original'))
window.show()
app.processEvents()
window.start()
deadline = time.monotonic() + config.duration_s + 15
try:
    if window.temporary is None:
        raise RuntimeError('GUI did not create a session')
    while window.temporary is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    if window.temporary is not None:
        window.force_stop()
        raise TimeoutError('GUI did not finish within the test limit')
    if window.result is None or window.forced:
        raise RuntimeError('GUI did not receive a normal worker result')
    shutil.copytree(window.last_output, args.output)
    (args.output.parent/'gui-preview.log').write_text(window.logs.toPlainText(), encoding='utf-8')
    if window.result.get('counters') != window.last_counters:
        raise AssertionError('GUI counters differ from final worker result')
    print(json.dumps(window.result), flush=True)
    exit_code = window.result['exit_code']
finally:
    window.close()
    app.processEvents()
sys.exit(exit_code)
