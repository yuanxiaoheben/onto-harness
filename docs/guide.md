# 用户指南

本文统一介绍本地使用与运维。配置字段和返回格式见 [接口参考](reference.md)，内部实现见 [架构说明](architecture.md)。所有命令从包含 `pyproject.toml` 的项目根目录执行。

## 安装与简单启动

Python 3.11+。推荐在虚拟环境中安装：

```powershell
python -m pip install -e ".[web]"
```

在项目根目录的 `.env` 中设置 `DEEPSEEK_API_KEY`，文件格式参考 [.env.example](../.env.example)。首次创建时可设置 `DEEPSEEK_MODEL`；模型名应为账户实际可用的标识。

```powershell
python scripts/launch.py
```

Windows 使用本项目 `.venv` 时：

```powershell
.\.venv\Scripts\python.exe scripts\launch.py
```

启动器自动加载允许的 `.env` 变量，首次创建 `workspace/`，启动可编辑的 LLM 聊天页并打开浏览器。默认从 8000 端口开始；端口被其他程序占用时尝试后续端口，地址以窗口显示为准。已有相同工作区的服务时复用服务。关闭服务窗口或按 Ctrl+C 停止，数据不删除。

启动器不会覆盖已有工作区配置。若要改模型，编辑该工作区的 `harness.json`，然后重新启动服务。库和普通 `ob-harness` CLI 不自动读取 `.env`；自动加载是启动器的显式功能。

其他启动选项：

```powershell
python scripts/launch.py --workspace another-workspace
python scripts/launch.py --no-browser --port 8080
python scripts/launch.py --check
```

`--check` 可准备工作区并检查本地配置，不启动服务、不调用模型。Windows 的 [start.bat](../start.bat) 可双击运行；首次缺少虚拟环境时会尝试创建并安装 Web 依赖。

## 对话构建与正式保存

可以逐条输入：

```text
加入鲸，它属于 Mammal
加入蓝鲸作为鲸的下位概念
把鲸的定义改成“生活在水中的哺乳动物”
把鲸重命名为鲸类
查看差异
撤销上一步
保存
确认保存
```

新增、修改、删除和撤销只改变当前草案。发送“保存”后，页面显示新增/修改/删除、改前改后、用户来源和校验结果。核对后发送独立的“确认保存”，或点击对应审阅包的确认按钮，才产生正式版本。

确认默认 15 分钟有效，绑定本体、会话、草案 revision 和内容哈希。编辑、撤销或其他新消息使旧审阅失效；正式版本变化也会阻止提交。没有有效审阅包时，“确认保存”只准备审阅，不同时生成和发布新内容。模型返回的相同文字不能代替用户确认。

用户引文会作为本体来源保留。原始陈述能证明用户说过这些内容，但不代表已经获得外部事实验证。正式保存后会话继续存在，下次操作进入新草案。

### 编辑边界

- 概念可修改名称、定义、别名和类型，ID 保持稳定。关系可修改约束和应用条件；改变端点或关系类型采用删旧边、加新边。
- 删除概念默认不级联。存在关联边时先说明范围；明确“删除鲸及其关系”才删除这些边，子概念保留。
- 每轮编辑为原子批次；证据错误、端点缺失、类型不匹配或层级环导致整批失败。
- 撤销恢复最近尚未撤销批次的快照，revision 继续增长。当前不支持 redo、选择性历史撤销或直接回退已提交版本。
- 其他会话先提交时，当前草案保留并报冲突。显式 `/rebase` 尝试迁移到最新正式版本；有记录或 schema 冲突时拒绝自动合并。

LLM 模式处理一般自然语言；scripted 只支持明确的有限语法，例如“加入鲸，它属于Mammal”“把鲸的定义改成……”和“删除鲸及其关系”。无法理解的表达不应被当成通用推理能力。

### 会话与恢复

浏览器刷新会恢复最近会话和持久消息。“新对话”创建另一会话，旧数据仍保存在工作区。网络中断后使用“恢复请求”复用原请求，避免重复编辑。

终端对话：

```powershell
ob-harness chat --workspace workspace --ontology demo --planner llm
ob-harness chat --workspace workspace --ontology demo --conversation CONVERSATION_ID --planner llm
```

普通 CLI 需从当前进程环境获得模型凭据。支持 `/diff`、`/undo`、`/save`、`/confirm`、`/rebase`、`/resume`、`/exit`。单条消息模式可使用 `--message` 和稳定的 `--message-id`；重试时复用会话、消息 ID、原文及规划器。

## 离线工作区与查询

不需要 API key 的流程验证：

