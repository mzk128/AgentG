# AgentG — Orchestrator–Worker 多智能体系统

AgentG 是基于 **LangGraph** 的可控 Multi-Agent / Agentic Workflow。系统先判断任务应当“简单直通”还是“复杂组队”；复杂任务由 Orchestrator 拆成结构化子任务，再交给 ResearchAgent、CodeAgent、DataAgent 和 SynthesisAgent。无依赖子任务并行 Fan-out，有依赖任务按拓扑顺序串行，最后由结构化 Reviewer 验收并定向返工。

> 本文档兼作工作进度记录。最近核对日期：**2026-09-07**。

## 当前进度

| 模块 | 状态 | 实现 |
|---|---|---|
| Task Router | ✅ | 类型化判断 `simple/team`；含多城市天气的并行任务确定性校正为 team，单城市实时天气限定 ResearchAgent |
| Orchestrator | ✅ | 输出含 Agent、依赖、工具、格式和验收标准的 `SubTaskSpec[]` |
| 四类 Worker | ✅ | Research、Code、Data、Synthesis，可继续扩展 |
| Fan-out / Fan-in | ✅ | Ready batch 内并行，汇聚后再调度下一层依赖 |
| 上下文隔离 | ✅ | Worker 只接收当前任务所需的最小上下文 |
| 结构化 Reviewer | ✅ | 输出 `pass/revise/blocked`、失败标准、返工目标、反馈和置信度 |
| 定向返工 | ✅ | 只重跑目标任务及受影响的下游，不重跑无关分支 |
| 完整工具循环 | ✅ | `AIMessage(tool_calls)` → 工具执行 → `ToolMessage` → 模型，支持多轮观察 |
| 资源终止策略 | ✅ | 限制时间、工具轮次/次数、连续错误和动作总预算；暂不计算 Token |
| 实时天气工具 | ✅ | 当前天气任务自动使用 Open-Meteo 结构化接口，不再依赖搜索摘要 |
| CLI / Web / SSE | ✅ | Agent 工作台 UI、可滚动会话时间线、Codex 式底部输入区、运行进度恢复和统一状态 |
| UI 风格 | ✅ | 左下角可切换石墨黑、深海蓝和纸张白，选择保存在浏览器本地 |
| 任务过程呈现 | ✅ | 执行步骤可逐项收起/展开；终态默认收起技术过程，JSON 结果自动转换为用户可读自然语言 |
| 会话任务导航 | ✅ | DeepSeek 风格的居中细刻度轨道；位于最右滚动条左侧，悬停/聚焦展开任务列表 |
| 品牌 Logo | ✅ | 已选用 `01-researcher-v2` 编号 01 `Researcher Core` 作为主产品 Logo，并通过共享的内联 `currentColor` SVG Symbol 接入 Web 左上角与主页面空状态；保留两组共 12 套探索资产供扩展场景使用 |
| 多轮会话 | ✅ | 完成后可在同一会话继续；每轮使用独立 `thread_id` 和 checkpoint，通过会话链关联 |
| 会话管理 | ✅ | 左侧会话右键可置顶/取消置顶、重命名或删除整会话；运行中禁止删除 |
| 本地工作目录 | ✅（逻辑约束） | Web 可绑定已存在的绝对路径；会话内继承并锁定，Python 相对路径从该目录解析 |
| Web 开发热加载 | ✅ | `--reload` 或 `WEB_RELOAD=true` 显式启用，仅监视源码/模板并排除运行产物 |
| 持久化恢复 | ✅ | 默认使用 MemorySaver；启用 Redis 后持久化 LangGraph checkpoint、pending writes 与 Web 历史，可按 `thread_id` 恢复 |
| Redis 3.2 兼容 | ✅ | 自定义 `BaseCheckpointSaver` 使用 RESP2 与 String/Hash/Set/ZSet，不依赖 RedisJSON 或 RediSearch |
| JSON Mode 兼容性 | ✅ | 显式声明 `json` 字段，并规范化常见的键名和单项数组偏差 |
| 自动化测试 | ✅ | Docs2KG / Python 3.11.9 中 **147 项通过** |

