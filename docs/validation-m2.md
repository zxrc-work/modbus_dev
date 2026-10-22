# M2 验证记录

日期：2026-10-22。Python 3.12.14；PyModbus 3.11.4；pySerial 3.5。
结果来自实际运行，没有把首次失败替换成复测通过。

## 范围与结果

- Windows：43 项单元测试通过；现有 HHD 虚拟串口 COM11/COM12 上六组 CLI 集成测试通过。
- 麒麟 x86_64：使用已有 py312 环境，43 项单元测试通过；隔离 PTY 上首次 100ms 场景未通过，第二轮六组通过。
- 六组场景：100ms 单点 10 秒/100 次、100ms 四点 3 秒/30 次、200ms 正弦 3 秒/15 次、每周期读回、注入超时/坏 CRC/异常应答、严格模式失败退出。
- 故障注入持续模式：1 次 timeout、1 次 bad_reply、1 次 exception_ambiguous、27 次 ack_ambiguous；严格模式首次超时即退出码 1。
- 对端使用独立 CRC 校验及寄存器存储；实际 CLI 子进程通过 pySerial 打开端口。没有安装驱动或修改麒麟系统 Python。

## 100ms 单点实测

下表是最终 Windows 轮次与麒麟复测各 100 次请求、99 个周期首发间隔，不是长期可靠性结论。

| 平台 | 正常 ACK | 平均间隔 ms | 最小 ms | 最大 ms | P99 上界 ms |
|---|---:|---:|---:|---:|---:|
| Windows 虚拟 COM | 100/100 | 100.016 | 88.088 | 110.777 | 110.8 |
| 麒麟 PTY 复测 | 100/100 | 100.010 | 96.143 | 103.615 | 103.7 |

P95/P99 为 0.1ms 有界直方图的桶上界；cycle_interval_ms 统计每槽位首发，send_interval_ms 包含读回等所有交易。
主机 API 调用和驱动交付时间不等于物理线路上的电平时间。虚拟串口不模拟真实波特率传输、RS485 换向和硬件缓存。

## 麒麟首次异常（保留）

首轮 100 次写入得到 64 次 ack、1 次 bad_reply、35 次 ack_ambiguous。
槽位 64 的发送时刻为 4229.470857039，deadline 为 4229.550257025（perf_counter 秒）。
首字节在 4229.550423784 才交付，已晚于 deadline 约 0.167ms；剩余字节在下一槽位作为 idle_rx 排出并重新等待静默。
因此不能把该响应计为按时成功；后续关联标记持续保留。其他有效回复不等于另外发生 35 次通信失败。

首次运行缺少模拟从站响应时间记录，无法归因到从站调度、主站调度或其他系统因素。
已在测试工具加入对端响应开始/结束时刻，且失败断言前保存 received.json；保持 80ms 超时和原有验收断言不变后，复测六组通过。
接受偶发抖动，但保留异常、计数和证据；短时复测不能估计其长期发生概率。

## 证据与复现

仓库 results/m2-validation/ 保留 windows-final、kylin-first、kylin-repeat 的报告与事件文件（results 为 Git 忽略目录）。
docs/validation-m2/ 保留三轮 report.json，便于版本控制；首次失败报告包含失败计数。
麒麟临时测试目录为 /tmp/modbus-dev-m2.Hn6Vne，临时目录可被系统清理，以本地归档为准。

```sh
python -m unittest discover -s tests -v
python tools/verify_virtual_serial.py --master COM11 --slave COM12 --output results/windows-new
# Linux / 麒麟，不打开现有硬件端口
python tools/verify_virtual_serial.py --pty --output results/kylin-new
```

代码修正：初次打开串口后再建立调度基准，避免初始化耗时侵占首个槽位；新增周期级统计避免读回流量扭曲周期指标。
未验证：物理 RS485/设备、长期运行、Python 3.10 实际运行、GUI 与通信并行压力、DYN/从站/LFOM。
