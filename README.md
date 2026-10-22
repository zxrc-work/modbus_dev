# Modbus Dev

独立的 Modbus 通信测试与回放工具，当前正在按阶段实施。

- [需求](docs/requirements.md)
- [架构设计](docs/design.md)
- [实施状态和迁移清单](docs/implementation.md)

## 当前可用能力

RTU 主站 FC16 发数、常量/正弦源、FC03/04 读回验证、连续/严格策略、端口枚举、
绝对周期调度、停止与重连、原始报文及有界异步日志。CLI 和未来 GUI 共用 SessionRunner。
**DYN、RTU/TCP 从站、LFOM 扩展、GUI 和打包尚未实现。validate 只校验配置，不代表功能已可运行。**

```sh
python -m pip install -e .
modbus-dev validate examples/rtu-master.json
modbus-dev ports
# 先修改示例中的端口和设备参数；run 会实际写寄存器。输出目录必须不存在。
modbus-dev run examples/rtu-master.json --output results/my-first-run
python -m unittest discover -s tests -v
```

开发基线 Python 3.10+；锁定 PyModbus 3.11.4 和 pySerial 3.5，CLI 不需要 Qt。
本轮 Windows 和麒麟均使用 Python 3.12.14 验证；未在系统 Python 3.7 上运行新包。
示例 COM11 仅为演示值，validate 不打开串口，run 会打开指定串口。
Linux 使用自己的设备路径；源码不限制 GUI 平台，发布包需要分平台构建验证。

设计禁止机械搬运原脚本。旧工具在迁移期间继续保留，新 CLI 验收完成后统一入口，不长期双线维护。

## 运行结果

调度容差默认是周期的 10%，配置字段 `schedule_tolerance_percent`，允许 0～100 的有限数值。
例如 `"period_ms": 100, "schedule_tolerance_percent": 10` 表示每周期首次发送相对计划时刻可晚 10ms（含边界）。
容差内正常且仍计入时间统计；超出时产生 `schedule_late` WARNING 和计数，不因这条告警停止任务。
比较的是实际写调用开始时刻，包含串口静默等待；同周期的后续分组和读回不重复判定。
它不会移动绝对时间基准，也不延长 `timeout_ms`：后者仍是相对计划槽位的整周期交易截止预算。
连续模式偶发通信异常继续，落后整周期跳槽不补发；严格模式仍在交易失败或落后整周期时停止。
本次调度配置不改变 RTU 应答关联不确定的取证规则。

Ctrl+C 请求停止并收尾。退出码：0 按策略完成，1 运行失败，2 配置/依赖错误，130 用户停止。
连续模式允许通信异常，退出码 0 不代表全部交易成功，需查看 counters 和 ack_correlation_uncertain。
输出目录含 config.json、events.jsonl、summary.json；命令结束后 stdout 输出结果 JSON。
原始收发、警告、错误及堆栈写入事件文件，通信线程不等待磁盘写入。
队列溢出记录丢失数量和序号范围，并将 evidence_complete 标记为 false；写入失败或达到 32 MiB 上限会停止任务。
轮转与 UI 展示在后续阶段实现。
RTU 超时后无法仅凭相同 ACK 确认属于哪次请求，因此后续有效回复保守标记 ambiguous；重连不会清除此状态。

- [协议库选型与边界](docs/pymodbus-evaluation.md)
- [Windows / 麒麟验证记录](docs/validation-m2.md)
