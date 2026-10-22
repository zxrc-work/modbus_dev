# Modbus Dev 架构设计

日期：2026-10-22。状态：设计基线；库版本和硬件性能须以验证记录为准。

M2 实施补充：当前 Python 3.10+，锁定 PyModbus 3.11.4 / pySerial 3.5；
RTU 采用库编码加受限 deadline 适配器，见 [选型记录](pymodbus-evaluation.md)。
下文 GUI、轮转日志与从站仍是设计目标；当前日志为单文件 32 MiB 严格证据模式。
串口读写采用有界等待，但操作系统打开串口及线程调度不提供硬实时保证。

## 1. 架构与依赖方向

```text
CLI ─────────────────┐
                     ├─ application：单会话、运行策略、停止、事件、结果
GUI → worker process ┘       ├─ sources：常量/正弦/DYN
                             ├─ mapping：点表和数值编码
                             ├─ engine：交易接口与库适配器
                             └─ reporting：异步记录与摘要
engine → PyModbus / pySerial → OS
integrations.lfom → application + capture/analysis（单独可选能力）
```

核心不得导入 GUI、Qt、LFOM 配置或 DCS 内部模块。平台判断只放在适配器/能力检测中。
不实现动态插件发现、HTTP 服务、多会话管理或通用消息总线。
核心可直接被 Python 调用，CLI 与 worker 采用同一个用例入口；不能调用旧脚本 main() 充当模块化。

## 2. 模块

| 模块 | 职责 | 禁止承担的职责 |
|---|---|---|
| domain | 不可变任务配置、点表、样本、交易结果、事件、错误代码 | UI、文件路径推断、系统 I/O |
| application | 会话状态机、策略、调度、停止与资源收尾 | CRC、Qt 控件、设备预设 |
| sources | 读取/生成值、原始时间和质量；准备阶段验证 DYN | 串口交易、LFOM 时间平移 |
| mapping | 地址覆盖检查、float32 排列、相邻好点分组 | 决定失败后继续或停止 |
| engines | 标准交易与从站服务；库类型转换成领域结果 | GUI 刷新、业务验收结论 |
| reporting | 有界日志写入、原始记录、摘要、CSV | 阻塞通信循环等待磁盘 |
| integrations.lfom | 配置转换、strace、CBOR、采样健康分析 | 作为通用核心依赖 |
| cli / gui | 参数/表单转换、状态展示、用户控制 | 复制业务规则或协议实现 |

## 3. 协议库复用决策

优先评估 PyModbus 的主站和从站能力，使用 pySerial 作为串口驱动，不重写完整 Modbus 栈。
库的具体版本精确锁定到验证版本；其 API 不按传统语义版本保证，升级必须跑协议和时序回归。
库异常、PDU 和 datastore 类型仅允许出现在 engines 内，不泄漏到应用层接口。

进入生产路径前必须验证：
1. 明确关闭隐式重试（例如 retries=0），应用层拥有唯一的重试/重连策略。
2. 超时是否覆盖完整交易、外部 deadline 是否真正能终止等待，取消后如何清理缓冲和关联状态。
3. trace_packet 的时间含义、分段行为、迟到 ACK 匹配、异常码和错误 CRC 可见性。
4. Windows 是否引入 flush 轮询；9600/8N1 下单点/四点 100 ms 交易是否满足预算。
5. 从站是否能拒绝所有非允许功能码，以及坏质量点能否正确返回异常，而不是旧值。

已有固定周期发送器是时序和失败语义的参考及回归基线，不直接整文件迁入。
若库确实不能满足特定诊断行为，只允许保留一个范围受限的专用 RTU 后端，并记录测试证据与原因；
不得仅为省事同时维护两个通用协议栈。在线帧处理和离线取证重组共享编码原语，不共享容错政策。

参考：https://pymodbus.readthedocs.io/en/stable/source/client.html
参考：https://github.com/pymodbus-dev/pymodbus

## 4. 数据复用

LFOM dyn_reader 是 DCS DYN 解码器的来源；DCS 已增加文件流/间隙处理，不能用文件名相同推断完全等价。
先以同一输入比对值、时间、质量、CRC 错误、缺口和边界，再确定唯一的可复用解码实现。
Modbus 回放继续要求时间连续；DCS 批量导出的容错选项不得默认带入回放。
滤波和降采样从 replay.py 的纯数据处理部分提取；不携带 TCP JSON 服务、LFOM 默认点名或 CLI。
短期复用实现记录来源版本/哈希及差异；长期共同包需要两个项目单独迁移，不在本轮改写 DCS。

## 5. 会话与调度

