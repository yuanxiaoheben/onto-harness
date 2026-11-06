# 验证与已知限制

本页合并源码测试、真实 API 样例和代码审查结果。它记录实际执行范围，不将 CI 配置、可选依赖安装或设计方案当作已完成验收。下面数据来自已有运行记录；本次文档整理不重跑计费推理。

## 当前源码记录

全景图增量验证：3 项 Web API 测试通过，覆盖正式/草案隔离、权限撤销后拒绝读取、资源/版本绑定及静态资源。Chrome headless 实际页面验证覆盖 20 节点/20 条关系、视图切换保留输入、中文定位、缩放、节点详情、正式 20/草案 21 节点隔离以及 390px 移动视口无横向溢出；浏览器无 JavaScript 异常。桌面和移动截图已查看，结果位于本地 `outputs/graph-browser-results.json`、`graph-desktop.png`、`graph-mobile.png`。该验证不调用外部模型。SVG 当前针对小型本体，大图性能尚未验收。

预置脚本在临时工作区验证 20 个概念、DAG 合法、中文检索、重复执行无变更和历史版本保留；默认工作区已正常提交预置内容。未覆盖已有其他领域本体的自动对齐质量。

最近完整套件：2026-11-06，Windows / Python 3.11，含 Web 和 LangGraph，**50 项通过、0 失败、0 跳过**。schema 预置模板增加 4 项测试，对话 schema 协商再增加 10 项，覆盖同意/拒绝、过期及旧草案绑定、权限撤销、重启恢复与确认幂等、原需求证据续接、撤销、连续扩展、新建/已有本体正式保存及未批准候选拒绝。日志保存在本地忽略目录 `outputs/schema-full-tests.log`，未调用外部模型。此前会话审计/状态调整复核的会话和 Web 子集为 **18 项全部通过**。

本次修改文件 Ruff 检查与 Web JavaScript 语法检查通过；全项目 Ruff 另发现原有 `tests/test_web.py` 导入分组问题，本次未修改该文件。schema 协商测试使用可控规划器替身；真实 LLM 识别缺失类型、提出扩展及新增非分类关系的抽取质量尚未在线验收，新增按钮尚未进行浏览器视觉验收。

| 范围 | 覆盖 |
| --- | --- |
| 文档构建 | 输入快照、分块、前一草案进入后续上下文、证据定位、结构拒绝 |
| 版本与权限 | read/propose/write、提交幂等、基础版本冲突、current 别名冻结 |
| 持久执行 | 真实子进程中断、取消、工具副作用未知、核实后恢复、可选 LangGraph |
| 对话编辑 | 多轮新增/纠正/改名、删除范围、连续撤销、稳定 ID、消息去重 |
| 对话提交 | 旧/过期确认拒绝、权限刷新、空白本体首次发布、事务失败回滚 |
| 会话恢复 | 编辑已落地但 Agent 未保存、Agent 完成而 Turn 未完成、来源清理保护 |
| 模型与接口 | HTTP 替身、429 计量、401 脱敏、拒绝重定向、Web CSRF/Origin/只读边界 |
| 上下文与记忆 | 字节裁剪、关系端点闭合、命名空间/版本/到期过滤 |

CLI 另以子进程验证了“新增 → 审阅 → 确认 → 同 key 重试”，只发布一次。Ruff、Node 脚本语法检查及非 strict Mypy（当时 61 个主包文件）通过；Mypy 参数为 `--ignore-missing-imports --follow-imports=silent`，不等于完整静态证明。

本地机器记录如 outputs/conversation-test-results.json、conversation-final-checks.json 和 conversation-cli-results.json 属于忽略产物，不作为项目运行依赖或公开数据。

## 真实 DeepSeek 会话验证

2026-11-06 使用账户可用的 deepseek-flash，JSON 非流式、thinking disabled、最大输出 2048、重试 0。测试数据为原创 Animal/Mammal 种子；没有使用私人文档或修改既有本体。

