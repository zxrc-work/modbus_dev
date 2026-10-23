# Modbus Dev

独立的 Modbus 通信测试与回放工具，当前正在按阶段实施。

- [需求](docs/requirements.md)
- [架构设计](docs/design.md)
- [实施状态与验证计划](docs/implementation.md)

## 当前可用能力

RTU 主站 FC16 发数、常量/正弦源、FC03/04 读回验证、连续/严格策略、端口枚举、
绝对周期调度、停止与重连、原始报文及有界异步日志。CLI 和 GUI 共用 SessionRunner。
RTU/TCP 只读从站支持常量/正弦、FC03/04、独立数据时钟和完整 float32 快照；其他请求功能码被拒绝。
PySide6 单会话 GUI 已实现配置编辑/加载/保存、启动/停止、状态、计数、日志预览和结果目录。
**独立安装包尚未交付。DYN/CSV 离线输入继续暂缓。validate 只校验配置，不代表功能已可运行。**

```sh
python -m pip install -e .
modbus-dev validate examples/rtu-master.json
modbus-dev ports
# 先修改示例中的端口和设备参数；run 会实际写寄存器。输出目录必须不存在。
modbus-dev run examples/rtu-master.json --output results/my-first-run
# TCP 只读从站默认监听 127.0.0.1:1502；Ctrl+C 停止。输出目录必须不存在。
modbus-dev run examples/tcp-slave.json --output results/tcp-first-run
# RTU 只读从站：先修改示例串口；与主站使用不同端口或不同设备。
modbus-dev run examples/rtu-slave.json --output results/rtu-slave-first-run
python -m unittest discover -s tests -v
```

## 桌面界面

```sh
python -m pip install -e ".[gui]"
python -m modbus_dev gui
# 安装后也可使用桌面入口：modbus-dev-gui
```

GUI 依赖可选，锁定 PySide6 6.7.2；普通 CLI 安装不需要 Qt。
界面采用统一浅色主题、分组配置标签页、关键计数和日志/详细统计工作区，配色和尺寸统一在
`src/modbus_dev/gui_theme.py` 管理。Windows 使用 GDI 字体后端、中文字体回退及完整字体像素对齐；
由 Qt 处理逐显示器 DPI 与分数缩放，窗口按可用桌面调整大小，小屏保留滚动操作。
详见 [GUI 设计与高 DPI 验证](docs/gui-design.md)。源码采用可编辑安装时，关闭并重新启动 GUI 即可加载界面更新。
默认选择本机 TCP 只读从站。可切换 RTU 主站/从站、加载现有 JSON 示例、校验后启动。
地址填写 float32 点的零基起始地址，用英文逗号分隔。RTU 主站启动后会执行 FC16 写入。
结果父目录下每次创建独立时间戳目录，运行期间禁止修改配置；停止后可以修改并再次启动。
关闭窗口会先请求工作进程收尾；超过 5 秒强制结束并标记证据可能不完整。
工作进程崩溃时显示异常，已有结果目录另存 gui-worker-error.json，不伪造正常摘要。

界面只显示最多 5 Hz 的状态和批量日志预览，最多保留 2000 行；原始报文写入 `events*.jsonl` 分片。
“数据与时序 → 日志保留”可配置分片数量，默认最多 100 片（包含当前文件），每片不超过 32 MiB。
超限删除最旧分片；历史淘汰、日志队列丢失和界面预览丢失分别展示，不把滚动窗口宣称为完整会话证据。
界面预览丢失与原始日志丢失分别计数。最终性能统计保存在 summary.json，并显示于结束日志。
本轮 87 项测试通过，验证 Windows 原生高 DPI/字体、Qt 离屏渲染、工作进程和本机 TCP 读数；
覆盖 100%～200% 及当前 300% 缩放，跨显示器拖动、麒麟桌面和安装包待验收。GUI 虚拟串口联调结果见最新通信验证记录。

开发基线 Python 3.10+；锁定 PyModbus 3.11.4 和 pySerial 3.5，CLI 不需要 Qt。
本轮 Windows 和麒麟均使用 Python 3.12.14 验证；未在系统 Python 3.7 上运行新包。
示例 COM11 仅为演示值，validate 不打开串口，run 会打开指定串口。
Linux 使用自己的设备路径；源码不限制 GUI 平台，发布包需要分平台构建验证。

软件独立安装、运行和验证。CLI 与 GUI 共用通用业务模块，协议能力优先复用成熟库。