```powershell
ob-harness init demo --writable
ob-harness doctor --workspace demo
ob-harness query --workspace demo --ontology demo --query mammal
ob-harness context --workspace demo --ontology demo --query mammal
ob-harness chat --workspace demo --ontology demo --planner scripted
```

`init` 生成包内原创本体、schema、两段文本和回放响应；不依赖外部研究数据。默认只读，`--writable` 为 demo 显式授予 read/propose/write。初始化拒绝覆盖已有配置或样例。

`query` 是带版本引用的确定性检索，不生成自由文本推理答案。`context` 预览实际选中的概念、关系和预算。默认 scripted Agent 查询也不调用规划模型。

## 从文档构建

以下使用新初始化的离线 demo，完整运行后正式版本增加一次：

```powershell
$run = ob-harness build --workspace demo --ontology demo --input demo/examples/part-1.txt demo/examples/part-2.txt | ConvertFrom-Json
ob-harness inspect $run.run_id --workspace demo --trace 0
ob-harness diff $run.result.changeset_id --workspace demo
ob-harness validate $run.result.changeset_id --workspace demo
ob-harness commit $run.result.changeset_id --workspace demo --expected-hash $run.result.changeset_hash --revision 1 --key document-example-1
ob-harness query --workspace demo --ontology demo --query whale
```

真实文档需要模型配置；回放只匹配包内固定样例。TXT/MD 使用基础安装；PDF/DOCX 可安装 `.[documents]`，布局解析可选 [MinerU](../plugins/mineru/README.md)。MinerU 输出的标准 document.json 可以作为 build 输入；安装插件不会自动替换普通 PDF loader。

构建时先保存输入快照，按片段检索并累计草案。有效结果进入后续上下文，无效片段记录错误并跳过。build 返回 ChangeSet；只有可信 CLI/Python commit 才更新正式版本。会话产生的 v2 提案则必须走会话确认，不能用旧 commit 绕过。

## 自定义本体与空白草案

先在可信配置 `permissions` 中为目标 ID 授权，再导入已有本体：

```powershell
ob-harness import-ontology --workspace demo --ontology animals --input ontology.json --schema schema.json
ob-harness export --workspace demo --ontology animals --output animals-version.json
```

导入要求 ID 尚不存在；导出不覆盖已有输出。导出只含一个本体快照与 schema，不是整个工作区备份。

从空白开始：

```powershell
ob-harness chat --workspace demo --ontology animals --new-ontology --planner llm
# 或：
ob-harness serve --workspace demo --ontology animals --editable --create-ontology --planner llm
```

