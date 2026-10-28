# 八点独立配置与周期预算验证

- 环境：macOS arm64，Python 3.12，PySide6 6.7.2；本地离屏 GUI、TCP 字节对端及伪串口/确定性时钟。
- 范围：版本 1～3 配置及草稿迁移、八点类型/编码、逐点混合操作与读回、地址连续合并、两区同地址不同类型、TCP/RTU 保持区写入保留、实际数值与曲线、20ms 下限/10ms 周期预留/1.2 倍预警线、双语与小屏可达性。
- 结果：完整回归 296 项，291 项通过，5 项 Windows 专属测试跳过；332 条翻译检查和编译通过。新旧任务示例均通过加载校验。
- 重要修正：禁用点有效默认排列需在迁移时明确保存；旧草稿共享字段需分别映射到两区；TCP 可写模式不能继续显示“只读从站”。首次回归中的旧全局操作控件、32 位 FC06 拆写及共享 TCP 快照断言已按新契约调整，新增独立回归保留相应边界覆盖。
- Qt 在沙箱内的 CPU 检测及本地监听曾被阻止；GUI/本地通信测试获准在沙箱外复测。自动审批曾超时，重试后完成。首次失败及各轮复测分别保留，没有覆盖或删除首次失败证据。
- 未覆盖：物理串口/RS485 设备、Windows/麒麟实机验收、USB 高负载长期稳定性与发布包重建；最低周期和黄色通过不代表硬实时保证。
- 详细结果：`results/independent-points-full-first.log`、`results/independent-points-full-retest-1.log`、`results/independent-points-full-retest-2.log`、`results/independent-points-full-retest-3.log`、`results/independent-points-full-final.log`；最终翻译日志为 `results/independent-points-translation-final.log`，中英文截图与布局报告位于 `results/independent-points-preview/`。