实施顺序：TCP 只读从站 → RTU 只读从站 → GUI → DYN/CSV 离线数据。
TCP 示例中 unit=1，FC03/04 共用地址 0、2、4、6 的四个 float32 点，B1234 排列。
只允许完整点读取（例如 address=0/count=8）；半点、未映射地址或跨空洞返回异常 02。
数据按 period_ms 推进，与客户端轮询频率无关；duration_s=0 持续运行。
TCP 会话摘要使用 source_updates、slave_request、rx、tx 等计数；rx/tx 是驱动调用记录数，不是报文数。

RTU 从站使用相同点表和只读规则；广播及其他站号静默忽略。CRC 错误、短帧、超过 256 字节的帧整帧丢弃，
等待下一次静默后恢复，不从噪声中扫描嵌入请求。接收间隔依据驱动交付时刻，不能视为物理总线时间。
仅面向单主站请求/应答测试；Windows 虚拟 COM 与麒麟 PTY 已完成实测，详见最新通信验证记录。多点 RS485 与物理设备仍待验收。
持续策略下串口打开失败、读写异常或短写至少间隔 1 秒重连；严格策略遇到这些故障停止。
重连不重置数据时钟；无轮询时数据也继续更新。timeout_ms 限制从站单次写等待（另有 50ms 上限），
不是主站等待回复的超时；接收等待最多 10ms，并受下一数据更新时间约束。

## 运行结果

调度容差默认是周期的 10%，配置字段 `schedule_tolerance_percent`，允许 0～100 的有限数值。
以下调度容差和 ACK 关联说明用于 RTU 主站；从站按独立数据时钟更新快照。
例如 `"period_ms": 100, "schedule_tolerance_percent": 10` 表示每周期首次发送相对计划时刻可晚 10ms（含边界）。
容差内正常且仍计入时间统计；超出时产生 `schedule_late` WARNING 和计数，不因这条告警停止任务。
比较的是实际写调用开始时刻，包含串口静默等待；同周期的后续分组和读回不重复判定。
它不会移动绝对时间基准，也不延长 `timeout_ms`：后者仍是相对计划槽位的整周期交易截止预算。
连续模式偶发通信异常继续，落后整周期跳槽不补发；严格模式仍在交易失败或落后整周期时停止。
本次调度配置不改变 RTU 应答关联不确定的取证规则。

Ctrl+C 请求停止并收尾。退出码：0 按策略完成，1 运行失败，2 配置/依赖错误，130 用户停止。
连续模式允许通信异常，退出码 0 不代表全部交易成功，需查看 counters 和 ack_correlation_uncertain。
输出目录含 config.json、events.jsonl / events-000002.jsonl 等分片、summary.json；命令结束后 stdout 输出结果 JSON。
原始收发、警告、错误及堆栈写入事件文件，通信线程不等待磁盘写入。
队列溢出记录丢失数量和序号范围，并将 evidence_complete 标记为 false。
达到单片 32 MiB 时自动切换新片，不再因此停止任务；超出保留数量时删除最旧分片，
摘要单独记录淘汰的文件数、记录数、生产者序号范围和当前文件索引，并标记 retention_truncated=true、evidence_complete=false。
历史淘汰本身不使会话失败；文件创建、写入、刷新、关闭或淘汰删除失败仍会停止任务，避免静默丢证据或超出磁盘保留上限。
默认日志窗口最多约 3.125 GiB（不含配置和摘要）；限制只针对单个会话，旧会话结果目录不会自动删除。

旧配置无需修改，新配置可通过 JSON 或 GUI 设置保留数量（1～10000）：

```json
{
  "mode": "tcp-slave",
  "duration_s": 0,
  "logging": {"max_files": 100}
}
```

读取整份保留日志时应按序号合并所有分片，可复用 `modbus_dev.reporting.segments.event_paths(directory)`。
首个 `events.jsonl` 可能已淘汰，不能只读取该文件。实时状态只传固定大小统计，最终 `summary.json` 的
`segments.files` 保存当前保留分片的名称、字节数、记录数及生产者序号范围；队列丢失仍使用原有 `dropped` 计数。
RTU 超时后无法仅凭相同 ACK 确认属于哪次请求，因此后续有效回复保守标记 ambiguous；重连不会清除此状态。

- [协议库选型与边界](docs/pymodbus-evaluation.md)

## 验证记录

需求、设计、使用说明、验证方法与验收标准保留在 `docs/`；每轮验证报告和精简 JSON 摘要归档到
[`logs/validation/`](logs/validation/README.md)，纳入版本管理。原始日志、截图及完整通信证据仍输出到
`results/`，由 Git 忽略。报告中的复现命令和 `results/...` 路径均以项目根目录为基准。

- [最新 Windows / 麒麟实际通信验证](logs/validation/validation-20261023.md)
- [macOS Apple Silicon 实际通信与原生 GUI 验证](logs/validation/validation-macos-20261023.md)
- [M2 历史验证记录](logs/validation/validation-m2.md)