### 最近完成（2026-09-07）

1. **会话滚动**：将中央时间线改为唯一纵向滚动容器，取消单张 Agent 输出卡片的固定高度与嵌套滚动，长任务可连续回看。
2. **任务结束后继续对话**：会话与单次运行分离，终态后可继续新一轮；每轮使用新 `thread_id`，但共享 `conversation_id` 并通过 `parent_thread_id` 维持历史链。
3. **本地工作目录录入**：Web 底部输入区可录入已存在的绝对路径，后端完成规范化和目录校验，会话续接时自动继承且禁止切换已绑定目录。
4. **上下文与产物隔离**：后续轮次只携带历史任务和最终结果的精简上下文；生成代码按工作目录和 `thread_id` 分目录保存。
5. **底部 UI 与风格切换**：工作目录和执行按钮收入输入卡片工具栏，代码产物和快捷键提示放在最底层；左下角新增三套可持久化主题。
6. **会话管理**：会话右键菜单支持置顶、重命名和带确认的整会话删除；删除会同步清理全部轮次及 Redis checkpoints。
7. **Web 热加载**：新增显式开发模式，可监视 Python 源码与 HTML/CSS/JavaScript 模板，同时排除 `workspace/`、`.agentg/`和缓存。
8. **步骤折叠**：每个 Agent 执行步骤提供独立收起/展开按钮；终态任务默认折叠技术过程，运行中的新步骤保持展开。
9. **会话任务导航**：右侧改为 DeepSeek 风格的极窄刻度轨道；内容滚动条固定在主区域最右侧，导航列位于滚动条左侧并在会话可视区垂直居中。悬停或键盘聚焦时展开圆角任务列表，当前轮次使用蓝色文字与加长刻度标记，点击后平滑定位并跟随滚动高亮。
10. **用户结果总结**：成功、Reviewer 未通过、执行异常和资源终止均生成普通用户可理解的终态摘要；纯 JSON 或 JSON 代码块会转换为自然语言段落和编号结果，不暴露私有思维链，也不额外调用模型。
11. **验证**：Docs2KG / Python 3.11.9 全量 `147 passed`，并完成本地浏览器折叠、导航、成功/失败总结、三主题 Logo 与 760 px 窄屏布局核对。
12. **Logo 选型与接入**：在两组共 12 套概念中选定 `01-researcher-v2` 编号 01 `Researcher Core`。正式矢量资产继续使用透明背景、`viewBox="0 0 100 100"` 与 `currentColor`；Web 通过同一个内联 SVG Symbol 同时渲染左上角品牌标识与主页面空状态 Logo，避免两处图形漂移。

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

模型路由之后还会执行窄范围能力校正：包含多个或随机生成城市的天气任务强制进入 `team`，由 Orchestrator 拆分可并行查询；单城市当前/实时天气若走 `simple`，则强制使用拥有 `current_weather` 权限的 ResearchAgent。校正不覆盖历史天气数据分析等其他任务，避免用领域关键词替代通用路由判断。

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

为兼容部分 OpenAI-compatible 模型的 JSON Mode 输出偏差，`dependencies`、`allowed_tools` 和 `acceptance_criteria` 在解析时允许单个字符串，并立即规范化为单元素列表；`output_format` 则允许模型返回单元素字符串数组，并立即解包为正式的字符串类型。多元素或非字符串 `output_format` 仍会校验失败，不会把含糊数据送入调度器。提示词同时明确前三个字段必须始终输出 JSON 数组，而 `output_format` 必须始终输出 JSON 字符串。

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

### 多轮会话与工作目录

Web 将“会话”与“单次工作流运行”分开：

- `conversation_id` 标识一个可持续追加的会话；
- 每一轮仍创建新的 `thread_id`，避免复用已终止的 LangGraph checkpoint；
- `parent_thread_id` 与 `turn_index` 将各轮串成有序链，界面按会话聚合左侧列表，中央时间线展示全部轮次；
- 新一轮只携带前文的“用户任务 + 最终结果”精简摘要，不重放工具输出、执行日志或完整主图状态；
- 上一轮必须进入 completed、terminated 或 failed 终态后才能继续。