新建默认使用 general 模板：concept 类型及 is_a、part_of、has_property、has_function、participates_in、depends_on、precedes、related_to 八种关系。具体含义、方向及 Python 模板选择见 [接口参考](reference.md#预置-schema)。例如可以向 LLM 说明“心脏是器官的一种；心脏是循环系统的组成部分”，分别构建分类和组成关系。离线 scripted 规划器仍只支持原有简单语法，新增关系使用 LLM 或显式编辑接口。

已有工作区、本体和会话不自动升级 schema；`init` 的动物演示与精确回放继续使用原有 taxonomy 样例。Python 可传自定义领域 schema。确认前没有正式空本体，首次确认才创建正式资源。对话不能自行发放资源权限。

### 对话中扩展 schema

使用 LLM 规划器时，如果需求需要尚未声明的概念类型或关系类型，助手会先展示新增定义、关系方向、端点类型和约束，并询问是否同意修改。此时 schema 尚未改变。

1. 回复“同意修改schema”（或紧接该提案回复“同意”“可以”“是的”），只更新当前草案的 schema；回复“不同意”则保持原样。Web 也提供绑定该提案的同意/拒绝按钮。
2. 回复“继续构建”，助手使用原始用户陈述作为证据，在新 schema 下构建内容；肯定答复本身不作为本体事实证据。
3. 回复“保存”，审阅内容和 schema 的差异，再回复“确认保存”，共同发布正式版本。只修改 schema 也可提交。

每次提案绑定会话、用户和草案版本，15 分钟有效；期间发送其他消息会使该提案失效。过期、旧草案或权限不足均不会更新 schema。可用“撤销”逐步撤销内容编辑及 schema 扩展。

目前支持新增类型，不支持删除/重定义已有类型、修改已有关系约束或新增任意逻辑规则。schema 扩展后暂不支持跨 schema 的 `/rebase` 自动迁移。离线 scripted 模式不会自动识别缺失类型；自动提出建议需要 LLM，确认执行由本地程序完成。

## 本体全景图

页面顶部的“对话 / 全景图”按钮可切换视图，不清空聊天记录或输入框。全景图默认读取正式本体，可切换为当前会话草案；两种来源分别标注正式版本或草案修订号，草案修改不会自动进入正式图。

支持滚轮/按钮缩放、空白处拖动平移、拖动节点调整布局、适应画布、按名称/别名/ID 定位及按关系类型筛选。点击节点或用键盘 Enter/空格选中节点，右侧显示定义、别名和相邻关系，也可将该概念带回对话输入框。箭头保持原始 source → target 方向，分类关系按 schema 组织层次；布局调整只影响显示，不修改本体。

当前采用本地 SVG 渲染，无 CDN 和图形服务依赖，适合小型本体交互。重新进入或点击“刷新”读取最新数据；未发布的新本体请先创建会话，再选择“当前会话草案”。

### 20 节点预置示例

包内 `resources/animals20.json` 提供中英双语的简化动物概念示例，涵盖脊椎/无脊椎动物、哺乳动物、鸟、鱼、昆虫等，共 20 个概念。它是原创教学样例，不是权威生物分类标准。原有 `init` 两节点精确回放样例保持不变。

```powershell
# 在 harness 目录执行；省略 --apply 只预览。
.\.venv\Scripts\python.exe scripts\seed_ontology.py --workspace workspace --ontology demo --apply
```

预置脚本复用已存在的同名或别名概念，保留 ID、定义和已有关系，补充中文别名；新增内容通过带来源的 ChangeSet 正常提交为新版本。重复执行无新增变更。如果目标已有其他概念，总数可能超过 20。旧会话仍绑定其原草案，可新建对话读取最新正式版本。

## 诊断与任务恢复

```powershell
ob-harness runs --workspace demo
ob-harness inspect RUN_ID --workspace demo --events
ob-harness inspect BUILD_RUN_ID --workspace demo --trace 0
ob-harness cancel RUN_ID --workspace demo
ob-harness resume RUN_ID --workspace demo
ob-harness audit --workspace demo --limit 20
```

trace 从 0 开始，仅用于已完成的构建片段。普通 Agent 的澄清使用 `resume RUN_ID --answer 文本`；会话澄清必须发送新的用户消息，活动 turn 可用会话 `/resume` 或 Python 服务恢复。

| 状态或错误 | 处理 |
| --- | --- |
| completed | 查看业务结果；build 完成仍不等于已提交 |
| completed_with_errors | 查看各片段 trace，评估有效部分，不当作全量成功 |
| waiting_input | 补充用户信息；会话中作为新消息发送 |
| waiting_review | 外部工具副作用未知；先核实，再 resolve 实际 ToolResult |
| failed / cancelled | 检查错误和检查点，在配置兼容且仍有预算时恢复；不要盲目重复新建任务 |
| budget_exhausted | 恢复不增加预算，先检查耗尽原因 |
| conversation_conflict / draft_conflict | 刷新当前会话或草案，旧响应不得套用新状态 |
| version_conflict | 重新构建；会话可显式 /rebase，不覆盖正式版本 |
| context_overflow | 缩小输入分块或调整字节预算，创建新任务 |
| invalid_evidence / ambiguous_evidence | 核对逐字引文与来源，不能关闭证据检查 |
| resume_incompatible | 恢复原配置/代码，或创建新任务，不手改校验和 |
| workspace_busy | 等待工作区执行器退出 |
| artifact_missing / artifact_changed | 从完整备份恢复，排查缺失或修改的产物 |

## 经验、清理与备份

```powershell
ob-harness memory put --workspace demo --namespace research --content "检查鲸的上位概念方向" --ontology demo
ob-harness memory search --workspace demo --namespace research --query 鲸 --ontology demo
ob-harness context --workspace demo --ontology demo --query 鲸 --memory --namespace research
ob-harness memory clear --workspace demo --namespace research
ob-harness cleanup --workspace demo --older-than-days 30
```

长期经验需要显式写入；当前对话构建不自动使用 MemoryStore，也不将对话自动总结为经验。经验 search 排除过期记录，get 仍可用于管理性读取；过期不等于物理删除。

cleanup 默认为计划，`--apply` 才执行。正式版本、ChangeSet、登记资料、会话和被引用的历史/来源保留，不会删除全部超过期限的数据。审计单独保留。当前没有会话物理删除命令。

备份前停止使用该工作区的所有进程，再复制整个目录，包括 harness.json、.harness 和回放文件。SQLite 使用 WAL，不能只复制活动数据库的单个 sqlite3 文件。恢复到独立目录后，用 doctor、查询和 inspect 检查。

工作区包含原文、上下文与模型结果。`tracing.save_raw=false` 仅不在最终 trace 链接原始请求/响应，恢复产物仍可能包含全文；相关边界见 [安全说明](../SECURITY.md)。
