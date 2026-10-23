# 2026-10-23 Windows x64 便携包实包验收

## 版本与环境

- 实测提交：`9bea83695b61a066c408561173d376371ecddea7`，构建时工作区干净，`dirty=false`。
- 实际构建输入 SHA-256：`f7540bdf39de085389be180081c24b3e7ac8db97715d91abaa31f169d5745f97`。
- Windows 11 x64，系统版本 `10.0.26200`；Python 3.12.10 x64，独立 `.venv-build-windows-x64`。
- Visual Studio 2022 Community 17.14.19，MSVC 14.44.35207，Windows SDK 10.0.26100.0。
- 严格使用 `requirements/release.txt`：Cython 3.1.3、PyInstaller 6.22.3、hooks-contrib 2026.9、
  PySide6 6.7.2、PyModbus 3.11.4、pySerial 3.5；`pip check` 通过。
- COM 补测经用户明确指定，使用已安装的 HHD 虚拟串口对 COM11/COM12；应用使用 COM11，独立对端使用 COM12。
  未安装驱动、修改防火墙或连接物理串口与真实设备。

## 实际交付物

`dist/ModbusDev-0.1.0-windows-x64-f7540bdf39de.zip`，52,896,496 字节。

SHA-256：`065aa9859a64ce61f9a102b26b760f6c2d54d010db5a55e30f3921b2806d6916`。

完整解压后进入 `ModbusDev/`，双击 `ModbusDev.exe`；命令行使用相邻的 `modbus-dev.exe`。
必须保留 `_internal/`，无需在目标电脑安装 Python。归档还包含公开示例、使用说明、构建信息、保护审计及内容校验清单。

两个实际 PE 均为 AMD64；GUI 的 Windows subsystem 为 2，CLI 为 console subsystem 3。
全部 26 个应用模块均从包内 `.pyd` 加载，双入口 PYZ 与磁盘保护审计通过，CLI 诊断前不主动加载 Qt。

归档基于原始干净提交构建，一次完整发布构建通过，未修改应用源码或构建输入。
之后仅补充验收工具 `--cli` 参数、文档与测试回执；补测结束后重新核对构建输入摘要、归档 SHA-256
及解压安装目录全部文件，均保持不变。原始包内使用说明仍保留提交时的“Windows 待验收”措辞；
实际执行结果以归档旁更新后的 `.validation.json` 和本报告为准。

## 验收结果

| 范围 | 实际结果 |
|---|---|
| 源码回归 | 114 项，通过 112 项，跳过 2 项 POSIX 专属测试 |
| Cython 编译后同套回归 | 114 项，通过 112 项，跳过同两项 |
| Windows 源码字体/DPI | Windows 原生后端、字体、Per-Monitor V2、100%～200% 缩放通过 |
| 归档解压 | ZIP 实际解压到含中文与空格的独立目录；清除被测进程开发 Python/Qt 环境及 PATH |
| 基本命令 | 版本、串口枚举、3 个公开配置模板、全部原生模块加载位置检查通过 |
| TCP/worker | FC03/04 读取、拒写、非法地址、停止、同端口重启、自然结束通过 |
| 故障与收尾 | 端口占用失败及恢复、父管道断开、配置错误终态通过 |
| 实际 GUI | Windows 原生与 offscreen 各通过 5 组生命周期场景 |
| 发布版 GUI DPI 补测 | 实际 EXE 在 100%、125%、150%、175%、200% 下各通过 5 组生命周期场景，均为原生 Windows / Per-Monitor V2 |
| 虚拟 COM 协议矩阵 | RTU/TCP 从站各经 CLI 与 worker 验证，每组 146 项检查通过；RTU 另覆盖 CRC/噪声/短帧恢复 |
| 独立数据时钟 | RTU/TCP 从站在无请求期间持续更新，读出的正弦快照与实际发布事件一致 |
| RTU 主站 | 实际包内 CLI/worker 经 COM11/COM12 完成 FC16 四点正弦写入与 FC03 读回，各运行 20 秒 |
| 串口占用与恢复 | 实际独占 COM11 后出现 2 次打开失败，重试间隔约 1.012 秒；释放后恢复读数、正常停止 |
| 文件不变 | 完整发布验收及额外 DPI/COM 测试后，解压安装目录内容和归档 SHA-256 均未改变 |