Web 输入区可选绑定一个已存在的本机绝对目录。绑定后路径随会话继承且不得切换；`python_repl` 在进程锁保护下临时以该目录为当前目录，相对路径由此解析，Web 生成代码保存到 `<workspace>/.agentg/<thread_id>/`。未绑定时，Web 产物保存到项目内的 `workspace/<thread_id>/`。

> 这是工作目录和提示词层的范围约束，**不是操作系统级文件沙箱**。当前 `python_repl` 仍是进程内 `exec()`，理论上可使用绝对路径访问绑定目录外部。若要强保证“只在打开的文件夹内操作”，需要把工具迁移到只挂载该目录的受限子进程或容器。

### 持久化与恢复

`USE_REDIS=false` 时仍使用进程内 `MemorySaver`，适合本地临时运行。`USE_REDIS=true` 时，`RedisCheckpointSaver` 接管 LangGraph 原生检查点：

- 完整保存 checkpoint、metadata、父检查点链和并行分支的 pending writes；
- 同一 `thread_id` 可由新的 CLI/Web 进程读取并从最后一个可恢复节点继续；
- Web 的 pending、running、failed、completed、terminated 任务记录和历史列表同步写入 Redis；
- 每个可观察节点完成后增量保存消息、当前 Agent 和阶段性结构化结果，运行中任务不必等待终态即可查看；
- 删除 Web 任务时，同时删除该任务记录与对应的全部 LangGraph checkpoints；
- 恢复运行只刷新 `MAX_RUN_SECONDS` 的协作式墙钟窗口，已消耗的工具次数、动作预算和错误状态继续沿用。

该实现显式使用 RESP2，并且只调用 Redis 3.2 已支持的 String、Hash、Set 和 Sorted Set 命令，适配本项目现有的普通 Redis 3.2 环境。进程如果在某个节点执行期间退出，会从最近一次已经提交的检查点继续；尚未提交的当前节点可能重新执行，因此带外部副作用的工具后续仍需增加幂等键。

## 工具调用

| 工具 | 用途 |
|---|---|
| `web_search` | 使用 `ddgs` 搜索，失败时降级到 DuckDuckGo HTML/Lite |
| `current_weather` | 通过 Open-Meteo 获取城市实时天气，无需 API Key |
| `python_repl` | 在当前进程执行 Python；Web 模式下从会话绑定目录解析相对路径 |

工具由 LangChain `@tool` 注册。Worker 按“`AIMessage(tool_calls)` → 权限校验 → 工具执行 → `ToolMessage` → 模型”的循环运行；可用工具取角色工具箱与子任务白名单的交集。最终答案写入 `content`，原始观察和诊断分别写入 `tool_observations`、消息事件及 `error_logs`。

### 资源终止策略

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `MAX_TOOL_ROUNDS` | `4` | 单个 Worker 最多执行的工具观察轮次 |
| `MAX_TOOL_CALLS` | `12` | 整个工作流最多实际执行的工具调用数 |
| `MAX_CONSECUTIVE_ERRORS` | `2` | 单个 Worker 连续失败达到该值即终止 |
| `MAX_RUN_SECONDS` | `120` | 整个工作流的协作式墙钟截止时间 |
| `TOTAL_ACTION_BUDGET` | `40` | 整个工作流可用动作单位总数 |

一次模型调用或实际工具执行各消耗 1 个动作单位，Token 暂不计入。并行 Worker 使用互不重叠的预算切片；触发任一上限后记录 `termination.source/reason` 并进入 `blocked`。时间限制在调用边界检查，不能强制中断正在执行的进程内代码。

## 项目结构

