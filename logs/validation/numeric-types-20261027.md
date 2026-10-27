# 2026-10-27：通用数值类型源码验证

- 环境：macOS arm64，Python 3.12.11，PyModbus 3.11.4、pySerial 3.5、PySide6 6.7.2。
  C 代码生成使用独立构建环境中的 Cython 3.1.3。
- 范围：五种数值类型及全部允许排列、固定独立寄存器样例、整数上下界及精确输入、
  混合点表宽度/重叠/分组/线速预算、旧配置迁移、原始读回、部分点和非有限值诊断、
  实时整数显示、配置草稿和语言切换、GUI 到 worker、真实回环 TCP 及隔离 PTY 主从工作进程。
- 最终回归收集 238 项，通过 233 项、跳过 5 项 Windows 专属测试，无失败。
  完整输出：`results/numeric-types/regression-final-retest.log`。
- 新的固定 PDU 诊断工具实际检查 6 个 TCP、13 个 RTU 用例，全部通过；
  RTU 包含 FC06、FC16、混合类型、半点/非有限位模式、失败后原始编码不变。
  完整输入、回执和报文：`results/numeric-types/diagnostic-smoke/`。
- 278 条界面翻译检查及资源编译通过；三种角色离屏布局已查看，截图位于
  `results/numeric-types/preview/`。51 个应用模块的 Cython C 代码生成检查通过，
  输出位于 `build/numeric-types-cython/`，未进行本轮本机扩展编译或便携包重建。
- 初次沙箱内整套测试因 Qt 无法获取 NEON 特性而中止，单独混合 TCP 测试也受回环监听权限限制。
  保留 `baseline.log`、`types-first.log`；经允许在沙箱外复测通过，未覆盖首次异常。
- 未覆盖：Windows/麒麟实机、物理串口/RS485、采集主程序实机互操作及发布包验收。
  本工具保留原始寄存器诊断行为，不以工具互测代替被测主程序的业务完整性验收。
- 完整结果总目录：`results/numeric-types/`；功能和使用约束见[通用数值类型](../../docs/numeric-types.md)。