| 步骤 | 结果 |
| --- | --- |
| 新增鲸 → Mammal，下一轮新增蓝鲸 → 鲸 | 接受并在后续草案引用 |
| 更正鲸定义、蓝鲸改名 | 内容正确替换，ID 保持 |
| 撤销改名 | 恢复名称及原 ID |
| 请求删除但未授权处理关联边 | waiting_input，草案不变 |
| 用户明确范围后删除鲸及关联边 | 保留子概念；再次撤销恢复节点和全部关系 |
| 审阅、独立确认、确认重试 | 正式版本只从 1 增至 2 |
| 提交后查询、Web 后端 LLM 查询 | completed；ASGI TestClient 返回 HTTP 200 |

最终为 4 个概念、3 条层级关系：Mammal → Animal、鲸 → Mammal、蓝鲸 → 鲸。鲸定义为“生活在水中的哺乳动物类群”，蓝鲸为“一种鲸”。构建和撤销期间正式版本未改变。

| 范围 | 实际模型调用 | 输入 tokens | 输出 tokens | unknown_calls |
| --- | ---: | ---: | ---: | ---: |
| 会话链路（13 次消息调用，含确认重试） | 8 | 15,765 | 839 | 0 |
| Web 后端额外查询 | 1 | 2,243 | 30 | 0 |
| 合计 | **9** | **18,008** | **869** | **0** |

持久响应均 replay=false。保存、撤销和确认不额外调用模型。上述是提供方 usage，不推算账单。测试目录的产物、报告和数据库扫描未发现实际 key；密钥只注入测试进程环境。

## 历史验证与修复

| 时间/对象 | 记录 |
| --- | --- |
| 2026-10-30 原 MVP | Windows / CPython 3.11.17：17 项源码通过；独立核心 wheel 为 16 通过、1 个可选 LangGraph 跳过 |
| 2026-10-31 审查 | 修复 Python propose 将 current 字符串写入 ChangeSet 的问题，改为真实版本哈希；18 项通过 |
| Web 初版 | 安装 Web 依赖时记录 22 项通过；这是会话新增前的历史数量 |
| 原 DeepSeek 文档样例 | 两段文本构建、校验、提交和 Planner 查询通过；最终 4 概念/3 关系 |

真实调用曾发现并修复：模型证据偏移不准（改由宿主锚定精确 quote）、Planner 多余 JSON 字段（严格契约）、重复检索（有界摘要补充实际概念与终态查询指引）。早期生成仍出现来源未显式支持的修饰词，验证器不因此宣称 semantic_entailment=true。

旧 wheel 的安装通过不表示当前全部源码已经重新打包；发布前必须从当前源码重建并独立安装验证。已有构建产物的时间范围不能与最新源码混用。

## 复现

普通离线测试不调用 API：

```powershell
python -m pip install -e ".[dev,web,agent]" httpx
python -m unittest discover -s tests -v
```

最小核心安装可能按预期跳过可选依赖用例，需同时报告 skipped 数量。CI 配置列出目标环境，不代表远程任务已经运行。

显式真实推理，使用新的工作区名称并承担调用费用：

```powershell
# 文档构建及原 Planner
python scripts/live_smoke.py --workspace outputs/live-document-new --model your-model-id --dotenv .env --planner
# 对话构建完整链路，默认不含额外 Web 查询
python scripts/live_conversation.py --workspace outputs/live-conversation-new --model your-model-id --dotenv .env
```

脚本拒绝覆盖已有工作区；仅显式 --dotenv 读取该文件，失败保存报告并非零退出。账户模型标识需自行确认。

## 尚未覆盖

- Linux、其他 Python 版本和远程 CI 实际执行结果。
- 浏览器视觉及真实点击流程；Web 后端测试不等于页面验收。
- 真实本地 GPU 服务、长会话/大本体的性能和稳定性。
- MinerU 引擎、权重、设备及真实 PDF 解析。仅包装以 --no-deps 安装，发现/缺引擎拒绝路径通过。
- 系统性本体语义质量、上下文策略有效性和 RL 学习效果。

平台范围和未覆盖项在发布时应再次核对；不要从小样例通过推导通用自然语言成功率。