```text
AgentG/
├── README.md
├── AGENTS.md
├── requirements.txt / pyproject.toml
├── Dockerfile / docker-compose.yml
├── assets/branding/                # 主产品 Logo 与两组共 12 套品牌探索资产
├── src/multi_agent_system/
│   ├── agents.py                 # Router、Orchestrator、Workers、Reviewer、工具
│   ├── graph.py                  # 批次调度、Send、Fan-in、定向返工
│   ├── state.py                  # 主状态、Worker 隔离状态、reducers
│   ├── main.py                   # CLI
│   ├── web_server.py             # FastAPI / SSE / Redis 或内存任务历史
│   ├── storage.py                # Redis LangGraph checkpointer 与任务仓库
│   ├── web_templates/index.html
│   └── workspace/                # 不可信运行产物
└── tests/                        # 单元、图、Web 和集成测试
```

## 启动方式

AgentG 支持本地 Python 与 Docker Compose 两种方式。命令均从项目根目录执行。

### 1. 配置

首次运行由用户本人从示例创建并填写配置，变量说明以 `.env.example` 为准：

```powershell
Copy-Item .env.example .env
```

`.env` 是用户专属保护文件：仅用户本人可读取或修改，不得提交到 Git；项目 Agent 与自动化测试不得访问它。

### 2. 本地运行

要求 Python 3.10+：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启用 Redis 持久化时，先在独立终端启动服务（默认端口 `6379`）：

```powershell
redis-server
# 或使用当前本机安装路径
& 'D:\MisItems\Redis-x64-3.2.100\redis-server.exe'
redis-cli -h 127.0.0.1 -p 6379 PING
```

选择启动入口：

```powershell
# CLI
.\.venv\Scripts\python.exe -m src.multi_agent_system.main

# Web：http://localhost:8000
.\.venv\Scripts\python.exe -m src.multi_agent_system.web_server

# Web 开发热加载
.\.venv\Scripts\python.exe -m src.multi_agent_system.web_server --reload
```

`--reload` 仅用于开发；它会重启进程，只有 Redis 模式可恢复运行中的任务。`/api/health` 返回 `redis_connected=true` 表示持久化连接正常。停止服务使用 `Ctrl+C`。

### 3. Docker Compose

要求 Docker Compose；宿主机访问 <http://localhost:8080>：

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f agentg
docker compose down
```

Redis 默认关闭；启用时使用容器地址 `redis://redis:6379/0`，长期保留数据需另配持久卷。工作目录必须先通过 `volumes` 挂载，并在界面填写容器内路径。

### 4. 验证

测试使用 mock，不需要真实 API Key 或在线服务：

```powershell
D:\anconda\envs\Docs2KG\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

## Web API

Web 提供任务 CRUD、SSE 执行流、会话管理和健康检查。任务详情包含增量消息、会话链、稳定状态字段 `outcome/is_active`，以及不暴露私有思维链的自然语言 `user_summary`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | Web 页面 |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/tasks` | 创建新会话或续接会话；JSON 支持 `task`、可选 `parent_thread_id` 和 `workspace_path` |
| `GET` | `/api/tasks` | 列出任务历史、`outcome` 与当前进程活跃状态；启用 Redis 时可跨服务重启读取 |
| `GET` | `/api/tasks/{id}` | 获取增量消息、当前 Agent、`user_summary` 用户总结及 `conversation_turns` 会话链 |
| `GET` | `/api/tasks/{id}/stream` | 启动新任务，或恢复 stale running/failed/interrupted 任务，并通过 SSE 推送事件 |
| `DELETE` | `/api/tasks/{id}` | 删除任务记录与对应 checkpoints；运行中任务拒绝删除 |
| `PATCH` | `/api/conversations/{id}` | 通过 `title` 重命名，或通过 `is_pinned` 置顶/取消置顶整个会话 |
| `DELETE` | `/api/conversations/{id}` | 删除整个会话的所有轮次与 Redis checkpoints；运行中拒绝 |

创建任务后需连接 `/stream` 才会执行。终态会话可追加新轮次；stale running/failed/interrupted 任务可使用原 `thread_id` 从 checkpoint 恢复。

## 品牌与 Logo

![AgentG Researcher Core](assets/branding/researcher-v2/researcher-v2-logo.svg)

