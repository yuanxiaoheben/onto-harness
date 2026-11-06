# OB Harness MinerU Tool

可选安装的 PDF 解析插件，不依赖业务应用、LangGraph、数据库或 LLM Provider。主包只提供工具契约与发现机制，MinerU 依赖由此插件声明。

使用 Python 3.11–3.14，建议单独虚拟环境。从 `OB_harness/harness/` 安装：

```powershell
python -m pip install -e . -e "plugins/mineru"
python -m ob_harness tool-info mineru
python -m ob_harness tool-run mineru --input "C:/documents/example.pdf" --output outputs/parsing
```

该插件通过 `ob_harness.tools` entry point 注册为 `mineru`。
卸载插件使用 `python -m pip uninstall ob-harness-mineru`；pip 不自动删除 MinerU 及其依赖或已下载模型。
`tool-info` 的 ready 只检查当前环境中 MinerU 4.x 包和 `mineru-kit` 入口，不验证模型、设备或运行结果。

参数文件示例：

```json
{"document_id": "document", "tier": "flash", "ocr_mode": "txt"}
```

```powershell
python -m ob_harness tool-run mineru --input example.pdf --output outputs/parsing --arguments configs/tools/mineru-text.json --timeout 3600
```

默认 `tier=standard, ocr_mode=auto`，请求解析全部页。`flash + txt` 读取 PDF 文本层，不能替代扫描件 OCR。
首次模型解析可能下载权重；具体设备支持取决于 MinerU 和所选运行引擎。
依赖约束为 `mineru>=4.0,<5`，使用官方 `mineru-kit parse`，不沿用旧版 `-b pipeline` 或已移除的 extras。
参见 [官方安装说明](https://github.com/opendatalab/MinerU/blob/master/docs/en/quick_start/index.md) 和 [CLI 说明](https://github.com/opendatalab/MinerU/blob/master/docs/en/usage/cli_tools.md)。本插件未建立独立锁文件，部署应锁定实际安装组合。

每次调用新建输出子目录，保留输入 PDF 快照、`document.md`、标准 `document.json`、解析日志及 `manifest.json`。
成功时 manifest 记录版本、参数、输入 SHA-256 和产物校验和；失败/取消保留现场，不覆盖已有结果。
Agent 参数只接受文档 ID、tier、ocr_mode；输入路径、输出目录和时限由应用绑定，不接受模型提供命令或服务地址。
解析命令运行在同一 Python 环境的子进程中，不经 shell，不依赖 PATH 中其他版本的 mineru。
取消/超时会尝试终止 worker 及其子进程；不能保证取消外部服务或第三方模型运行库自行脱离进程树的工作。

返回结果为 `ob-harness.parsed-document/v1`，其中 `document` 指向 `ob-harness.document/v1` 文件。
解析文本为 MinerU Markdown，字符偏移基于该文本，不代表原始 PDF 页坐标。请求全页不等于已验证无遗漏，记录 `coverage_verified=false`。
本版不输出标准块坐标、不保证表格语义正确，也不进行本体构建；后续消费方式见 [工具架构说明](../../docs/tools.md)。

已验证插件包装的发现及缺引擎拒绝路径；MinerU 引擎、模型、设备和真实 PDF 解析尚未验收，详见 [验证记录](../../docs/validation.md)。这里的 Python/依赖范围来自插件声明，不等于所有组合已运行通过。