两项跳过为 POSIX 符号链接/执行权限归档往返、POSIX 服务端主动关闭连接后的立即重启测试。
Windows 同端口重启与端口占用恢复由实际包的生命周期验收另行覆盖。

每组 GUI 生命周期场景为：实际 GUI → 相邻包内 CLI worker → TCP 读取及正常停止、同端口自然结束、
worker 强制崩溃及恢复、端口占用及恢复、关闭窗口等待 worker 停止。原生截图已查看，中文及控件正常。
源码 DPI 预览辅助脚本会显式导入源码，因此不只依靠“编译后套件”证明发布版 DPI；补充矩阵直接执行
解压包的 `ModbusDev.exe --self-test`，同时核对截图像素尺寸与实际 DPR。

## RTU 主站读回与调度结果

每组计划 200 个槽位、100ms 周期、9600/8N1、80ms 整周期预算，地址为 0/2/4/6，独立对端约 5ms 后回复。

| 实际入口 | 写入 ACK | 成功读回 | 预算不足未发读回 | 首发迟到告警 | 周期平均间隔 ms | 周期 P99 上界 ms |
|---|---:|---:|---:|---:|---:|---:|
| 包内 CLI | 200 | 196 | 4 | 6 | 100.048 | 109.0 |
| 包内 worker | 200 | 185 | 15 | 18 | 99.959 | 114.0 |

所有已执行读回均一致，没有超时、坏回复或 ACK 关联不确定。原始事件确认 4/15 次未发送均为
FC03 在 RTU 静默等待后剩余预算不足；没有把这些可选读回计为成功。首发最大迟到分别约 14.814/17.833ms。
未改变应用 10% 调度容差、超时或调度策略。“通过”指协议和资源收尾断言通过，不代表零调度告警或硬实时达标。

## 证据与复现

- 精简机器摘要：[summary.json](packaging-windows-20261023/summary.json)。
- 完整构建日志：`results/build-windows-x64-attempt-1.log`。
- 源码/编译后回归、生成 C 代码与编译中间文件：`build/releases/windows-x64-c30arsid/`，不得分发其中源码。
- 实际解压、通信、截图及完整回执：`results/release-windows-x64-20261023-101414-566474/`。
- 原始构建回执保留为该目录的 `validation-build.json`，其中 COM 按原构建器行为记为待验收。
- `windows-supplement/report.json`、`com-matrix/report.json`、`com-contention/report.json` 保存补测结果。
  最终 `validation.json` 及 `dist/*.validation.json` 附加实际补测并更新虚拟 COM 状态；归档本体未改变。
- 补测入口记录在 `results/verify_packaged_windows.py`；COM 补测可使用已纳入版本管理的
  `tools/verify_backend_communications.py --cli`，完整命令见[打包说明](../../docs/packaging.md)。

```powershell
py -3.12 -m venv .venv-build-windows-x64
.venv-build-windows-x64\Scripts\python.exe -m pip install -r requirements/release.txt
.venv-build-windows-x64\Scripts\python.exe build_release.py --target windows-x64
```

环境准备最初受到执行沙箱限制：临时目录写入、pip 网络访问及 Git 所有权检查。
改用项目内 `build/tmp`、获准联网安装和本次进程范围内的 Git `safe.directory` 配置后继续，未修改全局 Git 设置。
构建及原生通信/GUI 验收获准在沙箱外运行。完整原生构建首轮即通过，未降低测试门禁。
安装日志保留在 `results/install-windows-x64.log`，所有实际验收原始记录均保留。

## 未覆盖范围

物理 USB/RS485、真实设备、多点总线、长期压力、外部网络、干净异机安全策略、代码签名、跨显示器拖动
以及发布版 GUI 与 RTU 的组合场景仍未覆盖。此次 GUI 通信采用回环 TCP，COM 验收覆盖发布版 CLI/worker。
麒麟发布包本轮未构建，不把此前源码通信通过视为发布包通过。
