# Ontology Harness

面向本体查询与增量构建的本地 Agent 框架。通过对话或文档提出概念、定义和关系，在草案中修改、删除、撤销，审阅后保存为正式本体版本。

提供 Python SDK、CLI 和可选 Web 界面，支持 DeepSeek API、兼容 Chat Completions 的本地服务及离线回放。要求 Python 3.11+；核心包不依赖外部数据库、研究数据集或模型权重。

## 快速开始

以下命令在包含 `pyproject.toml` 的项目根目录执行：

```powershell
python -m pip install -e ".[web]"
```

按照 [.env.example](.env.example) 创建 `.env`，填写自己的 `DEEPSEEK_API_KEY`；已有文件保留原内容。然后运行：

```powershell
python scripts/launch.py
```

启动器读取 `.env`，首次创建 `workspace/`、配置 DeepSeek 并打开浏览器。再次运行沿用已有工作区和配置。Windows 也可双击 [start.bat](start.bat)；已有 `.venv` 的启动命令见 [用户指南](docs/guide.md)。

在页面中依次输入：

```text
加入鲸，它属于 Mammal
把鲸的定义改成“生活在水中的哺乳动物”
查看差异
保存
确认保存
```

“保存”展示修改和来源；“确认保存”才创建正式版本。关闭服务后，草案、消息和正式版本仍保留在工作区。调用真实模型会产生提供方费用。

没有 API key 也可以先运行离线查询：

```powershell
python -m pip install -e .
ob-harness init demo --writable
ob-harness query --workspace demo --ontology demo --query mammal
```

## 当前能力

| 能力 | 实现 |
| --- | --- |
| 本体管理 | JSON 概念/关系、领域 schema、层级 DAG、不可变版本、导入导出 |
| 对话构建 | 持久会话、用户消息证据、多轮草案、新增/修改/删除/撤销、固定版本确认 |
| 文档构建 | 文档快照、字符分块、累计草案、证据和结构校验、显式 ChangeSet 提交 |
| 上下文 | none / keyword / all、可选一跳邻居、完整对象和关系端点、UTF-8 字节预算 |
| Agent 与恢复 | 脚本/LLM Planner、受绑定工具、调用预算、检查点、取消和不确定副作用核实 |
| 记忆与治理 | 显式经验存储、资源级 read/propose/write、幂等、版本冲突和审计 |
| 工具扩展 | Python entry point 插件；MinerU 可单独安装 |

当前面向可信本地环境。没有远程用户认证、完整 OWL 推理或 RL 上下文策略训练；结构校验不证明自然语言定义正确。通用自然语言需配置真实模型，scripted 只提供有限离线语法。

## 文档

- [用户指南](docs/guide.md)：启动、对话保存、文档构建、恢复和备份。
- [架构说明](docs/architecture.md)：六个模块、上下文机制、草案与提交事务。
- [接口参考](docs/reference.md)：配置、数据契约、Python、CLI 和实际 Web 接口。
- [工具扩展](docs/tools.md)：工具绑定、插件与 [MinerU](plugins/mineru/README.md)。
- [验证记录](docs/validation.md)：离线测试、真实 API 验证和未覆盖范围。
- [贡献指南](CONTRIBUTING.md)、[安全说明](SECURITY.md)、[路线图](TODO.md)、[发布清单](RELEASE.md)。

## 项目状态

当前包版本为 0.2.0；后续对话构建能力以源码交付，旧发行包不包含全部新功能。最近记录：Windows / Python 3.11 的 50 项离线测试通过，真实 DeepSeek 小样例完成多轮构建与确认提交；新增 schema 协商尚未在线验收，具体范围见验证记录。其他平台、页面视觉、MinerU 真实解析及系统性语义质量评估仍待验收。

公开发布前还需确定许可证、版权归属、维护者及安全联系渠道，并重新构建当前源码的发行包。当前尚未附 `LICENSE`，详见发布清单。
