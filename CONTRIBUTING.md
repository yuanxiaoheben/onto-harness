# 贡献指南

要求 Python 3.11+。项目根目录是包含 pyproject.toml 的目录；不需要相邻的研究仓库或共享数据集。

## 开发环境

```powershell
python -m pip install -e ".[dev,web,agent]" httpx
python -m unittest discover -s tests -v
```

Web 测试使用 TestClient，需要 httpx；不安装可选依赖时部分用例按预期跳过。文档 loader 可按需额外安装 `.[documents]`。普通测试使用临时工作区、回放和本地 HTTP 替身，不访问真实 API。

```powershell
ruff check --config pyproject.toml src tests scripts plugins/mineru/src
ruff format --config pyproject.toml --check src tests scripts
mypy --ignore-missing-imports --follow-imports=silent src/ob_harness
```

实际平台和历史结果统一记录在 [验证记录](docs/validation.md)。CI 配置不代表任务已经执行；真实模型/PDF 检查需显式启用、记录环境和调用用量。

## 代码边界

- 领域逻辑进入 services/、ontologies/ 等服务；CLI、Web 和工具共同复用。
- 修改本体时保留来源与版本前置条件，不绕过 ChangeSet/草案验证。
- 对话确认来自真实用户事件，不能给默认 Planner 注册直接提交或批准工具。
- 新检索/上下文实现保留完整记录、端点闭合、预算单位和选择追踪；当前默认算法直接由服务组装。
- 新工具声明 schema、effect、version、timeout 和真实幂等保证；内部仍执行领域授权。插件是可信 Python 扩展。
- 行为变化需考虑恢复签名、配置/操作协议兼容和累计预算。不要把数据库 schema、ChangeSet schema 和草案 revision 混用。

## 文档与提交说明

说明具体触发条件、修改后行为、验证和实际限制。关键数据/事务变化应有针对性不变量测试；纯文字或排版调整以链接/示例检查为主。

文档按 [目录](docs/README.md) 分工：使用放 guide，架构放 architecture，字段/API 放 reference，插件放 tools，结果放 validation，未实现项放 TODO。避免为同一功能再创建一份独立的“MVP/实现计划/最新说明”。变更记录只记录版本行为，不复制整份手册。

不提交 .env、私人文档、工作区、原始数据库、权重或本地运行产物。许可证、维护者及安全联系方式尚待首发前补齐，跟踪于 [发布清单](RELEASE.md)。
