"""生成双入口目录包；平台差异限制在打包阶段，不修改通信业务。"""
import os
from pathlib import Path
import subprocess
import sys


WINDOWS_MANIFEST = '''<?xml version="1.0" encoding="UTF-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
<dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2, PerMonitor</dpiAwareness>
<longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
</windowsSettings></application></assembly>'''


def render_spec(config, stage, native, protected, hidden):
    # 两个引导器使用相同编译模块及依赖，但 CLI 必须采用 console 引导器保留标准流。
    windows = config.target.startswith("windows-")
    text = f'''# 发布暂存目录中的双入口打包描述。
from PyInstaller.utils.hooks import copy_metadata
metadata = []
for name in ('pymodbus', 'pyserial', 'PySide6', 'PySide6_Essentials', 'shiboken6'):
    metadata += copy_metadata(name)
datas = metadata + [({str(stage / 'BUILD_INFO.json')!r}, 'release'),
                    ({str(native / config.package / 'ui/translations')!r}, {config.package + '/ui/translations'!r})]
common = dict(pathex=[{str(native)!r}], binaries=[], datas=datas, hiddenimports={hidden!r},
              excludes=['Cython', 'setuptools', 'pytest', 'tkinter', 'PyQt5', 'PyQt6', 'PySide2'], noarchive=False)
gui_a = Analysis([{str(stage / 'gui_entry.py')!r}], **common)
cli_a = Analysis([{str(stage / 'cli_entry.py')!r}], **common)
# 官方 Qt 运行时 hook 会提前导入 QtCore。CLI/worker 不初始化 Qt；GUI 入口仍保留官方 hook。
cli_a.scripts = [item for item in cli_a.scripts if item[0] != 'pyi_rth_pyside6']
for analysis in (gui_a, cli_a):
    leaked = {{item[0] for item in analysis.pure}}.intersection({protected!r})
    if leaked:
        raise RuntimeError('受保护模块回退为字节码: ' + ', '.join(sorted(leaked)))
gui = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True,
          name={Path(config.gui_executable).stem!r}, console=False, upx=False,
          manifest={WINDOWS_MANIFEST if windows else None!r})
cli = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True,
          name={Path(config.cli_executable).stem!r}, console=True, upx=False)
coll = COLLECT(gui, cli, gui_a.binaries, gui_a.datas, cli_a.binaries, cli_a.datas,
               name={config.application!r}, upx=False)
'''
    if config.target.startswith("macos-"):
        text += f'''app = BUNDLE(coll, name={config.application + '.app'!r},
    bundle_identifier={config.bundle_identifier!r},
    info_plist={{'CFBundleShortVersionString': {config.version!r}, 'NSHighResolutionCapable': True,
                'CFBundleExecutable': {config.gui_executable!r}, 'LSBackgroundOnly': False}})
'''
    return text


def make_bundle(config, stage, native, protected, hidden):
    for entry, module in (("gui", "desktop"), ("cli", "cli")):
        (stage / f"{entry}_entry.py").write_text(
            '"""最小发布入口，业务逻辑全部位于本机扩展中。"""\n'
            f"from {config.package}.{module} import main\n"
            "if __name__ == '__main__':\n    raise SystemExit(main())\n", encoding="utf-8")
    spec = stage / "portable.spec"
    spec.write_text(render_spec(config, stage, native, protected, hidden), encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(native), PYTHONNOUSERSITE="1")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                    "--distpath", str(stage / "dist"), "--workpath", str(stage / "pyinstaller"), str(spec)],
                   cwd=stage, env=environment, check=True)
    suffix = ".app" if config.target.startswith("macos-") else ""
    return stage / "dist" / (config.application + suffix)