主产品 Logo 为 [`Researcher Core`](assets/branding/researcher-v2/researcher-v2-logo.svg)，参考 DeskPet `01-researcher-v2`。SVG 使用透明背景、`viewBox="0 0 100 100"` 和 `currentColor`；Web 左上角与主页面共用同一 Symbol。其余探索方案、预览和适用场景见 [`assets/branding/README.md`](assets/branding/README.md)。

## 已知限制与风险

- `python_repl` 使用进程内 `exec()`，没有权限隔离或可抢占的硬超时；工作目录绑定也不是文件系统沙箱。墙钟策略只能在调用边界检查，不能中断死循环，不能直接面向不可信公网用户。
- 四类 Agent 共享同一个 `ChatOpenAI` 实例和模型配置，不是独立进程或服务。
- Redis 3.2 自定义 checkpointer 已覆盖当前同步图运行路径，尚未实现 LangGraph 异步 `aget_tuple/aput/alist` 接口；当前 CLI 与 Web 均使用同步 `stream`，不受影响。
- 已提交检查点能够恢复，但节点内部发生外部副作用后、检查点提交前进程退出时，该节点可能重跑；写操作型工具仍需幂等键或事务补偿。
- 会话上下文目前是最多 12 条的文本摘要链，尚未引入语义检索、自动压缩或长期记忆。
- 工作目录目前通过文本框录入服务端可见的绝对路径。标准浏览器目录选择器不会向页面暴露真实绝对路径，因此尚未提供类似桌面 IDE 的原生“打开文件夹”对话框。
- 开发热加载会重启 Web 进程；Redis 模式可依托 checkpoint 恢复 stale running 任务，但内存模式会丢失运行中任务记录。
- Web 缺少认证、限流、取消、输入长度限制和人工审批。

## 后续优化与可借鉴模式

以下内容按本轮要求留待后续：

1. 工具幂等与事务补偿：为可能产生外部副作用的工具增加 invocation ID、去重和补偿记录。
2. 硬超时与隔离：把 Python 工具迁移到可终止的受限子进程或容器。
3. Token 预算：待确认模型供应商的可靠 usage 元数据后，再纳入统一预算。
4. Human-in-the-loop：危险代码、文件写入、外部副作用和低置信度结果需审批。
5. 工作区硬隔离：将 Python 工具迁移到只挂载选定目录的受限子进程或容器，添加规范路径检查、符号链接越界拒绝、超时和写操作审批。
6. 工作目录录入体验：本地桌面版可接入原生文件夹选择器；纯 Web 版优先使用后端可见目录白名单、可选工作区列表和可读/可写状态提示，避免把浏览器文件上传误当成本地路径绑定。
7. 安全文件工具：为列举、读取、搜索、写入和补丁提供定向工具，默认屏蔽 `.env` 和工作区外路径，逐步减少对通用 `exec()` 的依赖。
8. 热加载下的任务连续性：开发服务重启前主动标记运行中任务为 interrupted，重启后自动重连 SSE 并从 Redis checkpoint 恢复。
9. 长会话 UI：为超长时间线增加虚拟渲染、导航过滤、全局展开/收起、返回顶部/跳到最新结果和滚动锚定。
10. 会话生命周期：增加取消当前轮、从任意历史轮次分支和孤立 checkpoint 定期回收。
11. 可观测性与评测：补充 Token、返工原因、产物来源和跨运行指标聚合。
12. 模型分层：Orchestrator/Reviewer 使用强模型，低风险 Worker 使用轻量模型。
13. 动态 Agent 注册：专业 Agent 数量明显增加后再引入能力发现和按需加载。
14. 多 Reviewer / Debate：只对安全、合规或高价值结果启用投票、仲裁。
15. A2A：Agent 拆成跨团队、跨框架的独立远程服务后再引入。
16. 工程治理：认证、限流、取消、Redis 数据卷和生产部署配置。

## 技术栈

- LangGraph / LangChain / langchain-openai
- Pydantic 结构化输出
- FastAPI / Uvicorn / Jinja2 / SSE
- Redis（可选 LangGraph checkpoints、pending writes 与 Web 历史）
- pandas / openpyxl / ddgs / BeautifulSoup
- pytest
