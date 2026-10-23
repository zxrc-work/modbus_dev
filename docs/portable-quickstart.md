# Modbus Dev 目录式便携版

本包携带 Python、Qt 和通信依赖，不要求安装 Python。所有业务、界面和工作进程模块均为
Cython 本机扩展；仅包初始化与最小入口保留字节码。编译不是不可逆的加密。

## 启动

- macOS arm64：完整解压后打开 `ModbusDev.app`。命令行入口为
  `./ModbusDev.app/Contents/MacOS/modbus-dev --help`。
- Windows x64：完整解压后双击 `ModbusDev.exe`；命令行使用 `modbus-dev.exe`。
  不要删除 `_internal`，不能单独复制 EXE。
- 麒麟 x86_64：完整解压后运行 `./ModbusDev`；命令行使用 `./modbus-dev`。
  依赖目标桌面环境、Qt 平台插件所需系统库及串口权限。

只使用匹配系统和 CPU 架构的包。Windows、麒麟的说明是构建适配，不表示已经完成实机验收。
执行过的验收仅以随归档提供的 `.validation.json` 为准。

## 配置、日志与安全边界

GUI 默认选择本机 TCP 只读从站，尚未启动通信。默认结果目录在当前用户数据目录的 `results` 下：

- macOS：`~/Library/Application Support/ModbusDev/results`
- Windows：`%LOCALAPPDATA%/ModbusDev/results`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/ModbusDev/results`

可在界面另选结果目录。配置文件由用户选择加载、保存；CLI 的相对路径仍相对于调用者工作目录。
程序不把配置或运行日志写回应用目录。启动诊断保存在同一用户数据目录的 `logs/startup-error.log`。

`examples/` 是公开配置模板。RTU 示例端口必须改为本机实际设备；`rtu-master` 会写寄存器，
连接真实设备前务必核对站号、波特率、地址和数据。不要将串口驱动、管理员权限或现场验收视为包内能力。

GUI 经包内独立 CLI 的 `worker` 子命令运行通信；stdin 是停止指令，stdout 是版本化 JSONL，
stderr 是诊断。停止默认等待最多 5 秒；强制退出不能算正常收尾，需检查 `gui-worker-error.json`。

macOS 当前使用 ad-hoc 签名，未完成 Developer ID 签名和公证。开发机原生运行通过不代表
带隔离属性的外部分发包已获 Gatekeeper 认可；对外正式分发前须另做签名、公证和异机首启验收。

## 核验

归档旁的 `.sha256` 校验整个归档；包内 `SHA256SUMS` 记录内容摘要；`BUILD_INFO.json`
记录源码摘要、提交号、未提交状态、目标平台和实际依赖版本。不要混用不同构建的入口和 `_internal`。

可手工执行 `modbus-dev self-check --output /可写目录/native.json` 检查所有受保护模块的实际加载位置。
GUI 的 `--self-test /可写目录/gui.json` 会启动临时回环 TCP 服务并运行生命周期自检，不访问真实串口。