状态：idle → starting → running ↔ reconnecting → stopping → stopped；不可恢复错误进入 failed。
单一 SessionRunner 拥有通信资源；同一进程禁止二次启动。运行配置不可变。
停止令牌贯穿准备、等待、读写和重连；所有阻塞调用有上限。停止后先释放通信，再收尾日志和摘要。

持续发数：perf_counter 绝对槽位；落后整周期跳至未来槽位；不追赶；异常后限频重连。
调度容差 schedule_tolerance_percent 默认 10%，可配置为 0～100；每周期首次实际写调用与计划时刻比较，
晚发不超过容差（含边界）为正常，超过产生 schedule_late WARNING 及计数，告警本身不停止任务。
读回和后续分组不作为新的周期首发；容差不延长交易 deadline，不造成时间基准漂移。
严格回放：每个源样本按顺序消费；落后整周期或交易失败即失败，禁止悄悄丢源样本。
从站：独立数据时钟更新寄存器快照；读请求不推动源时间；启动/结束时刻语义显式记录。

计划预算 = 所有请求/响应线路时间 + 必要静默 + 读回 + 设备/调度余量。
静默按实际串口格式计算；收到驱动交付数据后重新计时，持续噪声等待不突破 deadline。
write 返回时间、应答交付时间、源时间、计划时间分别保存。Windows 高精度计时请求成对释放。
一旦关联不确定，后续有效 ACK 仍保留 ambiguous，不能因重开串口就宣称严格关联恢复。

## 6. 日志与 UI 性能

事件信封：schema_version、session_id、sequence、utc、monotonic_ns、level、code、phase、message、details。
语义事件包括 transaction、schedule_missed、reconnect、protocol_exception、ack_ambiguous、log_overflow。
文件记录详细上下文和堆栈；UI 展示简短原因、关键参数和定位提示，可展开详情。

通信线程只构造有界事件并非阻塞入队；专门写入线程批量 JSONL，按时间/大小刷新。
初始默认队列容量 8192，刷新间隔 200 ms，单文件 32 MiB、最多 8 个分片，均可调整并写入快照。
队列满时丢弃事件必须累计数量/级别/序号范围，恢复时写入缺口记录；最终摘要独立保存丢失计数。
关键错误保留独立的最后错误状态，不依赖已经拥塞的普通队列；摘要标记 evidence_complete=false。
磁盘故障作为 log_write_failed 进入失败/停止流程，使用 stderr/worker 状态兜底，不能静默继续假装成功。
滚动淘汰也标记原始证据不完整；严格证据模式达到限额就停止，不覆盖证据。

GUI 不订阅全量报文：worker 单独产生最多 5 Hz 状态快照和批量警告。
UI 最多保留 2000 行日志，单批有上限，重复告警合并；完整证据保存在文件。
GUI 与 worker 使用版本化 JSONL 控制/状态协议；stdout 专用于协议，stderr 用于启动故障。
进程管道同样有界且异步发送，不允许 GUI 卡顿反向阻塞通信。停止先请求，超时才升级终止，并标记异常结束。

## 7. 配置、CLI 与交付

统一入口 `python -m modbus_dev` / `modbus-dev`；规划 validate、run、ports、analyze、lfom-capture 子命令。
GUI 通过相同配置模型保存/加载；配置含 schema_version，未知字段和版本拒绝，禁止静默拼错生效。
配置不含现场默认串口、硬编码 LFOM 路径；示例明确标注仅用于测试。
运行目录保存 config.json、events*.jsonl、summary.json，CSV 为导出产物。
退出码：0 按策略正常完成；1 运行失败；2 配置/依赖错误；130 用户中断。持续模式通信异常计数不等于退出失败。

GUI 为可选依赖；核心最低 Python 版本在库评估后确定，GUI 可有更高依赖要求。
跨平台源码、分平台打包；先采用普通 Python + PyInstaller，Cython 不作为必需条件。
界面遵循工业调试台布局：配置/操作、运行状态、交易统计、可展开日志；不以动画和逐帧曲线干扰诊断。

## 8. 验证与迁移顺序

M0：需求、设计、原实现复用清单和时序验收矩阵。
M1：独立包、配置模型、错误/事件契约、绝对调度和日志背压基础及单元测试。
M2：PyModbus 可行性测试/锁定版本，RTU 主站纵向打通；通过正常、迟到、CRC、异常、噪声、重连测试。
M3：DYN 对照与数据源、只读 RTU/TCP 从站、LFOM 扩展；独立对端和真实后台验证。
M4：单会话 GUI、工作进程、限流展示、停止/崩溃恢复、打包自检。
M5：Windows COM 和麒麟验证、100 ms GUI/日志压力对照、更新 LFOM 调用方、退役旧工具。

每阶段状态如实记录，未验证的端口/平台不标记通过。任何迁移不得放宽旧测试的有效值、质量和时序判据。
