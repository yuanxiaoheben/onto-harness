# 接口与配置参考

本页只列当前已实现接口；运行流程见 [用户指南](guide.md)。所有相对配置路径按所属工作区或 legacy 配置文件解析，不能假定相对进程当前目录。

## 工作区配置

`Application(Path(workspace))` 读取 `workspace/harness.json`。配置和主体来自可信宿主，不从模型输出建立。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| schema_version | 1 | 工作区配置格式 |
| subject | local | 本地主体标识，不是登录凭据 |
| permissions | {}；init 为 demo 生成权限 | ontology_id → read/propose/write 列表，无隐式继承 |
| retrieval.strategy | keyword | none / keyword / all |
| retrieval.top_k | 10 | keyword 种子上限 |
| retrieval.max_nodes / max_edges | 30 / 40 | 候选子图总量上限 |
| retrieval.hops | 0 | 支持 0 或 1 |
| context.max_input_bytes | 32000 | 序列化 messages 的 UTF-8 字节上限 |
| context.history_bytes | 2000 | 可选历史/经验块的字节上限 |
| chunking.max_chars / overlap_chars | 2400 / 100 | Python 字符；0 ≤ overlap < max_chars |
| budget.max_model_calls | 50 | build 及默认 Agent 的累计调用上限 |
| budget.timeout_seconds | 900 | 累计有效运行时长，恢复不重置 |
| tracing.save_raw | false | 最终 trace 是否链接原始请求/响应；不删除恢复数据 |
| model.provider | replay | replay / api / deepseek / local |
| model.responses_path | examples/responses.json | 回放匹配文件，相对工作区 |

嵌套节按默认值合并。execution_hash 覆盖影响执行的配置，回放时还包含 fixture 内容哈希；变化可能导致旧 run 无法恢复。权限配置不属于模型选择参数。

Agent 的 Budget 还独立限制 max_steps、max_tool_calls、max_observations、max_history_bytes。会话每轮当前为 max_steps=2、max_tool_calls=4，并使用工作区的模型调用/超时限制；额外的会话总调用额度默认 200。`context.history_bytes` 不等于 Agent 的观察摘要预算。

### 模型

API 示例（替换为自己的服务和模型）：

```json
{
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com",
  "model": "your-model-id",
  "api_key_env": "DEEPSEEK_API_KEY",
  "timeout": 60,
  "retries": 0,
  "max_output_tokens": 2048,
  "json_mode": true
}
```

本地服务使用 `provider=local`、自己的 base_url（例如 `http://127.0.0.1:8000/v1`）和模型名；按需配置 api_key_env。框架发送兼容 Chat Completions 的非流式请求，不下载模型权重或启动 GPU 引擎。

支持 temperature、thinking、reasoning_effort 等适配参数；是否被服务接受由提供方决定。json_mode=false 可省略 JSON response_format，但业务输出仍必须满足任务 JSON 契约。base_url 不允许凭据、查询串或片段，HTTP 重定向被拒绝。

timeout 取 (0,600] 秒，retries 为 0–3，max_output_tokens 为 1–131072。401/403 不重试；指定的限流/服务错误和传输错误可重试，每次尝试计量。响应上限为 8 MiB。usage 缺失时保留 unknown_calls，不按零成本处理。

回放按完整 request_hash，或精确 task+input_hash 匹配唯一响应，不匹配时报错。回放只验证流程，不能用它证明上下文选择或生成质量。

启动器只从项目 `.env` 读取 DEEPSEEK_API_KEY、LOCAL_MODEL_API_KEY、DEEPSEEK_MODEL，已有非空环境变量优先。其余 CLI/API 服务从进程环境取凭据；配置中禁止明文 api_key、任意 headers 等敏感字段。

## 数据契约

### 本体与 schema

本体保留 `metadata/entities/relations` 和未修改的自定义字段。entities 表示概念：id/name/type 必需，definition/aliases 可选；关系需要 id/source/target/type。

```json
{
  "schema_version": 1,
  "concept_types": ["concept"],
  "relation_types": {
    "is_a": {
      "direction": "child_to_parent",
      "hierarchical": true,
      "source_types": ["concept"],
      "target_types": ["concept"]
    }
  }
}
```

关系方向可为 child_to_parent、parent_to_child 或 directed；层级关系必须指定父子方向。校验各层级类型及归一方向后的联合 DAG。非层级关系可设置 `acyclic: true`，按该关系类型单独检查环（包括自环），不混入分类联合 DAG；未指定时允许环。可选 `description` 为关系语义说明，会随 schema 进入 LLM 上下文。自然语言约束和 semantic_entailment 保持 unknown，不自动形式化。

### 预置 schema

`schema_preset("general")` 为新建会话默认；`schema_preset("taxonomy")` 只含 is_a。每次调用返回独立副本。上述 JSON 是最小 taxonomy 示例，不是 general 的完整内容。

