# 2026-10-23 实际通信验证

## 版本与范围

实测提交：`fcb52cd844b5501435db357935e302acbf4a99ce`。从远端 main 快进更新后执行。
最近三次功能提交分别增加只读 RTU/TCP 从站、GUI 工作进程、界面与高 DPI 支持。
本轮重点验证实际通信与工作进程接口，没有修改通信逻辑。新增验证工具和结果供复现。

| 环境 | Python | PyModbus | pySerial | 已有可选 PySide6 |
|---|---|---|---|---|
| Windows | 3.12.10 | 3.11.4 | 3.5 | 6.9.1 |
| 麒麟 x86_64 | 3.12.14 | 3.11.4 | 3.5 | 6.11.2 |

通信依赖使用项目锁定版本。可选 GUI 使用现有环境，因此本轮 GUI 结果不能替代锁定 PySide6 6.7.2 的发布验收。
初次本机检查使用全局 PyModbus 3.10.0，日志保留；正式通信验证和 Windows 回归改用 3.11.4。
没有改变系统 Python、安装驱动或调整防火墙与 SSH 策略。

## 功能结果

- Windows 87 项回归全部通过；麒麟收集 87 项，通过 86 项、跳过 1 项 Windows 原生 DPI 测试。
- 原有 RTU 主站六组虚拟串口场景在两平台通过：100ms 单点、100ms 四点、200ms 正弦、读回、故障注入、严格模式失败。
- Windows 使用现有 HHD COM11/COM12，麒麟使用真实 PTY；实际 CLI 子进程、pySerial 驱动和独立字节对端参与通信。
- 两平台分别通过 8 组扩展场景：RTU/TCP 从站的 CLI 与 worker、两种从站的独立源时钟、主站 CLI/worker 的四点正弦写入及读回。
- 每个从站协议场景有 146 项检查：FC03/04 完整点读取、其余 125 个请求功能码拒绝、非法数量/地址/半点、广播/其他站号忽略、拒写后值保持不变；分别补测 RTU CRC/短帧/噪声/超长帧恢复和 TCP 拆包/粘包/事务号/坏客户端隔离/慢半包期限。
- 实际占用串口后释放：两平台均出现 2 次打开失败，约 1 秒间隔重试，随后成功读数；重连时源时钟持续推进。
- Windows 真实 Qt Workbench 在离屏渲染下，经 QProcess/worker 对接 COM 对端完成 20 秒通信；界面最终计数与摘要一致。不是仅用模拟 worker 结果驱动界面。
- 所有上述功能场景的原始日志均完整，没有读回值不一致；worker 请求停止的端到端耗时约 29～65ms（含进程退出）。

功能场景“通过”表示协议、数据及资源收尾断言通过；不会把调度告警或跳过可选读回解释成零异常性能达标。
没有为此次测试临时放宽生产超时或 10% 调度容差。

## 100ms 四点写入及每周期读回

每组运行 20 秒，计划 200 个槽位，9600/8N1，交易预算 80ms，独立对端在收到完整请求后等待约 5ms 回复。
写入使用 FC16、读回使用 FC03；源为正弦，读回与实际写入的寄存器字比较。

| 环境/入口 | ACK | 成功读回 | 迟发告警 | 预算不足事件 | 平均首发间隔 ms | 首发间隔 P99 上界 ms | 最大首发迟到 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| Windows CLI | 200 | 192 | 12 | 8 | 99.997 | 117.7 | 31.572 |
| Windows worker | 200 | 197 | 4 | 3 | 100.006 | 109.5 | 26.152 |
| Windows GUI（离屏） | 200 | 190 | 9 | 10 | 99.995 | 111.1 | 22.606 |
| 麒麟 CLI | 200 | 200 | 0 | 0 | 99.999 | 100.7 | 1.338 |
| 麒麟 worker | 199 | 199 | 0 | 1 | 100.508 | 104.4 | 4.789 |

Windows 三组均完成 200 次写入确认，分别有 8/3/10 次可选读回因剩余预算不足未发送。
麒麟 CLI 完成 200 次写入和读回；worker 有 1 个槽位在开始交易前预算不足，完成 199 次写入与读回。
这些是明确记录的未发送，不是坏 CRC 或发送后超时。未将缺少的交易算作成功。
麒麟 worker 最大首发间隔约 198ms 与该未发送槽位相符；只看平均间隔会掩盖它。

迟发相对绝对计划时刻判断，100ms 周期超过 10ms 才告警；不是把所有相邻间隔强行要求为 90～110ms。
周期首发和全部交易分开统计，避免读回流量扭曲周期。P99 是 0.1ms 直方图桶上界。
各入口顺序运行，负载与调度条件不同；本样本不能证明 GUI 导致或消除了抖动，也不能推断长期发生概率。

## 麒麟 TCP 与网络可达性

麒麟服务就绪后，本机独立客户端完成 146 项协议检查以及 100 次、目标间隔 100ms 的 TCP 读取。
本机读取平均响应约 0.736ms，最大约 1.676ms；该数值不包含真实外部网络往返。

Windows 到 `192.168.197.133` 本次临时 TCP 测试端口的直接连接两次超时。
同时确认服务监听在 `0.0.0.0`，且同服务的麒麟本机协议检查通过；直连具体网络原因尚未定位。
临时 SSH 转发被服务端拒绝，诊断为 `administratively prohibited`，因此没有通过转发宣称跨主机通信成功。
跨主机 TCP 直连验收仍未通过，不能用本机 TCP 结果替代。

额外准备尝试中保留了临时服务已结束、以及新进程未就绪时的 connection refused 结果。
确认监听就绪后使用独立目录重测并通过，没有删除首次失败文件。
临时转发已关闭；测试 TCP 服务正常 SIGTERM 收尾，退出码 130，日志完整。

## 证据与复现

机器摘要：[summary.json](validation-20261023/summary.json)。完整原始证据位于
`results/validation-20261023-fcb52cd/`（Git 忽略目录），含两平台配置、请求/响应十六进制、事件、摘要、stdout/stderr、回归日志。
麒麟结果已下载并保留 `kylin-results.tar.gz`；临时目录可能被系统回收，以本地归档为准。

```sh
python tools/verify_virtual_serial.py --master COM11 --slave COM12 --output results/master-new
python tools/verify_backend_communications.py --master COM11 --slave COM12 --output results/matrix-new --commit YOUR_COMMIT --seconds 20
python tools/verify_backend_communications.py --master COM11 --slave COM12 --output results/gui-new --commit YOUR_COMMIT --gui-only --seconds 20
python tools/verify_backend_communications.py --master COM11 --slave COM12 --output results/reconnect-new --commit YOUR_COMMIT --contention-only
# 麒麟/Linux：将串口参数替换为 --pty；核心矩阵不需要 GUI
python tools/verify_backend_communications.py --pty --output results/pty-new --commit YOUR_COMMIT --seconds 20
```

外部 TCP 验证工具 `verify_network_client.py` 要求目标是四点地址 0/2/4/6、值 12.5 的只读测试从站；
会发送拒写/非法请求，测试目标必须明确指定。正常验收先确认服务就绪。

尚未覆盖物理 RS485、多点总线、真实设备、长时稳定性、日志极限压力、原生桌面拖动交互、锁定 Qt 版本的发布包。
虚拟端口没有模拟物理线路传输、收发换向和硬件缓存，不能据此承诺硬实时或现场设备验收通过。
