# GUI 设计与高 DPI

界面采用统一浅色设计与 Windows 字体初始化方案，
继续使用 PySide6 6.7.2，不引入第三方 UI 组件库。协议、配置校验和工作进程沿用原有实现。

## 布局与公共样式

- 左侧为会话配置，按“连接与点表 / 数据与时序”分组；通信模式决定 TCP、串口、主站读回字段的显示。
- 右侧为会话控制、连接摘要、结果目录、关键计数和“运行日志 / 详细统计”。运行失败自动切回日志。
- 会话运行时锁定配置。关键计数来自实际工作进程；运行结束后修改下一次的模式，不会改写上一轮的统计含义。
- 窗口按可用桌面设定初始大小；配置与运行区均可滚动。逻辑宽度不足 920 时上下排列，分隔条可拖动。
- `gui_theme.py` 集中定义颜色、字体、调色板、控件状态、逻辑尺寸；页面通过语义角色选择外观。
- `gui_widgets.py` 提供公共卡片、组合框弹层和矢量下拉箭头，保留 Qt 的鼠标、方向键、Enter 和 Escape 交互。

主要规格为正文 14、辅助/日志 13、标题 24、输入/按钮/组合框外部高度 36，面板圆角 8。
全部采用 Qt 逻辑像素，不根据屏幕倍率手动缩放控件。

## Windows 清晰度

在首次创建 QApplication **之前**，默认选择 `windows:fontengine=gdi`，并设置
`PassThrough` 分数缩放策略。已显式设置的 `QT_QPA_PLATFORM` 和
`QT_SCALE_FACTOR_ROUNDING_POLICY` 会保留，不覆盖离屏测试或调用方设置。

正文按已安装字体选择 Segoe UI、微软雅黑/微软雅黑 UI 等字体并保留中文回退；
Windows 设置 `PreferFullHinting`，日志使用等宽字体并继承同一渲染偏好。
Qt 负责逐显示器 DPI 感知、窗口跨屏缩放和按实际像素渲染；不关闭系统高 DPI 缩放，
也不把低分辨率界面截图拉大。参见 [Qt 高 DPI 文档](https://doc.qt.io/qt-6/highdpi.html)。

这次处理覆盖源码入口 `python -m modbus_dev gui` 和 `modbus-dev-gui`。
独立 exe 安装包尚未交付；后续打包需另加 Per-Monitor V2 清单并验证实际 exe，不能用源码窗口测试代替。

## 预览和回归

不打开串口、不启动会话，生成三个模式的两组配置页及组合框弹层截图：

```powershell
python tools/preview_gui.py --output-dir results/gui-preview/offscreen
python tools/preview_gui.py --native --output-dir results/gui-preview/windows
python -m unittest discover -s tests -v
```

`--native` 使用隐藏的 Windows 原生窗口，不显示到桌面。
输出的 `report.json` 记录窗口逻辑尺寸、截图实际像素、DPR、字体、hinting、
控件高度及窗口是否获得 Per-Monitor V2。`--scale` 是相对系统原生倍率的测试乘数，
例如系统为 150% 时 `--scale 2` 得到 300%，不是 200%。

`tests/test_gui_display.py` 在独立进程中按原生倍率换算，测试 100%、125%、150%、175%、200%
及当前系统倍率，同时验证窄屏字段与启动操作可通过滚动访问。
`tests/test_gui_worker.py` 覆盖配置往返、下拉框键盘交互、字段状态、计数、
本机 TCP 读数、启动/停止/重启、崩溃和关闭收尾。

离屏截图不作为 Windows 字体清晰度结论；原生测试也不代替不同物理显示器间拖动、
Windows 输入法或麒麟桌面的实机验收。
