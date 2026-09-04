# AgentG — Orchestrator–Worker 多智能体系统

AgentG 是基于 **LangGraph** 的可控 Multi-Agent / Agentic Workflow。系统先判断任务应当“简单直通”还是“复杂组队”；复杂任务由 Orchestrator 拆成结构化子任务，再交给 ResearchAgent、CodeAgent、DataAgent 和 SynthesisAgent。无依赖子任务并行 Fan-out，有依赖任务按拓扑顺序串行，最后由结构化 Reviewer 验收并定向返工。

> 本文档兼作工作进度记录。最近核对日期：**2026-09-05**。

## 当前进度

| 模块 | 状态 | 实现 |
|---|---|---|
| Task Router | ✅ | 类型化判断 `simple/team`；简单任务选择一个专业 Worker |
| Orchestrator | ✅ | 输出含 Agent、依赖、工具、格式和验收标准的 `SubTaskSpec[]` |
| 四类 Worker | ✅ | Research、Code、Data、Synthesis，可继续扩展 |
| Fan-out / Fan-in | ✅ | Ready batch 内并行，汇聚后再调度下一层依赖 |
| 上下文隔离 | ✅ | Worker 只接收当前任务所需的最小上下文 |
| 结构化 Reviewer | ✅ | 输出 `pass/revise/blocked`、失败标准、返工目标、反馈和置信度 |
| 定向返工 | ✅ | 只重跑目标任务及受影响的下游，不重跑无关分支 |
| 完整工具循环 | ✅ | `AIMessage(tool_calls)` → 工具执行 → `ToolMessage` → 模型，支持多轮观察 |
| 资源终止策略 | ✅ | 限制时间、工具轮次/次数、连续错误和动作总预算；暂不计算 Token |
| 实时天气工具 | ✅ | 当前天气任务自动使用 Open-Meteo 结构化接口，不再依赖搜索摘要 |
| CLI / Web / SSE | ✅ | 已适配新节点、并行合并状态和历史展示 |
| MemorySaver | 🟡 | 仅为单进程内 LangGraph checkpoint |
| Redis | 🟡 | CLI 可选最终快照，尚未接入图 checkpoint 和 Web 历史 |
| JSON Mode 兼容性 | ✅ | 显式声明 `json` 字段，并规范化常见的键名和单项数组偏差 |
| 自动化测试 | ✅ | Docs2KG / Python 3.11.9 中 **107 项通过** |

### 最新验证

```text
环境：Conda Docs2KG / Python 3.11.9
命令：D:\anconda\envs\Docs2KG\python.exe -m pytest -q -p no:cacheprovider
结果：107 passed in 8.40s
日期：2026-09-05
```

测试通过 mock 隔离 ChatOpenAI、DuckDuckGo、HTTP 和 Redis，不需要真实 API Key 或在线服务；测试入口还会禁用 dotenv，避免读取用户的 `.env`。`compileall` 同期通过。

## 工作流

```text
START → Initialize Resources → Task Router
          ├─ simple → Prepare Simple Task ───────────────┐
          └─ team → Orchestrator → SubTask DAG ─────────┤
                                                         ↓
                                                   Prepare Batch
                                                         ↓
                     ┌───────────────────────────────────┼──────────────────┐
                     ↓                                   ↓                  ↓
              ResearchAgent                         CodeAgent          DataAgent
                     └───────────────────────────────────┼──────────────────┘
                                                         ↓
                                                      Fan-in
                                                         ↓
                                  尚有依赖任务 ──→ Prepare Batch
                                                         │
                                                         ↓
                                                  SynthesisAgent
                                                         ↓
                                                     Reviewer
                                     pass → END | blocked → END
                                                revise ↓
                                  revision_targets + 下游依赖闭包
                                                         ↓
                                                   Prepare Batch

任一阶段触发资源上限 → Resource Terminated（blocked）→ END
```

实际 Worker 组合由 Orchestrator 决定，并非所有任务都要使用全部四类 Agent。

## 核心设计

### 简单直通与复杂组队

Task Router 结构化输出：

```text
route: simple | team
worker_type: research | code | data | synthesis
reason: string
```

- `simple` 创建一个 `simple-1` 子任务，绕过 Orchestrator，直接进入所选 Worker。
- `team` 由 Orchestrator 生成子任务 DAG。

三个 `json_mode` 节点都会在提示词中声明准确的 JSON 字段。Task Router 还会兼容部分模型返回的 `{"type":"team"}`：解析前将合法的 `type` 别名规范化为 `route`，缺失的 `reason` 使用可识别的默认说明；其他非法路由值仍会触发校验错误。

