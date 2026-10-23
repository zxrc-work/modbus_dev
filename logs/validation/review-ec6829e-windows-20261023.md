# 2026-10-23 最近提交检查与 Windows 本机验证

## 版本与环境

- 被检查提交：`ec6829ef0f6a9dde74c918e38cbc2154676c2b6c`（`big update not test yet`）。开始时工作区干净。
- Windows 11 x64 / 10.0.26200，Python 3.12.6，PySide6 6.7.2，PyModbus 3.11.4，pySerial 3.5。
- 检查最新提交的 RTU 操作生成、寄存器区、权限、实时快照、worker 与 GUI 改动；业务源码未修改。
- 本机和沙箱外串口枚举均为空；用户随后明确选择先完成本机测试。

## 检查发现

1. **P2：跨测点的部分寄存器会错误归属。** `application/live.py:40` 在报文未完整覆盖测点时，把整段 `words` 保存给该测点。配置 A/B/C 地址 0/2/4，经实际 `RtuRegisters.response()` 处理 FC03 address=1/count=4、提交响应后，A 和 C 都显示 `1111 2222 3333 4444`；它们各自实际覆盖的寄存器只有 `1111` 和 `4444`。应按报文与测点地址范围的交集截取原始寄存器。FC16 跨点写入也会经过同一逻辑。
2. **P2：关联不确定的写入 ACK 丢失明确状态。** `application/session.py:108` 将 ambiguous ACK 的实时质量设置为 `ack`；计数正确记录为 `ack_ambiguous`，但 GUI 的状态映射没有 `ack`，直接显示英文 `ack`，没有“应答关联不确定”提示。使用生产 `_transaction()` 和返回 ambiguous ACK 的隔离对端复现，应保留 `ambiguous` 质量。
3. **仓库记录边界：** 最新提交新增 `docs/implementation.md`，其中包含实施流水、测试数量、机器环境和历史验收经过；与根目录 `AGENTS.md` 要求实施记录集中到 `logs/implementation.md` 的约定冲突。此次仅指出，未移动已有文件或修改链接。

前两项均保存了可重复执行的隔离复现脚本和实际 JSON 结果；现有回归未覆盖这两种情况。

## 验证结果

| 范围 | 结果 |
| --- | --- |
| 完整回归 | 145 项，142 通过，3 项跳过，无失败；耗时 35.657 秒 |
| 依赖与差异检查 | `python -m pip check`、`git diff --check` 通过 |
| Windows 原生显示 | 回归包含原生 GDI 字体、Per-Monitor V2 和 100%～200% 常用 DPI；实际 GUI 已打开并查看布局 |
| 新实时组件 | 现有离屏回归覆盖数值显示、只读权限、曲线可见/隐藏定时器和停止状态；隔离复现确认上述两项缺陷 |
| 实际 GUI → worker → TCP | 在原生窗口点击启动，独立原始报文客户端连接 `127.0.0.1:1502`；100 次交替 FC03/04 读取，以及 FC06/16 拒写、半点/未映射地址/非法数量与恢复检查，共 106 次通过 |
| 停止与同端口重启 | 在 GUI 停止并确认端口释放，再次启动同端口并重复 106 次检查，全部通过；两个会话最终均 stopped / exit_code=130 |
| 运行证据 | 两次会话各记录 106 次请求与应答；`evidence_complete=true`、`dropped=0`，GUI 显示完整证据、预览丢失 0 |

第二次实际 TCP 检查的平均响应约 0.489 ms，最大约 1.455 ms；仅表示本机回环样本，不代表物理串口性能。
测试会话均已停止，GUI 窗口保留供查看。

跳过项为 POSIX 归档符号链接/权限、POSIX PTY 四操作双进程联调，以及 POSIX 主动关闭连接后的重启。
Windows 同端口重启已由上述实际 GUI 会话另行验证；Windows RTU 双进程通信仍未覆盖。

## 异常与详细结果

- 首轮回归受到沙箱临时目录权限影响，出现 `PermissionError: [WinError 5]`，随后中断；这轮不算通过。
- 使用项目内 TEMP/TMP 在本机重跑完整套件，通过结果与首轮日志分别保留。
- 详细证据根目录：`results/review-ec6829e-20261023-192121/`（Git 忽略）。
- `regression-first.log` / `regression-retry.log`：首轮与重跑的完整输出。
- `review_probes.py` / `review-probes.json`：上述两项功能缺陷的复现与实际结果。
- `native-gui/*/`：两次实际会话各自的配置、完整事件日志及摘要；`native-gui-stopped-accessibility.txt` 为第一轮停止后的界面文本。
- `native_tcp_client.py`、`native-tcp-restart-report.json` / `native-tcp-restart-trace.json`：独立客户端脚本与第二轮实际报文证据；第一轮完整报文保留在对应会话事件日志中。
- `native-gui-direct.*.log`：原生 GUI 启动进程诊断。

本轮未连接物理设备、未执行 COM/RS485 或两机 RTU 联调、未重新构建或验收便携发布包。以上结果属于当前 Windows 源码运行验证。

## 后续修复与回归（2026-10-23）

- 已按用户要求修复前两项：实时原始寄存器按测点与报文地址范围的交集截取；写入 ACK 明确区分 `confirmed` 和 `ambiguous`。
- 新增 FC03/04 跨点读取、FC16 跨点写入，以及实际 RTU 编解码路径上先超时再收到 ACK 的回归；保留正常 ACK 的确认和有效趋势验证。
- 补充 GUI 对“应答关联不确定”状态、提示及曲线断点的断言。修复前新增回归的 4 个子场景失败，修复后针对性 15 项全部通过。
- Windows 本机完整回归：147 项，144 通过，3 项 POSIX 测试跳过，无失败；耗时 37.251 秒。`git diff --check` 通过。
- 修复前失败、针对性复测和完整回归分别保存在 `results/fix-live-20261023-193759/regression-before.log`、`regression-targeted-after.log`、`regression-full-after.log`；此前检查证据保持原样。
- 本次仅修复两项功能缺陷；上述文档归档问题仍未处理，未执行物理串口验证或重新构建发布包。
