# 工具与插件扩展

工具发现和绑定位于 `src/ob_harness/tools/`，数据访问复用 Application 和领域服务。插件安装不代表自动加入所有 Agent，也不授予本体写权限。

## 内置工具与绑定

| 工具 | 用途 | effect |
| --- | --- | --- |
| ontology_get | 按 ID 读取绑定版本的对象 | read |
| ontology_search / ontology_neighbors | 检索与邻接查询 | read |
| ontology_answer | 终态确定性查询，内部已执行检索 | read |
| ontology_propose | 基于固定版本提出 v1 操作 | propose |
| ontology_validate / ontology_diff | 校验与查看绑定提案 | read |
| ontology_build | 执行绑定文档构建 run | propose |

ToolContext 由可信宿主建立，绑定 Application、本体 ID/真实版本、文档 ID 到路径的映射、输出目录与时限。模型不能提供任意文件路径或切换工作区。build 需要绑定已准备好的子 run，提案/构建需要相应权限。

```powershell
ob-harness tool-list
ob-harness tool-info ontology_search
ob-harness tool-run ontology_search --workspace demo --ontology demo --arguments query.json
# query.json: {"query":"mammal"}
```

会话另绑定 draft_query/edit/undo/diff/prepare_review/rebase 以及 draft_propose_schema，使用本轮固定草案和来源范围。draft_propose_schema 只提出增量定义并询问用户，真正批准由宿主解析独立的用户回复执行。这些是会话运行内工具，不是可按任意 ID 全局调用的插件。schema 批准和正式 commit/approve 均不注册给默认 Planner。

## 调用契约

Tool 声明 name、description、input_schema、invoke，以及 effect、version、timeout_seconds、idempotent、可选 output_schema 和终态信息。

统一通过 `invoke_tool(tool, arguments)`：

1. 验证输入 schema。
2. 按工具时限等待调用。
3. 要求 `ToolResult(payload: dict, summary: dict)`。
4. 验证输出 schema，限制完整结果序列化后不超过 1 MB。

payload 包含完整业务结果或产物引用；summary 供下轮规划使用，应短小且只含授权内容。直接调用 tool.invoke 会跳过通用包装。

schema 支持 type、properties、required、additionalProperties、items、enum、const、数值/字符串/数组边界及描述字段；不支持的验证关键字会报错。复杂业务验证由工具执行。

effect 是声明，不是自动权限执行器。工具必须通过受控领域服务读写资源。幂等性同样需要业务回执等实际保证；不确定的非幂等调用不能宣称可以自动重试。行为改变要更新 version，自定义 Planner 提供稳定 fingerprint，供恢复兼容检查使用。

## 插件发现

独立包通过 entry point 注册：

```toml
[project.entry-points."ob_harness.tools"]
my_tool = "my_package:plugin"
```

plugin 提供 name、status() 和 bind(ToolContext) → Tool。注册表拒绝重复名称及覆盖内置名称。tool-list 只读元数据；info/bind 才加载第三方代码，不静默安装或下载模型。

插件是可信宿主的 Python 代码，没有代码沙箱。工具审计、超时和参数验证不等于文件系统或网络隔离。

## MinerU

MinerU 是独立可选插件，基础安装不引入其引擎和权重。参数、依赖与产物的唯一详细说明见 [插件 README](../plugins/mineru/README.md)。

```powershell
python -m pip install -e . -e "plugins/mineru"
ob-harness tool-info mineru
ob-harness tool-run mineru --input example.pdf --output outputs/parsing --arguments configs/tools/mineru-text.json
```

ready 只代表安装/入口检查，不保证模型、设备、PDF 覆盖或语义正确。实际验证范围见 [验证记录](validation.md)。

Python 受绑定调用：

```python
from pathlib import Path
from ob_harness.tools import ToolContext, ToolRegistry
from ob_harness.tools.invocation import invoke_tool

async def parse_pdf():
    context = ToolContext(
        documents={"paper": Path("example.pdf")},
        output_dir=Path("outputs/parsing"), timeout_seconds=3600,
    )
    tool = ToolRegistry().bind("mineru", context)
    return await invoke_tool(tool, {"document_id": "paper", "tier": "standard", "ocr_mode": "auto"})
```

结果中的 document 指向标准 document.json，可供 `load_document_artifact` 和 chunk_document 读取，或直接送入 build。文本跨度是解析 Markdown 的字符位置，不是原 PDF 坐标；旧 prepare 不会因安装插件自动改用 OCR。

解析进程使用绑定路径、独立输出目录和参数白名单，不经 shell。取消尝试终止进程树，但不能保证第三方外部服务撤回；完整限制见插件说明。