### Orchestrator–Worker

每个 `SubTaskSpec` 包含：

| 字段 | 说明 |
|---|---|
| `id` | 唯一子任务 ID，也是定向返工目标 |
| `agent_type` | `research/code/data/synthesis` |
| `objective` | Worker 不看完整对话也能理解的自足目标 |
| `dependencies` | 前置子任务 ID |
| `allowed_tools` | 最小工具白名单 |
| `output_format` | 期望输出格式 |
| `acceptance_criteria` | 可验证的验收标准 |

Orchestrator 会清理重复 ID、无效依赖和越权工具。若检测到循环依赖，计划会按声明顺序降级为串行。复杂计划末尾会确保存在最终 SynthesisAgent。

为兼容部分 OpenAI-compatible 模型的 JSON Mode 输出偏差，`dependencies`、`allowed_tools` 和 `acceptance_criteria` 在解析时允许单个字符串，并立即规范化为单元素列表；内部状态和后续调度仍只使用类型安全的 `list[str]`。提示词同时明确要求这三个字段始终输出 JSON 数组，即使只有一项也不能使用字符串。

### 专业 Worker 与工具权限

| Worker | 职责 | 默认工具 |
|---|---|---|
| ResearchAgent | 搜索、实时天气、来源整理和事实核验 | `web_search`、`current_weather` |
| CodeAgent | Python 设计、执行和技术实现 | `python_repl` |
| DataAgent | 数据读取、清洗、统计和表格处理 | `python_repl` |
| SynthesisAgent | 整合必要上游结果，形成最终答案 | 无 |

运行时只绑定角色工具箱与 `allowed_tools` 的交集；模型发出的未授权工具调用会被拒绝。

### 依赖感知 Fan-out / Fan-in

调度器从 `pending_task_ids` 中选择依赖均已完成的 ready batch，通过 LangGraph `Send` 动态分发。并行 Worker 结果由 reducer 合并到 `worker_results`，Fan-in 完成后再计算下一批。因此：

- 同一批没有相互依赖的任务可以并行；
- 有前置依赖的任务不会提前启动；
- 每个依赖只收到必要上游任务的最新结果。

### 结构化审查与定向返工

Reviewer 输出：

```text
status: pass | revise | blocked
failed_criteria: list[str]
revision_targets: list[subtask_id]
feedback: str
confidence: 0.0 .. 1.0
```

路由直接使用类型化 `status`，不再解析 `REWORK` 文本。`revision_targets` 会过滤为真实任务 ID；返工集合等于目标任务及其下游依赖闭包。例如 ResearchAgent 需要修订时，只重跑 ResearchAgent 和依赖它的 SynthesisAgent，独立的 DataAgent 不会重跑。

当前最多审查 2 轮，即首次执行后最多 1 轮定向返工。

### 共享状态与上下文隔离

`MultiAgentState` 是主图共享黑板，保存路由、计划、子任务、批次、版本化 Worker 结果、审查决定、错误、代码和消息。

`WorkerInputState` 是记忆与上下文隔离边界。Worker 只接收：

1. 自己的任务目标；
2. 直接依赖的最新上游结果；
3. 允许使用的工具；
4. 输出格式；
5. 验收标准；
6. 定向返工反馈和当前执行序号；
7. 当前 Worker 的截止时间、工具额度和动作预算切片。

Worker 不接收完整用户对话、完整主图状态或无关分支结果。

## 工具调用

- `web_search`：优先使用维护中的 `ddgs` 元搜索包，失败后降级到 DuckDuckGo HTML 和 Lite 页面；最终失败时返回不含敏感连接信息的异常类型诊断。
- `current_weather`：先通过 Open-Meteo Geocoding API 解析城市，再查询当前温度、体感温度、湿度、天气代码和风况；无需 API Key。当前或实时天气子任务会在计划规范化阶段自动切换到该工具。
- `python_repl`：在当前 Python 进程运行代码并捕获 stdout/异常。

CLI 和 Web 启动入口不再清空 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等系统代理变量，因此外部工具可以遵循用户运行环境的网络配置。

工具通过 LangChain `@tool` 注册。专业 Worker 使用完整的工具观察循环：模型返回含 `tool_calls` 的 `AIMessage` 后，系统校验角色工具箱与子任务白名单，执行获准工具，为每个调用生成具有相同 `tool_call_id` 的 `ToolMessage`，然后把对话历史再次提交给模型。循环持续到模型返回不含工具调用的最终答复，或触发资源上限。Worker 结果会记录 `tool_observations`、`usage` 和可选的 `termination_reason`。

