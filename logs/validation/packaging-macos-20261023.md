# 2026-10-23 macOS arm64 便携包实包验收

## 版本与环境

- 基准提交：`5af4e6f`；工作区包含尚未提交的发布实现，`dirty=true`。
- 实际输入源码摘要：`331c3e73d73ba6b2874e6b7e51b7c1a05e88884830fba2207fb857be23f057c9`。
- 本机 macOS / Apple Silicon arm64，Python 3.12.11，独立 `.venv-build-macos-arm64`。
- Cython 3.1.3、PyInstaller 6.22.3、hooks-contrib 2026.9、setuptools 80.9.0。
- 保持 PySide6 6.7.2、PyModbus 3.11.4、pySerial 3.5；完整版本见锁定清单和机器摘要。
- 未连接真实设备、打开已有物理串口、安装驱动或修改防火墙，RTU 仅使用新建隔离 PTY。

## 实际产物

`dist/ModbusDev-0.1.0-macos-arm64-331c3e73d73b.tar.gz`，40,530,372 字节。

SHA-256：`4c50601a9b776a75a54cc83dd767a7f743c8748ce23b1c71d65a5272636cf2d8`。

归档包含 `ModbusDev.app`、公开 JSON 示例、便携使用说明、构建信息、审计结果和内容校验清单。
GUI 和 CLI 两个 Mach-O arm64 入口共享包内运行时，不要求安装 Python。
26 个应用模块实际加载为 Cython 扩展；仅包初始化和最小引导接线保留字节码。
编译不是绝对保密。归档旁保存完整回执和 SHA-256，可提交的精简摘要见
[summary.json](packaging-macos-20261023/summary.json)。

## 验收结果

| 范围 | 结果 |
|---|---|
| 源码全量回归 | 收集 114 项，通过 113 项，跳过 1 项 Windows 专属测试 |
| 编译后同套回归 | 收集 114 项，通过 113 项，跳过同一项，不借用业务源码后备 |
| 保护审计 | 两个实际入口的 PYZ 及磁盘模块检查通过，26 个应用模块均为本机扩展 |
| CLI 基础命令 | 版本、串口枚举、三个公开示例校验、全部模块加载位置检查通过 |
| worker TCP | 独立字节客户端 FC03/04 四点读取、拒写、非法地址、JSONL 停止通过 |
| CLI TCP | 同端口重新启动、自然结束及日志摘要通过 |
| 故障及收尾 | 端口占用失败及释放恢复、父管道断开、配置错误终态通过 |
| RTU 从站 | 实际 CLI/worker 与 PTY 通信，独立 CRC 的 FC03/04 读值及停止通过 |
| RTU 主站 | 实际 CLI 与独立 PTY 对端完成 FC16 写入及 FC03 读回，读回一致 |
| GUI 原生 Cocoa | 实际 GUI → 包内独立 CLI worker，五组生命周期场景通过 |
| GUI 离屏 | 同五组实际工作进程场景通过，不使用模拟 worker |
| 应用包启动 | 经 LaunchServices 启动整个 `.app`，同五组场景通过，平台为 Cocoa |
| 签名完整性 | 解压 `.app` 的 `codesign --verify --deep --strict` 通过，仅 ad-hoc，不是公证 |
| 安装目录不变 | 自检前后完整内容校验清单一致，日志及截图不写回应用包 |

每组 GUI 场景包含：TCP 实际读取及正常停止、同端口重启并自然结束、worker 强制崩溃后标记
证据不完整并恢复、端口占用失败后恢复、关闭窗口等待工作进程停止。
原生截图已查看：Cocoa、Retina DPR=2、PingFang SC，窗口控件与中文正常。

本轮从归档解压到含中文及空格的路径后执行，不是在源码或未归档的打包临时目录执行。
运行环境清除开发 Python/Qt/QML 变量，PATH 不含虚拟环境和 Homebrew；CLI 在运行本机模块
诊断前不加载 Qt。开发机仍有系统图形环境，因此不能替代干净异机系统验收。

## 首次失败与修复

各轮失败、中间包和复测证据均保留，没有放宽业务时序或保护门禁：

1. `macos-arm64-6esb9tlw`：测试未考虑 `/var` → `/private/var` 链接，修正规范化路径比较。
2. `macos-arm64-n4idw_np`：编译暂存包遗漏 `python -m` 的轻量 `__main__` 接线，补齐入口，
   没有让业务模块回退为源码。
3. `macos-arm64-nio7m1go`：实际冻结程序发现官方 Qt 引导 hook 会预加载 QtCore；CLI 取消
   该 GUI 专属引导脚本，GUI 保留官方初始化，CLI `gui` 子命令改为转发独立 GUI。
4. `macos-arm64-v2gus1kr`：中间版本实包通过；随后补充初始化文件保护、版本一致性、
   Python 3.10 构建工具测试跳过边界及签名门禁，并完整重建，不用旧包替代新实现。
5. `macos-arm64-w0fxntki`：最终构建，全部上述门禁通过。

Clang 输出过生成代码的不可达分支注解及重复 rpath 警告；编译与实包验证通过，未通过关闭
Python 边界检查或修改通信预算掩盖警告。原生 GUI 出现过 macOS 输入法框架
`IMKCFRunLoopWakeUpReliable` 诊断，没有对应生命周期失败，未修改系统输入法。

## 证据与复现

- 最终编译及源码/编译后回归：`build/releases/macos-arm64-w0fxntki/`。
- 各轮总日志：`results/build-macos-arm64.log`。
- 最终解压、通信、截图及完整回执：`results/release-macos-arm64-20261023-095535-321654/`。
- 中间目录和完整产物由 Git 忽略；禁止分发其中生成的源码。
- 构建和其他平台步骤见 [发布说明](../../docs/packaging.md)。

## 未覆盖范围

Windows x64 / 麒麟 x86_64 包本轮尚未构建实测，仅准备原生构建适配。构建器在 Mac 上拒绝
这两个目标，不宣称跨平台交付全部完成。

Developer ID、公证、带隔离属性的异机首启、第三方许可分发审查、物理 USB/RS485、真实设备、
多点总线、长时间压力、外部网络、跨显示器及其他 CPU 架构均未覆盖。短时 PTY/TCP 通过不构成
硬实时或现场性能验收。
