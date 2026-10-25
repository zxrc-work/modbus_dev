# 2026-10-25 Windows STLink RTU 拒帧排查

本文保留两项改动合并时的实验记录。USB 分批交付等待在 `uncertain-issue` 分支单独保存，
以 Windows 超时更新提交 `d5b0a91` 为前置，仍为待评审候选方案；
前置提交的范围和验证见 [Windows 超时更新验证](windows-serial-timeout-20261025.md)。

- 实体排查基线为 `84797a6481343bae504d68c7a7e8d8737f08ecc4`；候选实现实体复测使用该提交上的未提交改动。
- Windows、独立 Python 3.12.10 / pySerial 3.5；用户指定 COM4，枚举为 STMicroelectronics STLink Virtual COM Port。
  用户保持主站以 115200 / 8N1、站号 1、约 100ms 周期发送 FC16，写地址 0 的两个寄存器。
- 固件源码参考 `waaall/gd32-template` 的 `Test-CubeMX` 分支，读取时为 `baf5b91652fa081256e999e60e113a888e69499f`。
  未读取板上固件版本，也未修改或烧录固件。

| 对照 | 时长 | 成功处理 / 应答 | 拒帧 | SetCommState 次数 |
| --- | ---: | ---: | ---: | ---: |
| 原方式 A1 | 30s | 280 | 20 | 2625 |
| 仅更新 COMMTIMEOUTS | 30s | 299 | 2 | 1 |
| 恢复原方式 A2 | 30s | 218 | 82 | 2593 |
| 临时方案：仅更新超时并统一等待 10ms 交付静默 | 60s | 600 | 0 | 1 |
| 候选实现：仅更新超时，仅为不完整已识别请求提供有界交付等待 | 120s | 1200 | 1 | 1 |

候选实现的 1 次拒帧是打开端口后立即收到的残留混合数据（事件 sequence=7），未被吞掉或改计成功；
之后 1200 次有效请求全部应答，稳定运行期间拒帧为 0。各组均无 Win32 API 返回失败及驱动串口错误标志。

仅更新超时后残留的两次拒帧是同一合法请求的两个片段，前后拼接后 CRC 正确；
两批交付约相隔 2.4ms，原 1.75ms 的应用交付静默判断将它误拆为两帧。
原方式的超时 setter 还会反复调用 SetCommState。A/B/A 在同一硬件链路上复现差异，
支持本次软件端修复；不能据此宣称其他系统或全部 USB 串口不存在相似问题。

源码回归 187 项，184 通过、3 项 POSIX 测试跳过；串口与 RTU 专项 58 项通过。
新增覆盖 Windows 超时更新与 DCB 调用隔离、线路/RS485 参数重配、关闭重开、原生 API 失败，
以及 2.4ms 分段写入、残帧到期和不得随细碎字节无限延长等待。CRC、坏帧整段丢弃和协议异常回归保留。

在 `uncertain-issue` 分支提交前，以 `d5b0a91` 为前置从暂存区导出快照再次运行完整回归：
187 项中 184 通过、3 项 POSIX 测试跳过，耗时约 35.8s；本轮未连接实体 COM4。
该快照回归完整日志为 `results/usb-delivery-branch-20261025/full-source-tests.log`。

详细证据在 `results/rtu-stlink-20261025-0943/`：各组事件、配置、报告、API 计数、
`comparison.json`、`targeted-tests.log`、`full-source-tests.log`。临时对照脚本为
`results/probe_stlink_timeouts.py`；原始用户失败会话位于 `results/20261025-093120-f33838bb/`
及 `results/20261025-093717-279d4911/`，均保留。

本次验证为 Windows 源码从站与该条实体链路；未重测 Mac/Linux、所有适配器、长时间压力，
也未将结果归入现有便携包。现有 dist 归档不包含此次修复。