### 资源终止策略

一次图运行在开始时固定资源策略与截止时间：

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `MAX_TOOL_ROUNDS` | `4` | 单个 Worker 最多执行的工具观察轮次 |
| `MAX_TOOL_CALLS` | `12` | 整个工作流最多实际执行的工具调用数 |
| `MAX_CONSECUTIVE_ERRORS` | `2` | 单个 Worker 连续失败达到该值即终止 |
| `MAX_RUN_SECONDS` | `120` | 整个工作流的协作式墙钟截止时间 |
| `TOTAL_ACTION_BUDGET` | `40` | 整个工作流可用动作单位总数 |

动作预算当前定义为：一次模型调用消耗 1，一次实际工具执行消耗 1。Token 不计入终止策略，`token_budget_enabled` 固定为 `false`。Router、Orchestrator、Worker 和 Reviewer 的模型调用都会计数；拒绝或因限额而未执行的工具不计工具次数，也不消耗工具动作。

并行 Fan-out 前，调度器按剩余总量为同一批 Worker 分配互不重叠的工具与动作额度，防止并发超额；Fan-in 汇总实际消耗，未用额度可由后续批次继续使用。触发上限后，状态记录 `termination.source/reason`，工作流进入结构化 `blocked` 终态。时间检查发生在模型或工具调用之间，不能中断已经卡住的进程内 Python 代码或正在进行的外部调用。

## 项目结构

```text
AgentG/
├── README.md
├── AGENTS.md
├── requirements.txt / pyproject.toml
├── Dockerfile / docker-compose.yml
├── src/multi_agent_system/
│   ├── agents.py                 # Router、Orchestrator、Workers、Reviewer、工具
│   ├── graph.py                  # 批次调度、Send、Fan-in、定向返工
│   ├── state.py                  # 主状态、Worker 隔离状态、reducers
│   ├── main.py                   # CLI
│   ├── web_server.py             # FastAPI / SSE / 内存任务历史
│   ├── storage.py                # Redis 最终快照
│   ├── web_templates/index.html
│   └── workspace/                # 不可信运行产物
└── tests/                        # 单元、图、Web 和集成测试
```

## 启动方式

AgentG 支持两种启动方式：本地 Python 环境运行，以及 Docker Compose 容器部署。两种方式都需要先在项目根目录创建 `.env` 并配置可用的模型 API。

### 公共配置

从 `.env.example` 创建本地配置文件：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```ini
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
USE_REDIS=false
REDIS_URL=redis://localhost:6379/0
MAX_TOOL_ROUNDS=4
MAX_TOOL_CALLS=12
MAX_CONSECUTIVE_ERRORS=2
MAX_RUN_SECONDS=120
TOTAL_ACTION_BUDGET=40
```

`.env` 是用户专属保护文件，只能由用户本人读取和修改。项目 Agent 不得读取、查看、打印、修改、覆盖、移动或删除该文件；自动化测试使用 mock 和临时环境变量，不依赖读取真实 `.env`。同时不要提交真实 `.env` 或 API Key。

### 方式一：本地运行

要求本机安装 Python 3.10+。以下命令在项目根目录执行。

1. 创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

从旧版本升级时也需要重新执行安装命令，以安装已更名的 `ddgs` 搜索依赖；Docker 用户需要重新执行 `docker compose up --build -d` 构建镜像。

2. 根据使用场景选择一种入口：

```powershell
# 命令行交互模式
.\.venv\Scripts\python.exe -m src.multi_agent_system.main

# Web 服务模式
.\.venv\Scripts\python.exe -m src.multi_agent_system.web_server
```

Web 服务启动后访问：<http://localhost:8000>。命令行模式会提示输入任务描述，并在终端持续输出各工作流节点的结果。

如需停止 Web 服务，在运行它的终端按 `Ctrl+C`。

### 方式二：Docker Compose 容器部署

要求本机安装 Docker Desktop 或其他支持 Docker Compose 的运行环境。Docker 镜像默认启动 Web 服务，容器内监听 `8000`，宿主机映射到 `8080`。

1. 确认项目根目录已经存在配置好的 `.env`。

2. 构建并在后台启动：

```powershell
docker compose up --build -d
```

3. 查看容器状态和日志：

```powershell
docker compose ps
docker compose logs -f agentg
```

