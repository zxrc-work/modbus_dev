# 2026-10-23 macOS Apple Silicon 验证

## 结论与环境

**当前源码可在本机 macOS 上运行 CLI、RTU 主从站、本机 TCP 只读从站和原生 GUI，无需修改业务代码。**
本轮只使用隔离 PTY 和 `127.0.0.1`，没有连接真实设备、打开已有物理串口或写入真实寄存器。

- 实测提交：`4e09bc0afa809c671a6a0c6c17936289a6e28984`。
- 系统：macOS 27.0.1，Apple Silicon / arm64。
- Python：3.12.11；项目内 `.venv`，未修改全局 Python 依赖。
- 使用项目锁定版本：PyModbus 3.11.4、pySerial 3.5、PySide6 6.7.2。
- 可编辑安装成功，`pip check` 无依赖冲突；三个示例配置及串口枚举命令通过。

## 验证结果

| 范围 | 结果 |
|---|---|
| 全量自动测试 | 收集 87 项，86 项通过，1 项 Windows 原生 DPI 专用测试正常跳过 |
| 原生 Cocoa GUI/worker 测试 | 15 项通过；与全量测试有重叠，不计为额外独立用例 |
| 原有 RTU 主站 PTY 场景 | 6 组通过：100ms 单点、四点、200ms 正弦、读回、故障注入、严格模式失败 |
| CLI/worker 通信矩阵 | 8 组通过：RTU/TCP 只读从站、独立源时钟、RTU 四点正弦写入与读回 |
| 串口占用与恢复 | 1 组通过；实际观察到 2 次打开失败，重试间隔约 1.002s，释放后读数成功 |
| GUI 实际 RTU 通信 | 离屏、原生 Cocoa 各 1 组通过；均经 QProcess/worker 与独立 PTY 对端通信 |
| 原生窗口检查 | 默认平台为 Cocoa，Retina DPR=2，字体 PingFang SC；默认窗口未超出可用桌面 |

集成验证共 17 组。四组从站协议场景每组完成 146 项检查，覆盖 FC03/04、其他功能码拒绝、
地址/数量/半点边界、站号过滤，以及 RTU 坏帧恢复和 TCP 拆包/粘包/异常客户端隔离。
测试对端独立计算原始字节及 CRC，没有用业务编码器自证通信正确。

原生 GUI 测试覆盖停止、重启、关闭收尾、启动失败、worker 崩溃恢复、配置切换、下拉键盘交互、
日志上限与最终计数。默认窗口截图已人工查看，中文显示正常；关键输入框与按钮高度均为 36 逻辑像素。
没有发现需要修改业务代码的 macOS 兼容性问题。

## 100ms 四点写入及每周期读回

每组运行 20 秒，计划 200 个槽位，9600/8N1，交易预算 80ms，调度容差 10%。
独立 PTY 对端收到请求后等待约 5ms 回复。没有为本轮测试放宽生产超时或调度容差。

| 入口 | ACK | 读回 | 迟发告警 | 预算不足事件 | 平均周期首发间隔 ms | 周期首发间隔 P99 上界 ms | 最大周期首发迟到 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| CLI | 199 | 199 | 0 | 1 | 100.505 | 103.0 | 4.592 |
| worker | 200 | 200 | 0 | 0 | 100.001 | 102.6 | 4.398 |
| GUI 离屏 | 200 | 200 | 0 | 0 | 100.012 | 103.3 | 4.090 |
| GUI 原生 Cocoa | 200 | 200 | 0 | 0 | 100.016 | 102.7 | 4.681 |

CLI 有 1 个槽位在开始交易前预算不足，记录为 `schedule_missed`，未发送该槽位；
最大周期首发间隔约 201ms 与此相符，不能将缺少的交易算作成功。
所有四点场景均未出现读回不一致。各入口顺序执行，不能由本次短样本推断 GUI 对抖动的因果影响或长期概率。
P99 为 0.1ms 直方图桶上界；周期首发与包含读回的全部发送分别统计。

持续运行 worker 的停止请求至进程退出耗时约 39～76ms。所有归档会话摘要的
`evidence_complete` 均为 true；预期的故障注入、用户停止和严格失败按各自策略验收，
并非要求所有进程退出码都为 0。

## 沙箱与系统诊断

首次在受限执行沙箱内启动锁定 Qt 时中断，报 `Incompatible processor` / `neon`。
同环境执行 `sysctl` 返回 `Operation not permitted`。
允许系统特性读取后，`hw.optional.neon=1`，同一个 `.venv` 内的 Qt、全量回归和原生 Cocoa 验证均通过。
因此本次错误与执行沙箱限制有关，没有通过升级 Qt、修改 CPU 检测或绕过项目依赖锁定来掩盖问题。
首次中断日志保留为 `unittest-sandbox.log`，不作为项目功能测试通过证据。

原生测试还输出过 macOS 输入法框架的 `IMKCFRunLoopWakeUpReliable` 诊断；
本轮未观察到对应的键盘交互或生命周期测试失败，未修改系统输入法设置。

## 启动与复现

以下命令在项目根目录执行，已有本轮创建并安装好的 `.venv` 可直接使用。

```sh
# 新环境首次安装：Python 3.10+；本轮使用 3.12.11。
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gui]'

# 启动原生桌面界面；默认是尚未运行的本机 TCP 只读从站。
.venv/bin/python -m modbus_dev gui

.venv/bin/python -m unittest discover -s tests -v
QT_QPA_PLATFORM=cocoa .venv/bin/python -m unittest discover -s tests -p test_gui_worker.py -v

# 每个输出目录必须不存在；重复执行时更换目录名。
.venv/bin/python tools/verify_virtual_serial.py --pty --output results/macos-master-new
.venv/bin/python tools/verify_backend_communications.py --pty \
  --output results/macos-matrix-new --commit "$(git rev-parse HEAD)" --seconds 20
.venv/bin/python tools/verify_backend_communications.py --pty \
  --output results/macos-contention-new --commit "$(git rev-parse HEAD)" --contention-only
.venv/bin/python tools/verify_backend_communications.py --pty \
  --output results/macos-gui-offscreen-new --commit "$(git rev-parse HEAD)" --gui-only --seconds 20
QT_QPA_PLATFORM=cocoa .venv/bin/python tools/verify_backend_communications.py --pty \
  --output results/macos-gui-cocoa-new --commit "$(git rev-parse HEAD)" --gui-only --seconds 20
```

RTU 对真实设备运行前必须单独确认 macOS 的 `/dev/cu.*` 设备路径及波特率、站号、地址等参数，
不能直接使用示例的 Windows COM 端口；`rtu-master` 会实际写寄存器。

## 证据与未覆盖范围

- 可提交的机器摘要：[summary.json](validation-macos-20261023/summary.json)。
- 完整本机证据：`results/validation-20261023-macos-4e09bc0/`（Git 忽略目录），
  包含环境、测试日志、各场景配置、独立对端十六进制记录、事件、会话摘要及 GUI 原始/归档副本。
- 原生窗口证据：该目录下 `native-preview/native-default.png` 和 `native-preview/report.json`；
  `probe_native_gui.py` 为本轮窗口检查脚本，不启动通信。
- 未覆盖：物理 USB 串口/RS485、真实设备、多点总线、跨主机 TCP、长时间稳定性、
  Intel Mac、跨显示器拖动，以及 `.app`/DMG 打包、签名与公证。

本结论是本机源码运行与短时虚拟通信通过，不是硬件链路、硬实时或可分发安装包验收通过。