| 关系 | source → target | 结构检查 |
| --- | --- | --- |
| is_a | 下位概念 → 上位概念 | 分类 DAG |
| part_of | 部分概念 → 整体概念 | 独立无环检查 |
| has_property | 承载者概念 → 属性/性质概念 | 端点、类型 |
| has_function | 承载者概念 → 功能概念 | 端点、类型 |
| participates_in | 参与者概念 → 过程概念 | 端点、类型 |
| depends_on | 依赖方概念 → 前提概念 | 端点、类型 |
| precedes | 较早过程/阶段概念 → 较晚过程/阶段概念 | 独立无环检查 |
| related_to | 来源概念 → 关联概念 | 端点、类型 |

所有端点仍为 `concept`，可表示对象、过程、性质、功能等概念，不表示具体个体。属性关系的目标是性质概念，不是数值或字符串。领域特定的类型划分及端点限制由自定义 schema 提供。

非分类关系表示有来源支持的概念级关系，适用范围可记录在 `application_conditions` / `constraints`；当前不提供 OWL 限制量词、属性值/基数推理、逆关系或传递闭包。related_to 不自动生成反向边，depends_on 不等同于因果。新增关系仍需证据、审阅及确认。schema 支持用户明确同意后的增量扩展，见 [对话扩展流程](guide.md#对话中扩展-schema)。

```python
from ob_harness.ontologies import schema_preset

# app 为已初始化的 Application；须先给目标 ontology_id 配置权限。
conversation = app.conversations.create(
    ontology_id="new_domain",
    create_spec={"schema": schema_preset("general")},
)
```

模板只用于新建，不修改已有版本；原有动物演示与回放保持其原 schema。

### 身份与变更

| 对象 | 身份/字段 |
| --- | --- |
| OntologyRef | ontology_id、version、content_hash；version 为本体与 schema 的规范 JSON 哈希 |
| ChangeSet | id/hash、schema_version、ontology_id、base_version、revision、operations、validation |
| ChangeSet v1 | add_concept/add_relation/enrich_concept；定义不覆盖；可信 CLI/Python commit |
| ChangeSet v2 | 字段修改/删除、记录前置哈希、字段支持；mode/domain_schema/submission_policy/binding 进入哈希正文 |
| Conversation | subject、ontology_id、active_draft_id、revision、active_turn_id、pending_review_id、消息引用 |
| Turn | message_id、run_id、固定 draft_revision/snapshot_ref、允许来源、配置与结果 |
| Draft | base_version、mode、revision、snapshot_ref、schema_ref、undo_stack、status |
| ReviewPacket | subject、conversation/draft/revision、候选/提案哈希、完整 diff、期限、presented/status |
| ContextPack | messages、选中/舍弃 ID、input_bytes/budget、模板哈希和来源；token_count=None |

所有 ChangeSet 不可变，当前 revision=1；草案 revision 和 run checkpoint revision 分别增长。`current` 在进入提案时解析成真实版本，不保存可变别名。

会话 v2 净操作为 add/update/delete_concept/relation；修改带 expected_record_hash。概念只允许 name/definition/aliases/type，关系更新只允许 constraints/application_conditions；ID 不可修改。unset 仅用于可选字段。任意自定义字段、已有 schema 定义改写和概念合并不属于当前对话编辑范围。

`draft_propose_schema` 是会话绑定的提议工具，参数 `{reason, additions}`；additions 可含 concept_types 数组和 relation_types 对象，每次最多新增 30 个类型。新增关系须有 description、direction、source_types、target_types，可选 hierarchical/acyclic；拒绝不支持的约束字段及已有类型重定义。该工具仅持久化提案并返回 `schema_proposal`，没有供模型调用的批准工具。

用户仍通过已有 messages 接口回复，可用 `reply_to` 指向 schema 提案 ID。宿主将肯定/否定答复绑定到 active turn，返回 `schema_updated` 或 `schema_rejected`。接受时验证 propose 权限、提案身份/状态/期限、草案版本及候选合法性；schema 更新、历史、回执和审计在同一事务内完成。草案保存 `base_schema_ref`；revision 保存 before_schema_ref/schema_ref，撤销同时恢复内容与 schema。

diff/review 新增 `schema_changes`；ChangeSet v2 的 binding 携带 `schema_approval_ids`，哈希覆盖批准链和最终 domain_schema。仓库检查批准链的会话、草案、主体、资源及前后哈希；未经批准的已有本体 schema 扩展不能进入候选。正式发布仍需要 read/write 权限和独立的“确认保存”。

### 来源与字符位置

EvidenceRef 包含 `document_id/artifact/text_hash/start/end/quote`，必须与已登记到目标本体的不可变文档精确匹配。start/end 是 Python 字符下标，左闭右开，不是 UTF-8 字节、PDF 页码或坐标。

`text_hash=digest(text)` 使用规范 JSON 字符串哈希；解析产物 metadata.text_sha256 使用原始 UTF-8 字节哈希，两者不能混用。文档模型给出 quote 后，宿主仅对唯一精确匹配补齐偏移；重复或缺失引文拒绝。会话 quote 必须唯一出现在本轮允许的用户来源中。

用户消息保存为 Document，标记 source_kind=user_message。事实支持 field_evidence 与操作 instruction_refs 分开；历史 assistant 文本不自动升级为证据。变更会保留旧快照，而不是改写用户原话。

## Python 服务

```python
from pathlib import Path
from ob_harness.services import Application

app = Application(Path("demo"))
result = app.answer("demo", "mammal")
context = app.context("demo", "mammal")
```

常用入口：

| 服务 | 方法 |
| --- | --- |
| Application | search / answer / neighbors / context / create_build / build / inspect / list_runs / resume / doctor |
| app.ontologies | import_ontology / read / versions / propose / changeset / validate / diff / commit |
| app.conversations | create / get / messages / send / resume / present；drafts.read/diff |
| app.memories | put / get / search / delete / clear |

文档构建需要异步调用并检查 status：

```python
async def build_example(app):
    run = await app.build("demo", [Path("demo/examples/part-1.txt")])
    if run["status"] != "completed":
        return run
    proposal = run["result"]
    return app.ontologies.diff(proposal["changeset_id"])
```

会话示例：

```python
from uuid import uuid4

async def send_example(app):
    service = app.conversations
    conv = service.create(ontology_id="demo")
    return await service.send(
        conv["id"], text="加入鲸，它属于Mammal",
        client_message_id=uuid4().hex,
        expected_revision=conv["revision"], planner="scripted",
    )
```

send 返回 conversation_id/revision、turn_id、run_id、status、message、result、draft。收到独立用户确认后才能再次 send 确认文本；可信传输层应先展示 review.diff，再调用 present。不要用模型自己生成的字符串调用确认服务。

接口既可能抛出前置 HarnessError，也可能返回 failed/部分结果状态；不能只用“没有异常”判断成功。旧 CLI/Python commit 不能发布会话 v2。

## CLI

| 命令族 | 用途 |
| --- | --- |
| init / doctor | 初始化、依赖/配置诊断；--probe-model 显式真实推理 |
| query / context | 正式本体检索与上下文预览 |
| build / agent | 文档草案构建及有界工具编排 |
| chat / serve | 持久对话 CLI 与可选 Web |
| import-ontology / export | 导入与单版本导出 |
| runs / inspect / resume / cancel / resolve | 执行状态、轨迹、恢复及副作用核实 |
| diff / validate / commit | v1 提案审核与显式提交 |
| memory / audit / cleanup | 长期经验、审计与引用感知清理 |
| tool-list / tool-info / tool-run | 工具发现与受绑定执行 |
| ontology-info / prepare / extract | 保留的 legacy 入口，能力边界见架构说明 |

参数以 `ob-harness COMMAND --help` 为准。JSON 业务失败/部分状态通常返回退出码 2，前置异常返回 1，Ctrl+C 返回 130；成功请求也应检查业务 payload。

## 实际 Web 接口

服务绑定一个工作区和本体。默认只读，--editable 启用会话写入；可编辑模式只允许 loopback 监听。默认没有认证，不是远程多租户 API。

| 方法与路径 | 用途 |
| --- | --- |
| GET / | 聊天页 |
| GET /api/health | 本地健康检查 |
| GET /api/info | 本体/模型信息、可用模式、editable 模式的 CSRF token |
| GET /api/ontology | 完整正式本体及 schema；固定服务绑定资源/版本，每次重查 read 权限；全景图使用 |
| POST /api/chat | 原查询 Agent；run_id 仅用于该 run 的 waiting_input 回答 |
| POST /api/conversations | 创建绑定本体的会话；max_model_calls 默认 200，范围 1–10000 |
| GET /api/conversations/{id} | 会话状态与草案概览 |
| GET /api/conversations/{id}/messages?after_seq=N | 持久消息 |
| GET /api/conversations/{id}/draft | 完整当前草案 |
| POST /api/conversations/{id}/messages | 发送消息，执行一轮或处理用户确认 |
| POST /api/conversations/{id}/resume | 恢复活动 turn |
| POST /api/conversations/{id}/reviews/{review_id}/present | 记录可信客户端已展示审阅包 |

消息请求字段：message（1–8000 字符）、client_message_id、expected_revision、planner（scripted/llm）、可选 reply_to。绑定主体、工作区和本体不由消息字段授予。用户确认完整等于“确认保存”、`/confirm` 或 `confirm save`；reply_to 可指定审阅包。

POST 会话接口要求 `X-Harness-CSRF`、合法 Host/Origin、Content-Length，体积上限 64 KiB。浏览器先从同源 /api/info 获取 token。没有 /v1、SSE、后台 Worker、独立 publish 或任意 ChangeSet PATCH 接口。