启动成功后访问：<http://localhost:8080>。

4. 停止并移除本项目容器：

```powershell
docker compose down
```

`docker-compose.yml` 会把本地 `src/` 映射到容器 `/app/src`，因此源码修改会立即反映到容器文件系统；Python 进程是否自动加载修改取决于服务器启动配置，必要时执行 `docker compose restart agentg`。

Redis 默认关闭。若启用 Compose 中预留的 Redis 服务，需要取消 `redis` 服务和 `depends_on` 的注释，并将容器使用的地址配置为 `REDIS_URL=redis://redis:6379/0`。当前 Redis 只用于 CLI 结束后的最终状态快照，Docker 默认启动的是 Web 服务，因此通常保持 `USE_REDIS=false`。

### 验证安装

本项目已在 Docs2KG / Python 3.11.9 环境中验证。测试不需要真实 API Key 或在线服务：

```powershell
D:\anconda\envs\Docs2KG\python.exe -m pytest -q -p no:cacheprovider

# 使用当前虚拟环境也可以运行
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

## Web API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | Web 页面 |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/tasks` | 创建任务，JSON 为 `{"task": "..."}` |
| `GET` | `/api/tasks` | 列出内存任务历史 |
| `GET` | `/api/tasks/{id}` | 获取路由、子任务、Worker 结果、资源用量和审查详情 |
| `GET` | `/api/tasks/{id}/stream` | 启动任务并通过 SSE 推送节点事件 |
| `DELETE` | `/api/tasks/{id}` | 删除内存任务记录 |

创建任务后必须连接 `/stream` 才会真正执行；同一任务只能启动一次。

## 已知限制与风险

- `python_repl` 使用进程内 `exec()`，没有权限隔离或可抢占的硬超时；墙钟策略只能在调用边界检查，不能中断死循环，不能直接面向不可信公网用户。
- 四类 Agent 共享同一个 `ChatOpenAI` 实例和模型配置，不是独立进程或服务。
- `MemorySaver`、Web `_task_store` 和 CLI Redis 快照尚未统一为可恢复持久化。
- Redis 快照仍需完善 LangChain 消息对象的序列化适配。
- `workspace/agent_generated_code_*.py` 会按序号覆盖，并发任务没有独立目录。
- Web 缺少认证、限流、取消、输入长度限制和人工审批。
- 当前没有 `.gitignore`；初始化版本控制前应忽略 `.env`、`.venv/`、`__pycache__/` 和运行产物。

## 后续优化与可借鉴模式

以下内容按本轮要求留待后续：

1. 持久化恢复：统一 LangGraph checkpoint、Web 历史和 Redis。
2. 硬超时与隔离：把 Python 工具迁移到可终止的受限子进程或容器。
3. Token 预算：待确认模型供应商的可靠 usage 元数据后，再纳入统一预算。
4. Human-in-the-loop：危险代码、文件写入、外部副作用和低置信度结果需审批。
5. 工作区隔离：按 `thread_id/task_id` 建目录并限制文件访问范围。
6. 可观测性与评测：补充 Token、返工原因、产物来源和跨运行指标聚合。
7. 模型分层：Orchestrator/Reviewer 使用强模型，低风险 Worker 使用轻量模型。
8. 动态 Agent 注册：专业 Agent 数量明显增加后再引入能力发现和按需加载。
9. 多 Reviewer / Debate：只对安全、合规或高价值结果启用投票、仲裁。
10. A2A：Agent 拆成跨团队、跨框架的独立远程服务后再引入。
11. 工程治理：同步依赖、补 `.gitignore`、认证、限流、取消和生产部署配置。

## 当前不建议直接采用的模式

- **自由 Group Chat / Selector Chat**：容易重复工作、膨胀上下文、提高成本并使终止困难；仅适合特殊讨论或评审节点。
- **全量 Handoff / Swarm**：更适合由不同角色长期接管用户会话；当前一次性任务由中央 Orchestrator 更可控。
- **现在引入 A2A**：三个 Agent 仍在同一进程和代码库，引入服务发现、认证和网络一致性成本过早。
- **默认多 Agent Debate**：延迟和成本较高，应按风险选择性启用，不能替代结构化 Reviewer。

## 技术栈

- LangGraph / LangChain / langchain-openai
- Pydantic 结构化输出
- FastAPI / Uvicorn / Jinja2 / SSE
- Redis（当前为可选最终快照）
- pandas / openpyxl / ddgs / BeautifulSoup
- pytest
