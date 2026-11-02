# AgentG — Orchestrator–Worker 多智能体系统

AgentG 是基于 **LangGraph** 的可控 Multi-Agent / Agentic Workflow。系统先判断任务应当“简单直通”还是“复杂组队”；复杂任务由 Orchestrator 拆成结构化子任务，再交给 ResearchAgent、CodeAgent、DataAgent 和 SynthesisAgent。无依赖子任务并行 Fan-out，有依赖任务按拓扑顺序串行，最后由结构化 Reviewer 验收并定向返工。

> 本文档兼作工作进度记录。最近更新与代码核对：**2026-10-16**（L1.6 管理、纠错、停用与逻辑删除已实现），最终全量离线 **612 项通过**。长期记忆默认关闭；L1.7 真实验收样例见 [MemoryAcceptance.md](MemoryAcceptance.md)，尚未执行的项目不记为通过。

## 当前进度

| 模块 | 状态 | 实现 |
|---|---|---|
| Task Router | ✅ | 类型化判断 `simple/team`；含多城市天气的并行任务确定性校正为 team，单城市实时天气限定 ResearchAgent |
| Orchestrator | ✅ | 输出含 Agent、依赖、工具、格式和验收标准的 `SubTaskSpec[]` |
| 四类 Worker | ✅ | Research、Code、Data、Synthesis，可继续扩展 |
| Fan-out / Fan-in | ✅ | Ready batch 内并行，汇聚后再调度下一层依赖 |
| 上下文隔离 | ✅ | Worker 只接收当前任务所需的最小上下文 |
| 短期记忆治理 | ✅ | 全局准入上限约覆盖 8～10 万模型 Token 的中英混合请求；保留近期问答窗口、历史抽取摘要、完整工具调用组裁剪和上游交付字段投影 |
| 长期记忆 | L1.1～L1.6 完成 | SQLite 受控读写、预算检索、最小 Worker 投影、恢复/逐调用重校验及管理 UI；逻辑删除保留版本，无向量检索，真实验收待完成 |
| 结构化 Reviewer | ✅ | 输出 `pass/revise/blocked`、类型化定向修订；`MAX_REVIEW_ROUNDS` 控制总审查轮数，启动回退 2；直接构造 `Settings` 默认 100 |
| 结构化决策轨迹 | ✅ | Worker 独立记录输入、工具选择、工具观察、修订与交付事件；引用证据，不生成或展示私有思维链 |
| 版本化产物与编辑式修复 | ✅ | 每次交付记录版本、基线、哈希和变更路径；支持受控 JSON/Text 补丁、完整替换和重新生成 |
| 定向返工 | ✅ | 只重跑目标任务及受影响的下游；返工 Worker 读取旧交付与绑定最新基线的修订契约 |
| 完整工具循环 | ✅ | `AIMessage(tool_calls)` → 双重权限校验 → 工具执行 → 匹配调用 ID 的 `ToolMessage` → 模型，支持多轮观察 |
| 类型化工具运行时 | ✅ | 通用 dispatcher 严格校验参数，统一结果/错误码；显式空白名单不授权，必需工具有成功证据检查 |
| Python 执行隔离 | ✅（非 OS 沙箱） | 每次调用独立子进程，硬超时、输出上限、最小环境及 Python 审计路径/网络策略 |
| 资源终止策略 | ✅ | 限制时间、工具轮次/次数、连续错误和动作总预算；暂不计算 Token |
| 实时天气工具 | ✅ | 当前天气任务自动使用 Open-Meteo 结构化接口，不再依赖搜索摘要 |
| 多来源网页搜索 | ✅（基础回退） | `web_search` 依次尝试 Bing RSS、百度 HTML、DDGS 与 DuckDuckGo HTML，统一返回带来源的结构化结果；尚未实现正文抓取与来源可信度评级 |
| CLI / Web / SSE | ✅ | Agent 工作台 UI、可滚动会话时间线、Codex 式底部输入区、运行进度恢复和统一状态 |
| UI 风格 | ✅ | 左下角可切换石墨黑、深海蓝和纸张白，选择保存在浏览器本地 |
| 任务过程呈现 | ✅ | 运行中展开技术过程；终态统一折叠“思考与执行过程”并直接显示用户结果总结，单步仍可展开 |
| 决策轨迹归属 | ✅ | `DecisionTrace` 按 `agent_type + task_id + attempt` 收容在对应 Worker 卡片内部，不再与 Agent 输出平铺 |
| 会话任务导航 | ✅ | DeepSeek 风格居中刻度轨道；右侧滚动条左边显示轮次导航，未到末尾时提供 Codex 式“跳至底部”按钮 |
| Markdown 结果渲染 | ✅ | 用户结果及 Agent 交付支持标题、列表、表格、代码、引用和安全链接；原始 HTML 被转义，技术错误仍按纯文本显示 |
| 品牌 Logo | ✅ | 已选用 `01-researcher-v2` 编号 01 `Researcher Core` 作为主产品 Logo，并通过共享的内联 `currentColor` SVG Symbol 接入 Web 左上角与主页面空状态；保留两组共 12 套探索资产供扩展场景使用 |
| 多轮会话 | ✅ | 完成后可在同一会话继续；每轮使用独立 `thread_id` 和 checkpoint，通过会话链关联 |
| 会话管理 | ✅ | 左侧长列表可独立滚动；右键可置顶、重命名或删除整会话；运行中禁止删除，标题版本保护避免执行进度覆盖重命名 |
| 本地工作目录 | ✅（审计约束） | Web 绑定并锁定已存在目录；Python 子进程使用独立 cwd，审计钩子限制路径；仍非 OS 级硬隔离 |
| Web 开发热加载 | ✅ | `--reload` 或 `WEB_RELOAD=true` 显式启用，仅监视源码/模板并排除运行产物 |
| 持久化恢复 | ✅ | 默认使用 MemorySaver；启用 Redis 后持久化 LangGraph checkpoint、pending writes 与 Web 历史，可按 `thread_id` 恢复 |
| Redis 3.2 兼容 | ✅ | 自定义 `BaseCheckpointSaver` 使用 RESP2 与 String/Hash/Set/ZSet，不依赖 RedisJSON 或 RediSearch |
| JSON Mode 兼容性 | ✅ | 显式声明 `json` 字段；Reviewer 可把与唯一目标严格一一对应的字符串修订要求转换为类型化契约，歧义输出进入 `review_output_invalid` 阻塞 |
| 自动化测试 | ✅ 离线回归 | 2026-10-16 L1.6 针对性 **360 项通过**，最终全量 **612 项通过**；真实服务和长期记忆全链路验收不能由 mock 测试代替 |

### L1.6：记忆管理与逻辑删除（2026-10-16）

新增 `memory_management.py` / `memory_api.py`，通过左下角“记忆”或标题栏“记忆管理”进入。支持按用户/项目/当前会话/授权产物范围查看、状态筛选、列表/版本翻页、来源和修改原因，以及候选确认、纠错、停用、重新启用、逻辑删除。正文和来源使用纯文本展示；修改须确认原正文、前驱版本和 SHA-256，旧页面更新返回 409，不猜测合并。

- 纠错/生命周期操作追加版本，旧记录不覆盖。偏好/规则明确纠错后可生效；事实纠错仍为候选、不刷新原时效。管理 UI 不接受客户端 `verified`，不能把点击确认当独立事实证据。
- 删除会话默认只清理本会话/产物记忆，保留用户/项目知识；用户可明确选择同时逻辑删除历史来源关联到该会话的跨作用域条目。清理在单个 SQLite 批事务中校验全部前驱，之后才清除 Redis 历史/checkpoints；SQLite 不可用则保留会话并返回 503。两库仍无分布式事务；中途失败需重试，已写 tombstone 不复活。
- 每次 Worker 模型调用前由宿主重新校验原快照。工具循环中已使用的记忆被撤销时停止 Worker，避免再次发送含旧数据的调用历史；已经发出的模型调用或工具动作不能撤回。该回调只存于进程上下文，不把仓库/函数序列化到 Worker 状态。
- **删除是逻辑删除，不是全副本擦除**。旧版本、旧会话、Redis 与备份可含原文；没有自动物理清理、删除清单导入器或 OS ACL 配置。删除后备份保留 tombstone；删除前备份恢复至新路径后，须人工对照现库删除记录并重新执行删除，核查完毕再启用。保留期/访问权限由用户管理，详见 L1.6。

L1.7 样例已单独编写至 [MemoryAcceptance.md](MemoryAcceptance.md)，覆盖最小读写链路、纠错冲突、隔离/预算、真实恢复/撤销和离线故障注入；产物记忆无 UI 新建入口，其真实写入验收有明确前置条件。

本轮针对性 **`360 passed in 20.78s`**，全量 **`612 passed in 41.02s`**（Docs2KG / Python 3.11.9），含新增 31 项管理/删除/撤销回归。模型/Redis mock，外部 HTTP 默认拒绝，SQLite 全用临时库；本地浏览器检查三主题、候选确认 v2、纠错及旧版保留、HTML 文本、关联删除预览、760px 单列与焦点循环，控制台无警告/错误。临时 UI 服务与标签页已关闭，未读取 `.env`、启动 Redis、调用真实模型/工具服务、执行用户产物或访问实际记忆库。仓库已有 `AgentLearn/AgentLearning.md` 两处行尾空格属于本轮之前的改动，未擅自清理；本轮相关文件无新增 diff 空白错误。

### L1.3～L1.5：受控记忆读写与图接入（2026-10-16，前次记录）

新增 [`memory_service.py`](src/multi_agent_system/memory_service.py)、[`memory_runtime.py`](src/multi_agent_system/memory_runtime.py)、[`memory_paths.py`](src/multi_agent_system/memory_paths.py)。SQLite 继续管理版本，Redis 继续管理 checkpoint，没有更换 Redis key schema 或增加外部依赖。

- **写入**：显式命令 → 宿主来源目录核对 → 敏感/时效检查 → 同 namespace/kind/key 的稳定记录身份 → 固化版本写入计划 → SQLite 幂等提交。普通聊天不自动授权；可选确定性提取只把最终公开交付保存为未验证候选，不增加模型调用，不存工具日志或私有思维链。
- **读取**：先限制宿主授权 namespace、active/验证/时效/当前版本，再做有界关键词排序和去重。默认 Top-K=5、独立 UTF-8 字节预算 4000、每 namespace 扫描上限 200；候选不检索，规则/显式必要条目放不下则 blocked，不截掉关键约束。
- **图接入**：启用后增加 `prepare_memory → Router/Orchestrator → 按 Worker 目标取用 → Reviewer → stage_memory_writes → commit_memory_writes`；simple/team 都覆盖。Worker 只获得自己需要的快照及宿主验证绑定，不获得数据库连接、整库或无关分支。实际 Worker 输入另记入 `worker_results[].memory_snapshot`。
- **恢复**：重查来源坐标、当前版本、删除/停用及过期状态；可选失效项剔除，必要项结构化阻塞，不静默取新版替换旧快照。固化计划在提交前进入 checkpoint，已提交 SQLite 但 checkpoint 未记回执时，重放同一计划不增加版本；不清零原资源计数，也不承诺外部工具 exactly-once。
- **保护与可观测性**：Python 普通文件 IO、目录/祖先移动和 `sqlite3.connect` 拒绝默认/自定义记忆数据目录；关闭功能、恢复旧 payload 或存储不可用时仍保留此保护，WAL/SHM 一并保护并排除热加载。Web 拒绝客户端伪造身份/投影，展示保存/候选/未保存状态；初始化配置异常会结束 SSE 而不一直停在初始阶段。仍非 OS 强沙箱，Web 仍只适合可信本地使用。

启用方式、准确命令语法、事实确认边界与配置见后文 L1.3～L1.5。L1.6 管理/逻辑删除现已实现；L1.7 真实模型/Redis 验收、物理清理、认证/注册、向量检索、通用语义冲突检测与完整 DLP 尚待完成。

本轮针对性离线回归 **`230 passed in 18.92s`**，最终全量 **`581 passed in 29.47s`**（Docs2KG / Python 3.11.9）。Windows 沙箱会卡住 ASGI TestClient 的本地 `socketpair`，因此获授权后在沙箱外运行 Web/全量回归；外部 HTTP 默认拒绝。首轮全量暴露旧搜索集成测试仅 mock DDGS、Bing 回退意外发生真实查询，已补齐 provider mock，并在测试中默认拒绝 urllib/requests/httpx/DDGS 的未 mock 外部入口；修复后回归不依赖网络。没有读取 `.env`、启动 Redis、调用真实模型或执行用户产物；没有访问项目实际记忆库。

### L1.2：SQLite 仓库已完成（2026-10-07，前次记录）

新增独立 [`memory_store.py`](src/multi_agent_system/memory_store.py)：`MemoryRepository` 接口与 `SQLiteMemoryRepository` 实现，使用 Python 标准库 `sqlite3`，不需要安装数据库服务器或额外 pip 包。本机 Docs2KG / Python 3.11.9 已确认内置 SQLite **3.45.3**。SQLite 与 MySQL/SQL Server 一样使用表、索引和事务，但以本地文件承载数据库，由应用进程直接访问，不需要监听端口或启动服务。

仓库提供不可变版本、当前版本指针、来源关联、namespace/事件键唯一索引；通过 `BEGIN IMMEDIATE`、真实前驱校验和版本比较避免并发覆盖。同一事件完整载荷相同才复用，冲突明确拒绝。有效查询只返回当前 active、已验证且未过期的条目，管理/历史接口可查看候选和旧版本。初始化支持空库 → schema v1，拒绝未知版本、异库或不完整结构；SQLite 在线备份包含已提交 WAL 数据，恢复只写入新路径，不覆盖已有数据库。

默认路径固定为项目应用根目录下 `.agentg/memory/long_term.sqlite3`，本机即 `D:\A_Mycodes\Agent_code\AgentG\.agentg\memory\long_term.sqlite3`，不随终端 cwd 或任务工作目录改变。`.agentg/` 已加入 Git 忽略，现有热加载也排除此目录。新增 `LONG_TERM_MEMORY_ENABLED=false`、`MEMORY_DB_PATH`、`MEMORY_DB_BUSY_TIMEOUT_MS=5000`；工厂在关闭时不做数据库 IO，开启后仍须宿主显式调用。本轮没有在项目目录创建实际记忆库，也没有接入 CLI/Web 自动记忆流程、主图状态、关键词/向量检索或写入确认。Redis 职责和 key schema 不变。

已确认用户把 `test_settings_default_values` 的审查默认值断言改为 **100**，与直接 `Settings` 构造一致；新增环境值 100 的测试，保留 1/2/3 轮边界。`get_settings()` 无环境设置/无效值仍回退 **2**，旧 checkpoint 缺字段也回退 2；需要运行 100 轮时，由用户本人设置 `MAX_REVIEW_ROUNDS=100`，其他资源上限不会随之增加。

仓库/契约/配置针对性回归 **`216 passed in 5.00s`**；最终全量 **`497 passed in 30.18s`**。测试使用临时 SQLite、mock 外部服务和受控子进程，覆盖跨进程重开、并发竞争、幂等提交、事务/迁移失败回滚、时效与归属隔离、损坏诊断、WAL 备份和恢复。未访问 `.env`、启动 Redis、调用真实模型或执行用户产物。下一步为 **L1.3 受控写入**：来源所有权、敏感过滤、时效政策、冲突与用户确认；不能仅因为结构合法就允许自动记忆。

### L1.1：长期记忆契约已完成（2026-10-07，前次记录）

新增 [`memory_records.py`](src/multi_agent_system/memory_records.py) 与独立离线测试。`MemoryRecord` 及嵌套契约禁止额外字段和隐式数值/字符串转换，采用冻结模型、不可变来源元组和规范枚举；校验 namespace、来源种类、UTC 时间、版本前驱、精确 UTF-8 内容 SHA-256 以及产物组合身份。`MemoryIdentity` 提供显式本地用户身份与宿主 namespace 构造/核对，稳定 key 不依赖会话标题或工作目录。

L1.1 只建立契约，不自动验证来源真实性或账户权限。候选默认未验证；有效偏好/规则要求用户确认，历史失效记录仍可保存但不能通过本地可用性判断。`validate_memory_successor()` 校验真实前驱的身份、版本、哈希与时间；幂等键生成器只提供事件身份，数据库去重和并发控制由现已完成的 L1.2 承担。

该阶段未接入 SQLite、主图状态、CLI/Web 账户/项目注册、自动记忆提取或新配置，原有运行流程未改；随后完成的 L1.2 见上节。

当时 Docs2KG / Python 3.11.9 针对性回归为 **`116 passed in 0.47s`**；全量为 **`1 failed, 409 passed in 26.38s`**，唯一失败为 `Settings.max_review_rounds=100` 与测试期望 2 不一致。随后用户将断言改为 100，本次全量已恢复通过；启动回退仍为 2，不把两种构造路径混为一谈。测试禁用 dotenv、mock 外部服务，未读取 `.env`、启动 Redis 或执行用户产物。

### 长期记忆方案确认（2026-10-07，前次仅文档）

已确定采用 **SQLite 长期记忆 + Redis checkpoint** 的双存储方案，新增独立 Pydantic `MemoryRecord` 契约；以用户级、项目级、会话级和产物级区分作用域，以偏好、事实、经验和规则区分内容类型。读取采用权限过滤、按需检索、预算投影与快照；写入采用显式记忆请求或任务后候选提取，经校验、去重、冲突处理及必要确认后提交版本。完整步骤与验收标准见“后续路线图 → L1”。

方案确认阶段只更新文档，当时未创建数据库、契约或新配置，也未运行测试；随后完成的 L1.1 与本轮验证见上节。`294 passed in 17.82s` 是 2026-10-03 的历史回归结果，不是当前测试结论。

### 最近代码复核（2026-10-03）

本轮修复左侧历史会话列表的高度溢出：侧栏和 Grid 行显式允许收缩，历史列表占据剩余高度并独立滚动，品牌、新建任务和底部设置保持固定。桌面支持滚轮、滚动条拖动和聚焦列表后的键盘滚动；窄屏保留横向会话列表。浏览器以 60 条临时会话复现并验证，不使用真实 Redis 或用户历史数据。

Reviewer 总轮数由原来的源码常量升级为 `MAX_REVIEW_ROUNDS` 环境配置，默认仍为 `2`，即首次审查后最多一次定向返工。上限在初始化时写入 `resource_policy.max_review_rounds`，图调度与 Web 返工状态共用路由判断，不再分别写死 `2`；恢复已有 checkpoint 保留该轮原上限和已用次数，旧 checkpoint 没有该字段时兼容上限 `2`。此项不会自动扩大时间、工具或动作额度。

2026-10-03 最终全量离线回归为 **`294 passed in 17.82s`**（Docs2KG / Python 3.11.9）。本地浏览器确认 60 条会话可滚动并打开最早会话、三主题和右键菜单正常、760 px 窄屏横向列表与底部输入区可见、控制台无警告/错误。此次验收使用临时内存服务；未操作保护文件 `.env`、未启动 Redis，真实模型与 Redis 恢复验收仍由用户进行。

当前工作树已经具备 Orchestrator–Worker 主流程、依赖感知并行、四类专业 Worker、完整工具循环、短期记忆治理、Redis checkpoint、结构化 Reviewer、版本化产物、定向返工和 Web 工作台。`MultiAgentState` 继续作为可持久化的全局结构化工作记忆，`WorkerInputState` 则是由调度器按子任务派生的最小上下文视图；二者共同实现全局协作、Worker 隔离和恢复。

以下为历史修复记录，其中“待复测”是当时状态；当前用户验收结论见本节末尾。

2026-09-22 将全局模型输入准入默认值从 `16000` 提高到 `320000` 个 UTF-8 字节估算单位，并把输出预留从 `2000` 提高到 `16000`。按中英文、JSON 与工具协议混合负载粗略换算，单次请求可容纳约 **8～10 万真实模型 Token**；这仍不是精确 tokenizer 计数，也不会自动探测服务商窗口。会话历史 `4000`、旧摘要 `800`、直接上游 `3000` 等最小上下文子预算保持不变，避免因为扩大总窗口而默认灌入无关历史。配置、记忆与 Agent 针对性测试为 **`104 passed in 4.94s`**，全量离线回归为 **`271 passed in 18.44s`**。

R0 回归修复已于 2026-09-18 完成。Reviewer 不再把所有工具观察统一压成 240 个估算单位，而是优先投影持久化观察中的结构化 `ToolExecutionResult.data`：小型结果保持原始 JSON 类型和完整字段；大结果只在超过 Reviewer 单观察预算时转换为带明确标记的字符串预览。`source_truncated` 表示工具运行时在存储前已经截断，`projection_truncated` 表示完整存储结果仅在 Reviewer 输入视图中被缩短，二者可以独立取值，避免把审查层裁剪误判为工具执行失败。

2026-09-18 的真实用例进一步暴露了两个边界。第一，多工具观察下旧的大结果信封携带过多非必要字段，连单条最小预算都可能放不下；现在大结果信封只保留工具名、调用 ID、成功/错误状态、错误码、证据预览以及源端/投影截断标志。第二，将单条预算增大到 `1600/480/8000` 会让诊断、交付、Schema 和固定提示词各自合规但总和超过上下文；当前已恢复验证过的 `1200/320/4800`，并由统一准入函数对整份 Reviewer 请求逐级缩放，直到实际满足当前 `memory_policy`。

Reviewer 现在先去除等价重复观察，保留全部不同的成功证据和每种工具最新失败，再按实际剩余容量缩短子任务描述、交付正文、证据预览、计划和错误摘要；持久化原始结果不变。若 `execution_result` 与最新 Synthesis 产物在文本或规范化 JSON 上相同，提示词只发送 Synthesis 内容，并用 `deduplicated_with_task_id` 建立引用。2026-09-18 的针对性 Reviewer 测试为 **`14 passed in 2.57s`**，当日全量回归为 **`260 passed in 16.58s`**。

2026-09-21 的 V0 L0-02 两次真实验收依次发现：部分 OpenAI-compatible 模型会在 Reviewer 的 `status="pass"` 结果中省略 `feedback`，或把本应为空数组的 `revision_instructions` 输出成空对象 `{}`，导致 Pydantic 在进入图路由前解析失败。当前只在明确的 `pass` 场景执行无损规范化：缺失 `feedback` 时补入稳定通过说明，`failed_criteria`、`revision_targets` 或 `revision_instructions` 的精确空对象转换为 `[]`。非空对象、`revise/blocked` 的错误集合类型、缺失原因及显式空白反馈仍会被拒绝。用户复测后，**L0-02 已通过真实环境验收**；**L1-01 单城市天气也已通过**。

2026-09-22 的 L2-03、L2-04 与 L3-02 真实测试共同暴露了新的 Reviewer 边界偏差：模型正确返回 `status="revise"` 和目标 ID，却把 `revision_instructions` 输出成与目标一一对应的字符串数组。当前 Reviewer Prompt 已加入完整对象字段与合法 JSON 示例；Pydantic 前置边界只在目标非空、无重复、指令全为非空字符串且数量完全一致时，按位置转换为正式 `RevisionInstruction`。数量不一致、重复目标、空白或混合类型不作猜测，解析失败会保留既有 Worker 产物并返回 `status="blocked"`、`failed_criteria=["review_output_invalid"]`，不再冒泡为 Web 普通执行异常。Agent 回归为 **`83 passed in 4.09s`**，全量离线测试为 **`279 passed in 19.64s`**；三个真实用例仍待用户复测。

2026-09-23 针对 L2-04 长时间停留在“初始化资源策略”的现象完成可观测性与超时修复：实际阻塞点可能是后续模型 HTTP 请求，而旧 UI 只会在节点完成后更新持久化阶段。现在每个完成节点都会预写下一可见阶段，运行条使用动态高亮并显示实时用时；单次模型请求新增 `MODEL_REQUEST_TIMEOUT_SECONDS` 硬超时，且实际值不超过 `MAX_RUN_SECONDS`。运行中的已完成 Agent 卡片自动折叠，左下角可在 Ctrl/Alt/Shift + Enter 三种执行热键间切换并保存为本地界面偏好。会话重命名新增版本戳与进程内写锁，执行线程持有的旧记录不能覆盖新标题。针对性测试 **`87 passed in 8.21s`**，最终全量离线测试 **`283 passed in 20.21s`**；本地浏览器确认热键切换、提示同步及控制台无错误，L2-04 真实模型/天气链路仍需复测。

L1-01 同时暴露了结果区域直接显示 Markdown 源码的问题。Web 现已加入不依赖构建工具的安全 Markdown 子集渲染，支持标题、强调、列表、引用、代码块、表格和 `http/https/mailto` 链接；输入先转义，原始 HTML/脚本不会执行，错误诊断继续使用纯文本。禁用 dotenv、内存存储的本地浏览器验收确认表格正常显示、脚本文本被转义且控制台无警告或错误。

L2-03 首次真实测试并非执行了五次失败请求：模型一次提出五个搜索调用，前两个实际执行后均因原 DDGS/DuckDuckGo 端点不可用而失败；连续错误阈值随即终止，剩余三个只生成 `max_consecutive_errors_exceeded` 观察以维持 `AIMessage`/`ToolMessage` 配对。`web_search` 现按 Bing RSS、百度 HTML、DDGS、DuckDuckGo HTML 顺序执行有界来源回退，首个有效来源即返回统一的标题、URL、摘要和 provider 元数据。五城市五次成功观察的 Worker 回归已补齐；L2-03 仍需用户在真实网络环境复测，不能提前记为通过。

L2-03 第二次真实测试已完成搜索、排序和汇总，但由 Reviewer 的 `context_budget_exceeded` 终止。根因不是长期记忆缺失，而是宽 DAG 中子任务描述按“每字段、每任务”设置固定下限，八个子任务即使进入最低缩放档仍有不可压缩开销；同时每个只有一条观察的任务重复携带无信息增益的选择统计。当前子任务投影改为“验收标准优先”的单一 `review_requirements`，并对整组子任务实施总预算；精确的“一条选中、一条总计”观察不再重复发送统计信封。最紧张档位只把已经超长的工具数据转换为带 `projection_truncated=true` 的状态投影，小型结构化结果在容量允许时仍完整保留，原始 checkpoint 和 Worker 交付均不修改。新增八阶段、七条工具证据、`12000` 容量的回归后，全量离线测试为 **`271 passed in 16.22s`**。L2-03 仍待用户再次真实复测。

当前阶段结论：R0 已完成，V0 真实环境验收仍在进行。按用户最近反馈，L0～L2 中除 L2-04 外的用例已通过；L2-04 的运行阶段与超时修复后仍需真实复测，L3-02 尚未收到新的通过反馈。约 10 万 Token 请求、Redis 跨进程恢复和 Docker 验收也未确认。自动化回归和临时内存服务的浏览器检查不替代真实环境验收；不读取 `.env`，不擅自启动 Redis，不执行用户生成产物。

此前建议按 **V0 → M1 → T1/T2 → L1** 推进；用户现已推进到 L1.6。下一步为 L1.7 真实验收。真实模型/Redis 验收、tokenizer、关键事实保留、长结果引用和工具恢复仍为独立待办，不因接入长期记忆而自动完成。扩大权限或面向不可信用户部署前，必须完成 S1 OS/容器级安全隔离。

### 前次开发记录（2026-09-15）

Reviewer 修订指令的 JSON Mode 边界偏差已经修复：模型输出 `target_id/action` 时会在 Pydantic 正式校验前严格规范化为 `task_id/mode`，规范化后只保存正式字段；别名与正式字段冲突时继续拒绝。前次文档曾提前把“小型结构化 `ToolExecutionResult.data` 优先投影”记录为已完成；2026-09-17 发现并修复该实现缺口后，当前代码与文档现已重新一致。

此前新增的公开结构化决策轨迹和版本化编辑式返工保持不变。轨迹来自程序能够验证的任务输入、工具请求、工具观察、修订与交付事件，不要求模型输出私有思维链。返工可读取旧交付；Reviewer 通过 `RevisionInstruction` 选择 `patch/replace/regenerate`，宿主在应用补丁前验证最新版本、SHA-256 内容哈希、允许路径和 JSON 保留路径。

| 核对项 | 实现与测试证据 | 结论 |
|---|---|---|
| 记忆管理 | `memory.py` 已接入图初始化、Router / Orchestrator / Worker / Reviewer 与 Web 续接；`test_memory.py`、`test_hardening.py` 覆盖容量、摘要、投影和消息配对 | 已完成基础治理；尚非精确 tokenizer 或长期语义记忆 |
| 工具调用 | `tool_runtime.py` 已接入 Worker；`test_tool_runtime.py`、`test_hardening.py` 覆盖严格参数、宿主参数保护、类型化结果及必需工具证据 | 已完成本轮协议与权限加固 |
| 决策轨迹 | Worker 返回独立 `decision_traces`；Web 增量持久化并通过 SSE/历史详情显示输入、工具选择、观察、修订和交付 | 已完成后端与无构建前端接入；未在本轮进行真实浏览器视觉验收 |
| 编辑式返工 | `artifact_runtime.py` 提供 `ArtifactRecord`、`RevisionInstruction`、`ArtifactPatch` 和受控应用器；图调度为返工 Worker 注入旧交付及绑定基线 | 支持 JSON Pointer 与唯一文本片段替换；无精确路径时继续使用完整替换/重新生成 |
| Python 执行 | `python_runtime.py` / `python_runner.py`；`test_python_runtime.py` 覆盖独立变量/cwd、并发、环境、超时回收、输出上限与普通 IO 策略 | 已完成子进程治理；未实现恶意代码强隔离 |
| 持久化与多轮会话 | `test_storage.py`、`test_web.py` 覆盖精简记忆续接及类型化观察的 mock Redis checkpoint 恢复 | 已完成离线验证；节点内工具副作用尚无幂等日志 |
| 工作流回归 | `test_graph.py`、`test_integration.py` 覆盖依赖调度、Fan-in、定向返工、v1→v2 精确文本修订及三城市天气离线工具闭环 | 已完成离线验证；不等于线上模型与天气服务测试 |

2026-09-15 的历史基线为 Docs2KG / **Python 3.11.9** 全量 **`255 passed in 18.28s`**；2026-09-17 修复与 UI 修改后的基线为 257 项；2026-09-18 先完成多观察最小预算回归 258 项，再完成 Reviewer 整包动态预算和证据筛选，当日结果为 **`260 passed in 16.58s`**；2026-09-21 文档与代码一致性复核为 **`260 passed in 19.95s`**，Reviewer `pass` 边界修复后为 267 项，多来源搜索与 Markdown 渲染修复后为 270 项，宽 DAG Reviewer 聚合投影修复后为 **`271 passed in 16.22s`**；2026-09-22 提高全局上下文准入后为 271 项，字符串修订契约与结构化阻塞修复后为 **`279 passed in 19.64s`**；2026-09-23 会话标题并发保护、运行态 UI、热键和模型硬超时修复后为 **`283 passed in 20.21s`**。测试均以 mock 隔离外部模型、HTTP、天气与 Redis；Python 测试仅运行自编的受控样例。

### 记忆与工具加固（2026-09-11）

- 新增 `memory.py`：预算内保留最近问答与旧轮次抽取摘要；上游结果仅投影交付字段；Worker 模型输入按完整工具调用组裁剪，保留任务约束和调用 ID。
- 新增 `tool_runtime.py`：统一 Pydantic 参数校验和 `ToolExecutionResult`；不再用错误文本判断状态，不再把空白名单回退成全部角色工具。
- Router / SubTask 新增 `required_tools`，缺少必需成功观察时不能直接通过 Reviewer，改为定向返工。实时天气沿用专用工具规则。
- `python_repl` 迁移为一次一子进程：独立变量、cwd 与输出捕获，超时终止；默认不继承密钥环境，Python 审计层拒绝 `.env`、工作区外写入、网络与派生进程。
- 新配置都有默认值，仅更新 `.env.example`；无需修改已有 `.env` 即可启用。未启动本机 Redis，未执行真实模型/天气请求；测试使用离线模拟服务及自编的受控 Python 样例。
- 仍需后续完成 OS/容器级隔离、调用幂等日志、风险审批和长期语义记忆；这次加固不等于生产安全沙箱。
- 验证：`237 passed in 16.50s`，包括三城市天气的离线完整流程、长会话续接、类型化观察的 Redis 检查点恢复，以及 Python 超时回收和并发隔离。真实服务端到端测试仍需用户重启后进行。

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
allowed_tools: list[str]
required_tools: list[str]
```

- `simple` 创建一个 `simple-1` 子任务，绕过 Orchestrator，直接进入所选 Worker。
- `team` 由 Orchestrator 生成子任务 DAG。

三个 `json_mode` 节点都会在提示词中声明准确的 JSON 字段。Task Router 还会兼容部分模型返回的 `{"type":"team"}`：解析前将合法的 `type` 别名规范化为 `route`，缺失的 `reason` 使用可识别的默认说明。Reviewer 对明确的 `status="pass"` 允许省略 `feedback`，并把三个列表字段的精确空对象 `{}` 规范化为 `[]`；`revise/blocked` 仍必须提供反馈并遵守列表类型。其他非法路由值、非空对象、空白反馈或错误类型仍会触发校验错误。

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
| `required_tools` | 必须留下成功观察的工具，是 `allowed_tools` 的子集 |
| `output_format` | 期望输出格式 |
| `acceptance_criteria` | 可验证的验收标准 |

Orchestrator 会清理重复 ID、无效依赖和越权工具。若检测到循环依赖，计划会按声明顺序降级为串行。复杂计划末尾会确保存在最终 SynthesisAgent。

为兼容部分 OpenAI-compatible 模型的 JSON Mode 输出偏差，`dependencies`、`allowed_tools` 和 `acceptance_criteria` 在解析时允许单个字符串，并立即规范化为单元素列表；`output_format` 则允许模型返回单元素字符串数组，并立即解包为正式的字符串类型。多元素或非字符串 `output_format` 仍会校验失败，不会把含糊数据送入调度器。提示词同时明确前三个字段必须始终输出 JSON 数组，而 `output_format` 必须始终输出 JSON 字符串。

### 专业 Worker 与工具权限

| Worker | 职责 | 角色工具箱（非自动授权） |
|---|---|---|
| ResearchAgent | 搜索、实时天气、来源整理和事实核验 | `web_search`、`current_weather` |
| CodeAgent | Python 设计、执行和技术实现 | `python_repl` |
| DataAgent | 数据读取、清洗、统计和表格处理 | `python_repl` |
| SynthesisAgent | 整合必要上游结果，形成最终答案 | 无 |

运行时只绑定角色工具箱与 `allowed_tools` 的交集；缺失或显式空数组都表示不授权，simple 路径也由 Router 声明最小工具集合。模型发出的未授权调用会被拒绝。当前/实时天气是明确的业务例外：规范化为 ResearchAgent 的 `current_weather`，并同时列为必需工具。`required_tools` 必须是字符串数组，不做含糊类型转换。

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
revision_instructions: list[RevisionInstruction]
feedback: str
confidence: 0.0 .. 1.0
```

路由直接使用类型化 `status`，不再解析 `REWORK` 文本。`revision_targets` 会过滤为真实任务 ID；每个目标可带一条 `RevisionInstruction`，选择 `patch`、`replace` 或 `regenerate`，并声明允许修改的 JSON Pointer、必须保留的 JSON 路径、修改要求和证据要求。返工集合仍等于目标任务及其下游依赖闭包，因此无关分支不会重跑。

对于 OpenAI-compatible JSON Mode 的窄范围偏差，只有 `revision_targets` 与非空字符串型 `revision_instructions` 数量一致、目标无重复且可按位置唯一对应时，才在 Pydantic 正式校验前转换为根路径 `replace` 契约；转换后状态中不保留字符串。其他畸形输出不会触发猜测、整流程异常或定向返工，而是形成 `review_output_invalid` 的结构化 blocked 结论，并保留 Worker 交付供排查。

Worker 首次成功交付时建立 `ArtifactRecord` v1，记录内容类型、SHA-256、版本和产物 ID。返工时调度器只注入该 Worker 的旧交付与绑定最新版本/哈希的修订指令：JSON 产物支持 `add/replace/remove` 的受限路径操作；文本产物只支持 `/text` 上恰好匹配一次的 `replace_text`。候选补丁若基线过期、越过允许路径、修改保留字段或没有实际变化，会被宿主拒绝，旧产物保持不变。无法精确定位差异时使用完整 `replace`；缺少工具证据或旧产物不可用时使用 `regenerate`。

进入模型审查前，先确定性检查每个子任务的 `required_tools` 是否有成功观察；缺失则直接定向返工，不额外调用 Reviewer 模型。这是“执行证据存在性”检查，不代表来源质量或答案正确性已经得到保证。

**审查次数参数：`MAX_REVIEW_ROUNDS`**，默认 `2`，最小 `1`。它限制每轮任务进入 Reviewer 的总次数（包括首次审查和无需模型调用的证据门禁），不是 Worker 工具循环次数，也不是保证会执行的次数：`pass/blocked` 会立即结束；只有 `revise`、有效目标和剩余轮数才返工。设为 `1` 不返工，`2` 最多返工一次，`3` 最多返工两次。到达上限仍为 `revise` 时正常结束且审查未通过，不自动变为成功。

请用户自行在 `.env` 添加或修改 `MAX_REVIEW_ROUNDS=3`，然后重启 Web/CLI，再创建新一轮任务；`.env.example` 只提供模板，不直接生效。配置入口是 `src/multi_agent_system/config.py` 的 `Settings.max_review_rounds` / `get_settings()`；调度入口是 `graph.py` 的 `route_after_review()`。已有 checkpoint 恢复不重置审查计数或上限。增加返工机会仍受时间、工具、连续错误、动作和上下文容量限制。

### 共享状态与上下文隔离

`MultiAgentState` 是主图共享黑板，保存路由、计划、子任务、批次、版本化 Worker 结果、审查决定、错误、代码和消息。

`WorkerInputState` 是记忆与上下文隔离边界。Worker 只接收：

1. 自己的任务目标；
2. 直接依赖的最新上游交付结果（不含原始观察、日志和用量）；
3. 允许使用及必须提供成功证据的工具；
4. 输出格式；
5. 验收标准；
6. 定向返工反馈、类型化修订指令、自己的上一版可交付产物和当前执行序号；
7. 绑定工作目录、上下文策略，以及当前 Worker 的截止时间、工具额度和动作预算切片。

Worker 不接收完整用户对话、完整主图状态或无关分支结果。

### 多轮会话与工作目录

Web 将“会话”与“单次工作流运行”分开：

- `conversation_id` 标识一个可持续追加的会话；
- 每一轮仍创建新的 `thread_id`，避免复用已终止的 LangGraph checkpoint；
- `parent_thread_id` 与 `turn_index` 将各轮串成有序链，界面按会话聚合左侧列表，中央时间线展示全部轮次；
- 新一轮只携带预算内的“用户任务 + 最终结果”窗口与历史抽取摘要，不重放工具输出、异常原文或完整主图状态；
- 上一轮必须进入 completed、terminated 或 failed 终态后才能继续。

Web 输入区可选绑定一个已存在的本机绝对目录。绑定后路径随会话继承且不得切换；`python_repl` 的独立子进程以该目录为 cwd，不再修改服务进程的 cwd、stdout 或共享变量。Web 生成代码保存到 `<workspace>/.agentg/<thread_id>/`；未绑定时，Python 使用启动目录，Web 产物保存到项目内的 `workspace/<thread_id>/`。

> 当前增加了 Python 审计层路径约束、`.env` 拒绝和子进程硬超时，但**不是操作系统级文件沙箱**。解释器库路径仍可读取以支持 pandas 等依赖，Python 内省或原生扩展可能绕过审计；这些措施主要防范误操作，不能可靠隔离恶意代码。要强保证“只能访问选中目录”，还需 OS 权限/受限容器、网络隔离与 CPU/内存配额；Web 仍仅适合可信本地使用。

### 持久化与恢复

`USE_REDIS=false` 时仍使用进程内 `MemorySaver`，适合本地临时运行。`USE_REDIS=true` 时，`RedisCheckpointSaver` 接管 LangGraph 原生检查点：

- 完整保存 checkpoint、metadata、父检查点链和并行分支的 pending writes；
- 同一 `thread_id` 可由新的 CLI/Web 进程读取并从最后一个可恢复节点继续；
- Web 的 pending、running、failed、completed、terminated 任务记录和历史列表同步写入 Redis；
- 每个可观察节点完成后增量保存消息、当前 Agent 和阶段性结构化结果，运行中任务不必等待终态即可查看；
- 删除 Web 任务时，同时删除该任务记录与对应的全部 LangGraph checkpoints；
- 恢复运行只刷新 `MAX_RUN_SECONDS` 的协作式墙钟窗口，已消耗的工具次数、动作预算和错误状态继续沿用。

该实现显式使用 RESP2，并且只调用 Redis 3.2 已支持的 String、Hash、Set 和 Sorted Set 命令，适配本项目现有的普通 Redis 3.2 环境。进程如果在某个节点执行期间退出，会从最近一次已经提交的检查点继续；尚未提交的当前节点可能重新执行，因此带外部副作用的工具后续仍需增加幂等键。

## AgentG 的三个核心能力

下面从 Agent 系统最关键的三个方向分析当前实现。这里的“自我推理”指系统中可观察、可约束的任务判断、规划、执行反馈与审查流程，不代表也不展示模型私有思维链。

### 一、记忆系统

AgentG 当前实现“任务工作状态 + 会话短期记忆 + checkpoint 持久化 + 可选 SQLite 长期记忆”。长期记忆采用结构化筛选和确定性关键词检索，不是向量/语义检索；偏好需显式确认，任务经验默认只形成未验证候选。

`memory_records.py` 定义严格契约，`memory_store.py` 管理版本，`memory_service.py` 管理读写政策与投影，`memory_runtime.py` 将其接入 CLI/Web 共用图。功能默认关闭；启用也不意味着把全部历史自动保存或注入模型。

| 层次 | 当前实现 | 生命周期与作用 |
|---|---|---|
| 单次调用上下文 | 节点按职责构造 Prompt，并检查输入容量；Worker 使用保留完整调用组的有界消息副本 | 仅供当前节点或 Worker 工具循环使用 |
| 任务工作记忆 | [`MultiAgentState`](src/multi_agent_system/state.py) 保存路由、计划、子任务、结果、审查和资源状态 | 绑定一个 `thread_id`，由各节点按需读取 |
| Worker 隔离上下文 | [`WorkerInputState`](src/multi_agent_system/state.py) 只包含目标、直接上游结果、工具权限、输出要求、返工反馈和资源配额 | 防止完整会话、无关分支和主图状态进入 Worker |
| 会话短期记忆 | `conversation_context` 保存预算内近期问答与带标记的历史原文抽取摘要 | 在同一 `conversation_id` 的后续轮次中复用；原始 Web 历史仍单独保留 |
| 恢复存储 | `MemorySaver` 保存进程内 checkpoint；Redis 保存 checkpoint、pending writes 和 Web 任务记录 | 用于暂停、恢复和历史读取，不等同于长期语义记忆 |
| 长期记忆 | SQLite 保存四作用域 × 四类型及来源/时效/版本，MemoryService 有界检索与受控写入 | 可按授权跨任务复用；进入模型前仍受独立预算与整包容量治理 |

主状态中的 `messages` 主要用于运行轨迹、界面展示和 checkpoint，并不会自动作为完整历史传给每个模型节点。

#### 输入容量与有效上下文

[`memory.py`](src/multi_agent_system/memory.py) 统一准备模型输入，不调用模型、不下载 tokenizer，也不改变原始任务历史。默认按 UTF-8 字节数做保守 Token 估算，工具参数、调用 ID、消息包装和 schema 都参与估算；函数可注入本地 tokenizer，但当前启动配置尚未按模型自动选用 tokenizer。这个数不是供应商的精确 Token 用量，中文文本通常会被估得偏大。

当前全局默认值为 `320000` 个估算单位，按中英混合、JSON 和工具协议负载粗略对应约 8～10 万模型 Token；这是容量目标而非精确承诺。实际请求还要扣除 `16000` 输出预留与当前 Pydantic/工具 schema，且不得超过模型和 OpenAI-compatible 网关的真实上下文上限。

```text
可用消息容量 = CONTEXT_MAX_TOKENS
             - CONTEXT_OUTPUT_RESERVE
             - 当前工具 / 结构化输出 schema 的估算开销

消息容量内保留：固定任务与约束 + 有界历史/上游结果 + 完整的近期工具调用组
```

`CONTEXT_MAX_TOKENS` 是本地设置的输入容量政策，不会自动探测服务商模型窗口；应根据实际模型配置。输出预留只参与输入准入计算，并非供应商端的生成长度硬限制。若任务、验收标准或必需协议字段仍放不下，进入 `context_budget_exceeded` 的可解释 blocked 终态，不悄悄删除关键约束。

**输入窗口预算和运行总预算是两回事**：这里防止单次请求上下文过长；`TOTAL_ACTION_BUDGET` 仍按动作计费，累计 Token 依旧不参与运行终止策略。

#### 三种上下文管理方法

1. **跨轮次：预算窗口 + 抽取摘要。** 默认最多保留最近 6 轮完整问答，历史总预算为 4000 个估算单位，其中旧轮次摘要最多 800。容量不足时优先保留最近轮次；超长内容带截断标记。旧内容以“历史摘要（原文抽取，可能省略细节）”标记，不额外调用 LLM，也不凭空提炼用户偏好。只接收 user/assistant 文本；工具日志、原始异常和其他角色不进入新轮次。
2. **Worker 间：交付字段投影。** 直接上游只传 `task_id/agent_type/attempt/status/content`，默认合计 3000、单条 content 1500 个估算单位；每个依赖 ID 都保留，诊断与原始观察不传播。Router / Orchestrator 使用精简会话记忆；simple 把必要记忆写入目标，team 由 Orchestrator 形成自足子任务。
3. **Worker 内：按完整调用组裁剪。** 局部历史仍记录完整调用顺序，但每次模型调用使用有界副本。初始任务/约束固定保留；`AIMessage(tool_calls)` 与全部匹配 `ToolMessage` 作为不可拆组，超预算时移除最旧完整组，保留最近完整组。观察内容可以缩短，参数、工具名和 ID 不被截断；结构化观察缩短后仍是合法 JSON，并保留成功/错误状态。重复或孤立 ID 被拒绝。

Reviewer 对交付与诊断分别做有界投影，并提示截断不代表事实已验证。节点完成后，策略、会话摘要和结构化结果可随 checkpoint 恢复；压缩模型输入不等于清理 Web 历史或原始交付记录。UI 的 `user_summary` 仍只是展示转换，不是这里的记忆摘要。

#### 配置与剩余限制

| 环境变量 | 默认值 | 用途 |
|---|---:|---|
| `CONTEXT_MAX_TOKENS` | `320000` | 单次整包准入容量；字节估算下约覆盖 8～10 万模型 Token 的混合负载 |
| `CONTEXT_OUTPUT_RESERVE` | `16000` | 从总容量中扣除的输出预留；不是服务商生成上限 |
| `CONVERSATION_MAX_TOKENS` | `4000` | 跨轮次记忆总容量 |
| `SUMMARY_MAX_TOKENS` | `800` | 旧轮次抽取摘要上限 |
| `MEMORY_RECENT_TURNS` | `6` | 最多保留的近期问答轮数；容量不足时可更少 |
| `UPSTREAM_MAX_TOKENS` | `3000` | Worker 直接上游交付投影的总容量 |
| `TOOL_OUTPUT_MAX_CHARS` | `12000` | 单条工具结果序列化字符上限；进入模型前仍会按窗口进一步裁剪 |
| `PYTHON_TIMEOUT_SECONDS` | `15` | 单次 Python 子进程硬超时，不能超过剩余运行时间 |

这些配置都有默认值，示例见 `.env.example`；`.env` 仍仅由用户本人操作。抽取摘要不是语义理解算法，可能遗漏较早细节；近期工具组被裁剪后也无法重新检索全文。后续继续完善本地模型 tokenizer 适配、重要约束/事实槽位保留、长结果引用与摘要质量评测。长期记忆已选定 SQLite、四种作用域和独立读写工作流，具体实施见 L1；第一版先做结构化记录与关键词检索，不要求向量数据库。checkpoint 负责恢复执行，长期记忆负责按需辅助决策，二者不能混为一层，也不能用长期存储代替单次上下文容量治理。

### 二、工具调用

#### 当前工具与职责边界

| 工具 | 允许的 Worker | 执行位置 | 当前返回值 |
|---|---|---|---|
| `web_search` | ResearchAgent | AgentG Web/CLI 进程，访问 DDGS；失败时降级到 DuckDuckGo HTML/Lite | 结构化结果的 `data` 为标题、URL、摘要文本 |
| `current_weather` | ResearchAgent | AgentG Web/CLI 进程，访问 Open-Meteo 地理编码和实时天气接口 | 结构化结果的 `data` 为天气对象，失败有稳定错误码 |
| `python_repl` | CodeAgent、DataAgent | 每次启动独立 Python 子进程，绑定目录作为 cwd | 结构化结果的 `data` 含有界 stdout/stderr、退出码和截断标记 |

工具通过 LangChain `@tool` 注册，函数签名和文档字符串会形成提供给模型的工具描述。`WORKER_TOOLBOX` 定义角色级能力；Orchestrator 在 `SubTaskSpec.allowed_tools` 中给出任务级白名单，实际绑定集合为两者交集：

```text
available_tools = WORKER_TOOLBOX[worker_type] ∩ subtask.allowed_tools
```

当前完整调用链位于 [`_run_worker()`](src/multi_agent_system/agents.py)：

```text
构造最小 Worker 上下文
    ↓
llm.bind_tools(available_tools)
    ↓
模型返回 AIMessage(tool_calls=[name, args, id, ...])
    ↓
角色权限 ∩ 子任务白名单 + Pydantic 严格参数校验 + 资源边界检查
    ↓
统一 dispatcher 执行授权工具，得到 ToolExecutionResult
    ↓
为每次调用追加匹配 tool_call_id 的 ToolMessage
    ↓
按输入容量保留完整近期调用组，把有界消息副本发给模型
    ↓
继续调用工具，或检查 required_tools 成功证据后提交最终内容
```

模型一次响应可以提出多个工具调用；AgentG 会逐个执行并分别回传 `ToolMessage`。这里的“并行”目前发生在 LangGraph 的多个独立 Worker 之间，同一 Worker 一轮内的多个工具调用仍按顺序执行。未授权、超额或未实现的调用也会得到对应的错误 `ToolMessage`，让模型能够观察失败而不是丢失协议配对。

[`tool_runtime.py`](src/multi_agent_system/tool_runtime.py) 按已授权工具注册表分发：拒绝缺失、错误类型和未声明参数。工作目录、超时与输出上限使用宿主注入参数，不暴露给模型，也不允许模型覆盖。模型没有返回 ID 时生成当前轮唯一 ID；重复 ID 在执行工具前拒绝。

统一结果为 `ToolExecutionResult{ok, data, error_code, message, retryable, provenance, elapsed_ms, executed, truncated}`。成功/失败读取稳定字段，不再扫描“执行报错”等文字；`executed` 决定是否累计实际工具次数和动作单位，参数校验失败不占实际执行额度，但仍记错误。当前 dispatcher 不做隐式重试，`retryable` 是后续策略可读取的元数据。

`web_search` 的一次 Agent 工具调用内部使用有界 provider 回退：Bing RSS → 百度 HTML → DDGS → DuckDuckGo HTML。它们是同一只读搜索能力的传输来源回退，不会在资源账本中伪装成额外模型工具调用；成功时统一返回 `query/provider/results[]`，每条结果含 `title/url/snippet`，失败时在结构化错误中保留各 provider 的异常类型或空结果诊断。测试会逐一 mock 这些外部入口，不依赖真实网络。

最终交付内容与运行证据分开保存：`worker_results[].content` 保存交付回答或简洁失败说明；`tool_observations` 保存调用 ID、工具名、有界输出、错误标记及类型化 `result`；代码、诊断、资源用量与 UI 轨迹分别进入 `code_snippets`、`error_logs`、`resource_events` 和 `messages`。Reviewer 分区读取并检查必需工具证据，不把执行日志当作最终答案。

Python 每次调用使用独立变量空间和最小环境，不能依赖前一调用留下的变量；需要持续保存的数据应显式写入绑定工作区。Python 审计层默认拒绝网络与派生进程，实时天气/搜索应交给 ResearchAgent。stdout/stderr 有界捕获，死循环可在超时后终止；不使用进程级 cwd/stdout 锁，独立 Code/Data 调用可并发。

#### 资源终止策略

| 环境变量 | 默认值 | 含义 |
|---|---:|---|
| `MAX_TOOL_ROUNDS` | `4` | 单个 Worker 最多经历的工具观察轮次；一轮可包含多个调用 |
| `MAX_TOOL_CALLS` | `12` | 整个工作流最多实际执行的工具调用数 |
| `MAX_CONSECUTIVE_ERRORS` | `2` | 单个 Worker 连续工具失败达到该值即终止 |
| `MAX_RUN_SECONDS` | `120` | 整个工作流的协作式墙钟截止时间 |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | `180` | 单次模型 HTTP 请求硬超时；运行时取它与 `MAX_RUN_SECONDS` 的较小值 |
| `TOTAL_ACTION_BUDGET` | `40` | 整个工作流可用动作单位总数 |
| `MAX_REVIEW_ROUNDS` | `2` | 每轮任务的 Reviewer 总轮数，含首次；最多返工 `值 - 1` 次，不扩大资源额度 |

一次模型调用或实际工具执行各消耗 1 个动作单位，累计 Token 暂不计入。调度器将剩余额度切成互不重叠的并行 Worker 配额；触发上限后记录 `termination.source/reason` 并进入 `blocked`。总运行时间仍在调用边界协作检查；模型 HTTP 请求和 Python 子进程另有硬超时，避免底层调用无响应时无限等待。Python 超时取配置值与剩余墙钟时间的较小值，退出后回传 `python_timeout`。

资源值由用户在受保护的 `.env` 中修改，变量名和安全示例见 `.env.example`；代码默认值位于 `src/multi_agent_system/config.py`，修改环境变量后需要重启 Web/CLI 进程。对于“五个城市人口检索”这类多目标外部查询，若终态原因为 `max_run_seconds_exceeded`，建议先只把 `MAX_RUN_SECONDS` 从 `120` 调到 `300`；只有实际出现相应原因时，再考虑把 `MAX_TOOL_CALLS` 调到 `20` 或把 `TOTAL_ACTION_BUDGET` 调到 `60`。不建议仅为掩盖失败而提高 `MAX_CONSECUTIVE_ERRORS`。

| 终止原因 | 触发条件 |
|---|---|
| `max_run_seconds_exceeded` | 到达整个工作流的协作式墙钟截止时间 |
| `max_tool_rounds_exceeded` | 单个 Worker 的模型—工具观察轮次达到上限后仍请求工具 |
| `max_tool_calls_exceeded` | 整个工作流的实际工具执行次数耗尽 |
| `max_consecutive_errors_exceeded` | 单个 Worker 连续工具错误达到上限 |
| `total_action_budget_exhausted` | 模型调用与实际工具执行合计动作额度耗尽 |
| `context_budget_exceeded` | 必需任务约束、Schema、消息或最新完整工具调用组无法装入输入预算 |
| `invalid_tool_history` | 工具调用 ID 重复、观察孤立或 AI/Tool 消息配对不完整，协议被拒绝 |

`python_timeout` 是单次 Python 工具的硬超时错误，不一定立即终止整个工作流；它会进入工具错误计数，连续达到阈值时再触发 `max_consecutive_errors_exceeded`。必需工具证据缺失通常先进入定向返工，也不是立即的资源终止。

#### 已经做得较好的部分

- 已实现标准的“模型请求调用—宿主执行—观察结果回传—模型继续推理”闭环，并正确保留每个 `tool_call_id`。
- 角色工具箱和任务白名单形成两层权限检查，空值不再扩权；实时天气规范化为专用 `current_weather` 并检查成功证据。
- Worker 只看自己的目标、必要上游结果、工具和验收标准，工具观察不会污染其他无关 Worker。
- 工具轮次、实际调用、连续错误、墙钟和总动作预算都有边界；并行 Worker 不会共享同一份可重复消费的额度。
- 可交付答案与工具日志分离，并已有权限拒绝、`ToolMessage` 配对、连续错误和预算终止等自动化测试。

#### 当前问题与风险

| 优先级 | 问题 | 影响 |
|---|---|---|
| P0 | Python 独立子进程与审计钩子仍不构成 OS 沙箱 | Python 内省或原生扩展可能绕过审计；还没有 CPU/内存硬配额、OS 文件权限、容器网络隔离或 Windows Job Object 完整进程树治理 |
| P0 | 没有按工具风险分级的人工审批、运行中取消和 Web 身份鉴权 | 角色白名单能限制“可调用什么”，但不能代表用户已批准一次具体的写入或外部副作用 |
| P1 | `required_tools` 验证的是当前子任务的工具成功记录，不是完整事实覆盖 | 除窄范围天气规则外仍依赖计划正确声明 required；一次成功不保证所有目标都已查询或最终陈述与观察完全一致 |
| P1 | Worker 的局部工具消息历史和执行记录只在节点返回后进入 checkpoint | 节点中途崩溃时，最近一次外部调用可能已经产生副作用却尚未持久化；恢复会重跑节点，而当前没有幂等键、调用日志去重或补偿事务 |
| P1 | 输出虽已限长，但长结果缺少持久化引用和按需检索 | 被截断细节不能自动找回；完整 Web 历史与 checkpoint 尚无独立留存/存储容量策略 |
| P1 | AgentG 自身没有基于错误类别的重试、指数退避、限流和熔断 | 即使底层客户端可能自带有限重试，工作流也无法按业务错误语义决定是否重试；临时网络错误通常直接变成失败或依赖模型自行换参数 |
| P2 | 同一 Worker 一轮内的多个独立只读调用仍串行执行 | 多城市、分片查询等场景存在额外延迟；并行化前仍需保留依赖分析、配额切片和每工具并发上限 |
| P2 | `web_search` 已有多来源回退，但仍主要消费搜索结果页摘要，没有正文获取、来源等级、引文核验或内容级提示注入过滤 | 可用性优于单一端点，但搜索内容仍是不可信数据，结论的可追溯性和抗提示注入能力有限 |
| P2 | `current_weather` 对同名地点只按人口最大值消歧 | 用户只给城市名时可能命中错误国家或行政区，缺少候选确认以及国家/经纬度等强类型参数 |
| P2 | 已有单调用 elapsed_ms、来源和错误码，但缺少统一 trace/span、重试/缓存指标及参数脱敏 | UI 能展示过程，但还不足以完成生产级审计、性能分析和工具质量评测 |

#### 主流 Agent 系统的共同实现

OpenAI、Anthropic、LangGraph 与 MCP 的数据结构不同，但客户端工具调用都可以归纳为同一条受控协议：

```text
声明工具（name + description + JSON Schema）
  → 模型选择工具并返回 call id + typed arguments
  → 宿主校验 schema、权限、风险、预算与幂等性
  → 在本地、沙箱、远端服务或供应商托管环境执行
  → 以同一 call id 返回 typed result / typed error
  → 模型基于观察继续调用或给出最终答案
```

- OpenAI Function Calling 使用 JSON Schema 描述函数，通过 `call_id` 关联 `function_call` 与 `function_call_output`，支持严格 schema 和多函数调用；官方建议开启 strict mode。OpenAI Agents SDK 在此基础上加入函数自动封装、输入/输出 guardrail、超时、失败处理、追踪、延迟加载和逐调用审批。[Function Calling](https://developers.openai.com/api/docs/guides/function-calling) · [Agents SDK Tools](https://openai.github.io/openai-agents-python/tools/) · [Human-in-the-loop](https://openai.github.io/openai-agents-python/human_in_the_loop/)
- Anthropic 将客户端工具定义为 `name + description + input_schema`；模型产生带唯一 ID 的 `tool_use`，应用执行后用对应 `tool_use_id` 的 `tool_result` 回传。官方同时强调详细工具描述、必要的输入示例、清晰命名空间和只返回高信号字段。[Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) · [Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)
- LangGraph 的预构建 `ToolNode` 可以统一处理工具执行、并行调用、错误处理和状态注入，`tools_condition` 根据模型是否产生工具调用决定继续工具环还是结束；自定义工作流仍可在其外层保留预算和业务路由。[LangChain Tools / ToolNode](https://docs.langchain.com/oss/python/langchain/tools)
- MCP 使用 `tools/list` 动态发现工具、`tools/call` 调用工具，同时定义 `inputSchema`、可选 `outputSchema`、`structuredContent` 和 `isError`。`readOnly/destructive/idempotent/openWorld` 等 annotation 可帮助风险展示，但规范明确这些标注来自不可信服务时不能作为授权依据，并建议敏感调用保留用户确认。[MCP Tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

这些模式强调由模型提出结构化调用意图，再由宿主运行时校验、授权、执行、记录与终止。AgentG 本次保留自定义 Worker loop 的预算和调度控制，并把参数校验、类型化结果与子进程执行抽为独立模块；上述外部文档作为进一步演进的参考。

#### AgentG 可直接借鉴的演进路线

已落地：空白名单不授权、simple 最小工具集合、`required_tools` 证据检查、通用 dispatcher、统一结构化结果、一次一 Python 子进程、硬超时与输出/上下文预算。

下一阶段：

1. **OS 级执行边界**：只挂载授权目录，配置文件权限、网络、CPU/内存限制与完整进程树终止；Python 审计仅作纵深防御。
2. **幂等调用日志**：执行前持久化 `invocation_id + args_hash + status`，执行后记录结果；恢复复用已完成结果，写入类工具使用业务幂等键或补偿/人工确认。
3. **风险元数据与审批/取消**：工具注册表补充只读/破坏性、幂等性、并发等策略；敏感动作在执行前展示参数和目标范围，确认后沿 checkpoint 继续。
4. **可靠性与长结果检索**：基于稳定错误码与 `retryable` 做有预算的退避重试；原始大结果单独存储，仅传有来源引用的摘要并允许按需读取。
5. **受控扩展**：只对无依赖、只读且配额独立的调用增加 Worker 内并行；工具规模扩大后再引入 MCP / 延迟发现。保留当前权限、预算和交付/诊断分区。
6. **可观测与评测**：建立 run/worker/tool-call 三级 trace、参数脱敏、来源覆盖验证，评测选择准确率、参数有效率、成功率和恢复重复执行率。

### 三、自我推理

AgentG 不是让一个模型无限自主循环的“单体 Agent”，而是由 LangGraph 确定性控制流约束模型决策的 Agentic Workflow。它同时组合了三种常见范式：

| 范式 | AgentG 中的对应实现 | 边界 |
|---|---|---|
| Plan-and-Execute | Task Router 选择 simple/team；Orchestrator 生成类型化子任务 DAG；调度器按依赖批次执行 | 计划由模型生成，但依赖就绪、Fan-out/Fan-in 和资源终止由代码决定 |
| ReAct | Worker 根据目标提出工具调用，读取 `ToolMessage` 观察，再决定继续调用或输出答案 | 只发生在单个 Worker 的局部上下文，受工具白名单、轮次、次数、时间和动作预算限制 |
| Reflection | Reviewer 按验收标准返回 `pass/revise/blocked`，`revision_targets` 触发目标任务及下游闭包返工 | 由 `MAX_REVIEW_ROUNDS` 限次；默认审查 2 轮、最多返工 1 轮，不允许无限自我反思 |

可观察的推理控制链如下：

```text
任务理解与分流
  → 结构化规划
  → 确定性依赖调度
  → Worker 局部“行动—观察”循环
  → 记录证据关联的公开决策轨迹
  → Synthesis 汇总
  → 必需工具证据检查 → Reviewer 按标准审查
  → 通过 / 版本化编辑式返工 / 阻塞终止
```

这种设计的优点是把“生成式判断”和“确定性控制”分开：模型负责语义理解、计划内容、工具选择、候选补丁和结果综合；代码负责 schema、权限、依赖、预算、状态迁移、补丁边界和恢复。Worker 以 `DecisionTrace` 独立记录接收范围、工具选择、工具观察、修订与交付状态，并用 `tool_call`/`artifact` 引用连接证据。前端展示的是这些公开、可验证的执行摘要，不声称或尝试还原模型私有思维链。

当前自我推理仍有以下不足：Router、Orchestrator 与 Reviewer 基本都是单次模型判断；已有工具白名单/必需集合合法性和成功观察检查，但尚未系统验证任务覆盖率、验收标准质量与事实证据覆盖；执行发现前提错误时主要依靠定向返工，缺少受控重新规划；同一模型承担规划、执行和审查容易产生相关性偏差；Reviewer 置信度尚未驱动人工确认或不同审查策略；推理质量也没有形成独立离线评测指标。

后续可以按风险逐步增强：

1. 在 Orchestrator 后增加确定性 Plan Validator，验证 DAG 可达、工具 schema、输出契约、任务覆盖和预算可执行性，不合格时只重做计划。
2. 区分“结果需要修订”和“计划前提失效”；后一类进入有次数上限的 Re-plan，并显式保留已完成且仍有效的子任务结果。
3. Reviewer 强制引用验收标准、对应交付字段和工具来源；事实类任务增加证据覆盖与来源质量检查。
4. 高价值任务让 Reviewer 使用独立模型或独立证据上下文，普通任务继续单 Reviewer；低置信度、破坏性操作或多次返工再进入人工确认。
5. 为 Router 准确率、计划有效率、无效工具调用率、返工命中率、Reviewer 误判率和端到端成功率建立固定评测集，并通过 trace 定位失败阶段。

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
│   ├── memory.py                 # 容量估算、会话摘要、投影与完整工具组裁剪
│   ├── memory_records.py         # 长期记忆严格契约与稳定身份
│   ├── memory_store.py           # 独立 SQLite 版本/来源/幂等仓库
│   ├── memory_service.py         # 受控写入、确认、关键词/Top-K 与独立预算
│   ├── memory_runtime.py         # 宿主身份、最小投影、图接入与恢复重校验
│   ├── memory_paths.py           # Python 记忆目录拒绝列表（非 OS 沙箱）
│   ├── memory_management.py      # 严格管理命令、纠错/生命周期及关联清理
│   ├── memory_api.py             # 本地管理 API、宿主身份/产物授权
│   ├── memory_guard.py           # 逐次 Worker 模型调用的宿主撤销检查
│   ├── artifact_runtime.py       # 版本化产物、类型化修订指令与受控补丁
│   ├── tool_runtime.py           # 授权工具分发、严格参数与结构化结果
│   ├── python_runtime.py         # 独立 Python 子进程、超时与有界输出
│   ├── python_runner.py          # 子进程入口、环境与 Python 审计策略
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
$env:PYTHON_DOTENV_DISABLED='1'
$env:PYTHONDONTWRITEBYTECODE='1'
D:\anconda\envs\Docs2KG\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

上述环境变量只作用于当前测试终端；正常启动服务需使用新的终端，或移除 `PYTHON_DOTENV_DISABLED`，否则用户配置文件不会加载。

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

## 系统级限制

记忆、工具和推理的具体问题已在三个核心能力中分别列出；此外还有以下横向边界：

- 四类 Agent 共享同一个 `ChatOpenAI` 实例和模型配置，不是独立进程或远程服务。
- Redis 3.2 自定义 checkpointer 已覆盖当前同步图运行路径，尚未实现 LangGraph 异步 `aget_tuple/aput/alist` 接口；当前 CLI 与 Web 均使用同步 `stream`，不受影响。
- 工作目录目前通过文本框录入服务端可见的绝对路径。标准浏览器目录选择器不会向页面暴露真实绝对路径，因此尚未提供类似桌面 IDE 的原生“打开文件夹”对话框。
- 开发热加载会重启 Web 进程；Redis 模式可依托 checkpoint 恢复 stale running 任务，但内存模式会丢失运行中任务记录。
- Web 缺少完整的认证、限流、取消、输入长度限制和生产部署加固，不能直接暴露到不可信公网。
- Reviewer 的工具诊断是有界输入视图：小型结构化结果完整保留，大结果可能在审查层转换为带 `projection_truncated=true` 的文本预览；这保证容量可控，但 Reviewer 仍不能把被投影截断的缺失字段当作已验证事实。

## 后续路线图

下表分别标记已完成项、待实现计划与待完成联调；仅明确标为完成的部分已交付，没有预设工期。建议继续可信本地使用，按独立开发包推进记忆保真和调用恢复。P0 安全项可并行设计，但在扩大文件/网络权限、接受不可信代码或提供多人/公网服务之前必须完成。

| 阶段 | 优先级 / 当前状态 | 目标与依赖 |
|---|---|---|
| R0 当前回归修复 | ✅ 2026-09-18 完成；2026-09-21 复核 | Reviewer 结构化投影、动态整包预算、证据筛选及 Synthesis/最终输出去重；后续修复累计全量 270 项通过 |
| V0 真实环境验收 | 进行中 | 用户反馈 L0～L2 除 L2-04 外已通过；继续复测 L2-04、L3-02，以及刷新/Redis 恢复、审查次数配置，按 `TestSamples.md` 记录 |
| S1 安全边界 | P0，待方案确认 | 选定 Windows 受限运行或容器路线；是扩大权限与多人部署的前置条件 |
| M1 记忆保真 | P1，建议首个开发包 | 在已有长度治理之上提高 Token 估算准确性、关键约束保留和长结果可追溯性 |
| L1 长期记忆 | 2026-10-16 L1.1～L1.6 完成 | 契约、仓库、受控读写、图恢复和管理/逻辑删除已接入；下一步 L1.7 真实验收；Redis 保持 checkpoint |
| T1 工具调用日志 | P1，建议第二个开发包 | 为节点重放建立调用身份、结果复用和不确定状态处理；先于自动重试/自动续跑 |
| T2 可靠性与证据 | P1，依赖 T1；敏感操作还依赖 S1 | 有预算重试、审批/取消、目标级证据覆盖和 trace |
| R1 / W1 推理与体验 | P2，基础稳定后推进 | 受控重规划、热加载恢复、长会话性能；跨会话语义记忆按需求另立阶段 |

### R0：当前回归基线已恢复

Reviewer 诊断投影现已使未被工具运行时截断的小型 `ToolExecutionResult.data` 完整进入审查上下文，并分别记录 `source_truncated` 与 `projection_truncated`。大结果继续受恢复后的 `1200/320/4800` 边界约束；整份请求通过与最终模型调用相同的容量准入逐级缩放，等价重复证据和重复最终输出不再占用窗口。针对性测试 14 项、全量测试 260 项均通过，可以进入 V0。

### V0：继续完成真实环境验收

由用户在正常终端启动应用，避免测试终端的 `PYTHON_DOTENV_DISABLED=1` 阻止用户配置加载。启用 Redis 时由用户启动，Agent 代启必须先申请权限。无须为了本轮加固重建或覆盖 `.env`。

1. 新会话运行“三个随机城市当前天气”，核对城市数、来源、观测时间、成功工具记录及自然语言结论。模型拆分可变化，不要求真实执行路径与 mock 的固定五子任务完全相同。
2. 同会话追问“只比较刚才三个城市中气温最高的城市”，观察是否继承必要结论；若要求重新查询，必须产生新的实时观察，而不是把历史天气当成当前天气。
3. 在独立测试工作目录运行简单统计和文件产物任务，核对相对路径、产物位置及跨调用变量不保留的行为，不使用敏感文件。
4. 运行中刷新页面；Redis 模式在专门的无副作用测试任务上检查重启后按原 `thread_id` 恢复。现阶段允许未提交节点重跑，不能据此声称工具 exactly-once。

验收记录应保存样例、脱敏后的任务 ID、期望/实际结果、失败阶段及资源消耗；不要记录密钥或 `.env` 内容。通过后再把“真实服务联调”标为完成；本次尚无该证据。

### S1：建立真正的执行安全边界

目标：让 Python 不能凭服务账号的权限访问整个宿主系统。需要先确认执行后端及用户允许的工作区范围，不能仅增加审计规则后就宣布沙箱完成。

- 执行面：以现有 `python_runtime.py` 为接口边界，选择受限容器或明确的 OS 权限方案；限制文件、网络、CPU/内存和完整进程树，默认不继承凭据。
- 工作区：服务端注册/白名单与读写范围分开；不能把含 `.env` 的完整宿主项目直接暴露给工具进程。导入工作文件、挂载与产物导出均要排除保护文件并复查路径/重解析点。
- 工具与 Web：设计受控的列举/读取/搜索/写入/补丁工具，继续经过同一 dispatcher；写入、覆盖、删除等行为须有风险策略与审批。多人服务还需身份认证与按会话隔离，UI 工作目录输入不是授权依据。

验收：用临时测试目录覆盖绝对路径、`..`、符号链接/目录联接、并发竞态、网络、超时、内存和子进程残留；OS 层必须拒绝越界，而非仅依赖 Python 异常。所有检查都不得读取真实 `.env`。交付物包括执行后端、权限配置、威胁模型和平台化测试记录；若涉及环境安装或新服务启动，先确认授权。

### M1：从“控制长度”升级到“保留关键信息”

这是维持现有可信本地边界时建议首先实现的开发包，重点不是立刻上向量数据库。

- **M1.1 容量计量**：在 `config.py` / `memory.py` 增加显式的本地 tokenizer 适配入口；已知模型用已安装/已授权资源计量，未知模型保守回退并记录所用策略。消息、工具 schema、调用参数与输出预留使用同一计量口径；累计 Token 仍不参与动作预算。
- **M1.2 关键事实与约束**：为记忆建立版本化的事实/约束条目，记录来源轮次、作用域、有效时间与更新关系。预算不足时先移除低优先级历史，而不是仅保留字符串开头；冲突不得静默覆盖。时间敏感天气结果不能无限期复用，也不能把搜索文本提升为用户指令。
- **M1.3 长结果引用**：将允许保存的大结果与摘要分开，通过不透明 `artifact_id`、来源、内容哈希和大小元数据定位；Worker 仅按任务权限读取需要的片段。新增读取能力必须复用 dispatcher 与路径保护，不能让模型自行指定任意宿主绝对路径。
- **M1.4 评测与迁移**：增加 20 轮以上对话、长中文/代码混合文本、约束变更、过期事实和跨会话隔离样例；保留旧 checkpoint 的默认兼容，不重写用户原始历史。依赖变化同步两个依赖清单。

验收：所有模型输入在既定容量内，调用组不拆散、来源不丢失、其他会话数据不混入；固定样例的关键约束保留率须优于或不低于现有基线。长结果可以按授权引用重新读取；重启后来源/版本与摘要一致；tokenizer 缺失时明确回退，不能测试时偷偷联网下载。预算配置与所有裁剪/检索统计需有可检查记录，而不承诺无损记忆。

### L1：长期记忆系统（L1.1～L1.6 已实现）

以下方案在 2026-10-07 确认；截至 2026-10-16，L1.1～L1.6 的受控读写、管理 UI 与逻辑删除已实现，L1.7 真实验收以及物理擦除/自动保留治理仍待完成。目标是按授权复用必要知识，而不是把所有历史塞回 Prompt；长期存储不会扩大模型窗口或替代 Reviewer 动态预算。

#### L1.0 已确定的架构与边界

**作用域与内容类型是两个独立维度，可组合使用，不是八种互斥的记忆。**

| 作用域 `scope` | 归属与默认读取范围 | 可组合的内容示例 |
|---|---|---|
| 用户级 `user` | 当前用户授权的任务；不向其他用户开放 | 偏好：默认使用中文；规则：用户确认的报告格式要求 |
| 项目级 `project` | 绑定同一注册项目且有权限的任务 | 事实：项目采用 Orchestrator–Worker；规则：Python 产物目录约定；经验：已验证的项目修复方法 |
| 会话级 `conversation` | 同一 `conversation_id` 的后续轮次；默认不跨会话 | 事实：本次已选择的城市；规则：只修复缺失城市；经验：本会话失败原因与已尝试方法 |
| 产物级 `artifact` | 有权访问相应产物的任务；不因引用而获得文件访问权 | 事实：版本、哈希与来源；规则：不可改动的字段；经验：某版产物的修订记录 |

内容类型 `kind` 固定为偏好 `preference`、事实 `fact`、经验 `experience`、规则 `rule`。例如，项目级规则和用户级偏好可以同时命中；会话级事实也可以关联版本化产物。规则条目只能作为授权范围内的任务约束，不能覆盖系统安全策略或工具权限。经验记录的是公开结论、动作及证据，不保存模型私有思维链。

存储职责保持分离：

- **SQLite**：长期记忆的事实来源，保存条目、来源、版本、候选状态、有效时间、更新与删除关系；第一版使用结构化筛选和有界关键词检索。
- **Redis**：继续保存 LangGraph checkpoint、metadata、父链、pending writes、Web 任务历史及进度，不迁移现有 key schema 或更换 checkpointer。关闭 Redis 时仍沿用现有 `MemorySaver`；SQLite 的可用性独立管理。
- **模型上下文**：仅接收本次需要的记忆投影；Redis 中可以保存投影快照用于恢复，但不能因此把 checkpoint 当作长期记忆库。

数据库默认位于项目应用根目录 `.agentg/memory/long_term.sqlite3`，路径不随终端 cwd、任务工作目录或 Temp 改变。启用功能时宿主创建仓库；默认数据目录排除 Git，默认及自定义库/辅助文件均排除热加载。Python 调用由宿主注入拒绝目录，普通 IO 与 SQLite 连接不能访问库及 WAL/SHM，不能移动其祖先；Web 不允许把库目录绑定为工作区。自定义库应使用专用数据目录，其父目录整体受保护，勿设为工作区或 Python 库根目录。审计层非 OS 隔离；Docker 持久卷、系统权限/保留政策待完成。SQLite 备份独立于 Redis RDB/AOF。

#### L1.1 已完成：定义 `MemoryRecord` 与稳定身份

独立契约位于 [`memory_records.py`](src/multi_agent_system/memory_records.py)，不导入模型、数据库、配置或文件执行逻辑，也不直接把 `MultiAgentState` 序列化为长期记忆。

| 已实现字段 | 职责与校验要求 |
|---|---|
| `memory_id`、`schema_version` | 唯一记录身份与结构迁移版本；不同记忆不能仅靠正文相同合并 |
| `scope`、`kind` | 四种作用域与四种内容类型，分别校验 |
| `namespace` | 服务端生成的归属标识，包含所属用户及适用的项目/会话/产物 ID；不得由模型任意指定 |
| `content` | 有界、可读的事实/偏好/经验/规则正文，不包含整段日志或凭据 |
| `source_refs` | 类型化来源：用户确认消息、`thread_id/task_id/tool_call_id`、产物版本/哈希或外部 URL；可回溯原始证据 |
| `write_basis`、`verification_status` | 显式用户请求或任务候选；区分用户确认、来源支持与未验证。模型置信度不能等同于可信事实 |
| `created_at`、`updated_at`、`observed_at`、`expires_at` | 记录时间与事实观测/有效时间，统一带时区；时效策略不能仅看入库时间 |
| `version`、`supersedes` | 版本及替代关系；更新产生新版本，不静默覆盖旧结论 |
| `status` | 候选、有效、已替代、已删除等生命周期；普通检索只返回可用条目 |
| `content_hash`、`idempotency_key` | 完整性检查与节点重放去重；同正文哈希不自动代表同作用域、同来源或同业务操作 |
| `artifact_ref`（适用时） | 组合引用 `thread_id + task_id + artifact_id + version + content_hash`，不能把任务内 ID 当作全局唯一产物 ID |

具体约定与接口：

- `MemoryIdentity` 默认本地 `user_id="local-user"`，`project_id/conversation_id` 由宿主显式提供。`namespace_for()` 根据 scope 构造归属，`validate_namespace()` 拒绝与宿主上下文不同的坐标；`MemoryNamespace.key()` 产生稳定元组。用户级不能携带项目/会话/产物坐标；项目级必须有项目 ID；会话级必须有会话 ID；产物级必须有会话 ID 与完整运行限定身份。项目 ID 可用于限定会话/产物归属，但不从路径或名称计算。
- ID 是最长 128 字符的 ASCII 不透明标识，不接受空白、路径、数字强转或显示标题。`MemoryRecord.memory_id` 默认为新 UUID；同一逻辑记忆修订沿用 ID、增加 version，并通过 `MemoryVersionRef` 指向上一版。UUID 不是重放去重键。
- enum 字段只接受成员或精确规范字符串；数值字段不接受字符串/布尔值。`source_refs` 接受数组并冻结为元组，最多 32 个且不能重复；正文最多 8000 字符，保留原始文本并核对哈希，不自动清理空白。
- `MemorySourceRef` 区分用户消息、工具结果、公开交付、产物和外部 URL，每种只接受对应必需定位字段；`ArtifactReference` 绑定运行、任务、产物、版本和 SHA-256。URL 仅允许绝对 HTTP(S)，拒绝嵌入凭据、空白、控制字符和反斜杠；这里不访问 URL 或证明内容正确。
- Python 构造要求带时区的 `datetime`，统一规范到 UTC；JSON 存储往返使用 `model_dump_json()` / `model_validate_json()`。更新时间不能早于创建时间，观测不能晚于更新时间，失效时间须晚于观测或创建时间；已经过期的历史记录允许载入。
- 默认 `status=candidate`、`verification_status=unverified`；active 不能未验证，active 偏好/规则必须 user_confirmed，确认必须有用户消息来源。source_supported 要有工具/产物/外部来源，单凭公开交付不足。`is_usable_at()` 只判断生命周期和时间，不授予读取权限或满足本轮必需工具证据。
- `validate_memory_successor(previous, successor)` 还核对实际前驱哈希、连续版本、namespace/kind 不变与时间单调性，拒绝从已删除/已替代当前版继续修订。`memory_idempotency_key()` 按 namespace、来源事件、操作版本和候选键生成确定性写入身份；真正的唯一约束、版本竞态与来源归属验证由后续仓库/服务实现。

测试见 [`test_memory_records.py`](tests/test_memory_records.py)，覆盖全部 16 种 scope/kind 组合及结构、序列化、来源、版本、哈希、时效、不可变输入和跨运行产物身份。当前 Web 仍没有认证或项目注册服务，现有运行图也没有使用这些新模型；不同归属的结构核对不能宣称为真实多人授权或来源真实性核验。

#### L1.2 已完成：SQLite 仓库与存储治理

实现位于 [`memory_store.py`](src/multi_agent_system/memory_store.py)，没有新增依赖或改动 Redis 存储。所有内容由宿主显式传入，仓库不调用模型、不提取聊天内容、不自动确认或验证来源。

| 接口/结构 | 当前行为 |
|---|---|
| `MemoryRepository` / `SQLiteMemoryRepository` | 独立接口与 SQLite 实现；每次操作单独连接并关闭，可用 `with` 管理仓库生命周期 |
| `memory_versions` / `memory_heads` / `memory_sources` | 不可变版本 JSON、筛选元数据、当前版本指针、版本化来源关联；旧版不覆盖 |
| `save(record, identity=...)` | 重校验契约和 namespace；事务内检查真实前驱、连续版本/哈希/时间；相同 namespace + 幂等键 + 完整载荷复用，否则明确冲突 |
| `get()` / `history()` | 指定 namespace 与宿主身份，获取当前版、指定旧版或分页历史；显式查看不等于可用于新任务 |
| `query_active(at=...)` | 精确 namespace + kind 筛选，只返回当前 active/已验证、到时生效且未过期的版本，不回退旧 active 版 |
| `list_records()` | 管理视图，按状态/类型分页查看候选、停用、已替代、删除及过期记录，不用于直接组装 Prompt |
| `backup(new_path)` / `restore(backup_path, new_path)` | SQLite 在线备份包含已提交 WAL；核对 schema、引用、契约和版本链，只允许新目标文件，不覆盖源库 |

输入值使用参数化 SQL，接口不接受任意 SQL。`BEGIN IMMEDIATE` 与唯一约束/当前版本比较保证一次写入的原子性，WAL 支持读写并行，但 SQLite 仍是单写者模型。连接设置 foreign keys、FULL 同步与有界 busy timeout；空库迁移为 v1，未知未来版本、异库、缺失约束/索引明确拒绝，不自动重建覆盖。状态变更通过合法新版本提交；这里只支持逻辑删除保留历史，不提供物理擦除。

错误区分 `MemoryStoreBusy`、`MemoryStoreUnavailable`、`MemoryStoreCorrupt`、`MemorySchemaError` 与 `MemoryConflict`，不冒充空检索；读取时核对哈希、索引和来源。namespace 不是多人认证或项目注册；L1.3 根据宿主来源目录校验定位归属，不声称证明事实真实性。SQLite/Redis 无跨库原子事务，L1.5 用提交前固化计划与 SQLite 幂等键处理节点重放，不等于分布式事务。

新配置（只更新 `.env.example`，不操作 `.env`）：

```dotenv
LONG_TERM_MEMORY_ENABLED=false
MEMORY_DB_PATH=.agentg/memory/long_term.sqlite3
MEMORY_DB_BUSY_TIMEOUT_MS=5000
```

`create_memory_repository(settings)` 关闭时返回 `None` 且无数据库 IO；CLI/Web 共用 `build_graph()` 在启用功能时调用宿主运行层并建库。相对路径以项目应用根目录解析；保护配置、SQLite URI 和内存库路径拒绝。锁等待允许 1～60000 ms。仓库列表默认上限 100（最高 1000），不同于服务层默认 Top-K=5 和记忆预算 4000；启用自动候选仍需独立开关。

备份调用示例（应用代码中使用，目标必须不存在）：

```python
from src.multi_agent_system.memory_store import SQLiteMemoryRepository

with SQLiteMemoryRepository(".agentg/memory/long_term.sqlite3") as repository:
    repository.backup(".agentg/backups/memory-2026-10-07.sqlite3")

with SQLiteMemoryRepository.restore(
    ".agentg/backups/memory-2026-10-07.sqlite3",
    ".agentg/memory/restored.sqlite3",
) as restored:
    pass  # 核查后由用户明确选择是否切换 MEMORY_DB_PATH
```

不要在运行中只复制主 `.sqlite3` 文件：最新提交可能仍在 WAL 中。备份失败时只清理本次创建的未完成副本；当前 API 不含自动轮换、加密、保留期或跨库系统备份，也不会自动合并历史删除政策。

验收见 [`test_memory_store.py`](tests/test_memory_store.py)：跨进程重开、并发单胜者与旧版不变、重复事件不增记录、锁超时、事务/迁移回滚、时效/作用域隔离、损坏报错、WAL 备份与新库恢复均覆盖。L1.5 另覆盖路径拒绝与提交重放；系统权限、物理清理和备份保留仍待完成。

#### L1.3 已实现：受控写入工作流

`MemoryWriteIntent → MemoryWritePlan → MemoryWriteResult` 为严格独立契约。模型不写数据库；宿主先核对来源定位是否在当前用户/项目/会话的可信目录内，再做敏感/时效检查、版本基线校验和提交。定位一致不等于事实正确，来源目录和 `MemoryEvidence.verified` 只接受内部宿主传入，不是 Web/模型可声明的权限。

**启用配置由用户本人修改 `.env`，随后重启 CLI/Web；Agent 不读取或修改此文件。** 无需安装新依赖，SQLite 使用现有 Python 标准库：

```dotenv
LONG_TERM_MEMORY_ENABLED=true
MEMORY_USER_ID=local-user
MEMORY_PROJECT_ID=agentg-project
MEMORY_TOP_K=5
MEMORY_MAX_TOKENS=4000
MEMORY_SCAN_LIMIT=200
MEMORY_AUTO_EXTRACT=false
```

`MEMORY_PROJECT_ID` 是用户手工指定的稳定 ID，不是文件夹名或路径；留空时禁用项目作用域写入，不推测项目身份。Web 会话 ID 由服务端生成并在后续轮次继承；CLI 新任务用自身 thread ID 作为会话 ID。当前为可信本地单用户绑定，不是账户认证或项目注册。

第一版明确采用窄语法，避免把普通对话、网页指令或模型建议误当写入授权。以下命令可直接放在现有任务输入框或 CLI：

```text
记住[用户/偏好/reply_style]：默认用中文，先给结论。
记住[项目/规则/report_style]：报告保留来源，使用清晰的小节。
记住[会话/事实/population]：杭州2024年行政市域人口资料待核验。
确认记忆[会话/实际memory_id]：与候选完全相同的正文
```

scope 使用“用户/项目/会话”，kind 使用“偏好/事实/经验/规则”，key 为 1～128 字符的规范 ASCII ID。相同 namespace/kind/key 是同一逻辑条目；明确更新产生新版本，竞争基线冲突不覆盖。不同 key 不做通用语义合并，同正文的新确认事件仍保留版本审计。用户消息“请记住默认中文”等自由文本目前不解析为保存命令；界面以宿主显示的写入结果为准。

候选的真实 ID 可从 `GET /api/tasks/{thread_id}` 的 `memory_write_results[].reference.memory_id` 或 CLI 最终状态获取，再替换确认命令中的占位 ID。当前没有记忆管理面板，不能把示例占位文本当作真实 ID。

显式偏好/规则可形成 user_confirmed/active；事实和经验先为 candidate/unverified，普通读取看不到。确认要求 ID 与正文完全匹配且来源属于当前宿主，不能仅回“是”或让模型设置确认标记。事实即使用户同意保存，也须宿主独立解析成功工具/产物证据并核对时效；只有带此核验目录的内部确认接口才能设 source_supported，普通聊天确认不会使外部事实自动生效。产物级写入通过内部类型化接口绑定完整 `ArtifactReference`，不接受任意宿主路径；L1.6 管理 UI 可查看/管理已授权产物记忆，尚无 UI 新建产物记忆入口。

`MEMORY_AUTO_EXTRACT=true` 时，确定性地从本轮最终公开交付产生至多一条会话级经验候选，附审查状态和 deliverable 来源；不索引所有中间答案、工具日志或异常，不调用额外模型，正文超过上限不截断后伪装完整经验。失败经验也只作未验证候选，Reviewer 通过不会自动启用。默认关闭自动提取；敏感内容检测是保守模式匹配，不是完整 DLP/语义安全分类器，敏感项目不要据此假定自动提取绝不会泄漏。

```text
显式记忆请求 / 任务后候选提取
  → MemoryRecord 校验与服务端作用域绑定
  → 来源、敏感信息与时效检查
  → 同作用域去重、冲突识别
  → 按政策确认（必要时由用户确认）
  → SQLite 事务提交新版本 / 替代关系
  → 记录幂等写入结果与可回溯来源
```

- 模型只提出候选，不直接写数据库、不决定跨项目权限。通过 Reviewer 也不是事实正确性证明；客观事实仍需来源支持，用户确认偏好也不等于确认外部事实。
- 显式请求按清晰的记忆目标处理；普通聊天不默认视为“同意长期保存”。任务候选先保持候选状态，再按内容类型、依据和确认策略启用。
- 当前按稳定 key 和真实前驱版本识别冲突；未确认候选不能挤掉已生效记录，并发写入一方冲突。通用自然语言矛盾识别不在本轮实现范围。当前明确命令可更新旧偏好，但不能解除工具/安全边界。
- 事实默认带观测/失效时间，一般默认 TTL 30 天，天气/实时标记最长 1 小时；人口正文必须含统计年份和行政/都会区口径。候选的默认时间用于保守时效管理，不证明真实观测时间；启用前须由宿主对来源核验。旧事实不替代实时工具调用。
- 不读取或保存 `.env`、密钥、未经同意的敏感内容、模型私有思维链；不自动索引全部工具日志、异常或网页文本，也不把网页中的指令升级为长期规则。
- 若候选提取使用额外模型调用，必须计入现有动作/时间预算，或使用显式独立的后台额度与任务状态；不能在任务终态后暗中无限调用。候选提取失败不撤销已完成的用户交付，但需展示记忆未保存。
- 提交身份结合来源运行、提取步骤/版本和候选身份，覆盖“SQLite 已提交、Redis 未落盘”后的重放；不能只用正文哈希去重有意的后续更新。

验收：显式请求可保存并查询，自动候选未确认不进入普通检索，冲突可追溯、敏感内容拒绝、预算不足可解释、重放不重复写入；Reviewer 通过不会自动把所有输出变成有效事实。

#### L1.4 已实现：按需读取与独立预算

```text
任务/子任务目标 + 服务端身份与作用域
  → 权限、状态、版本、有效时间过滤
  → 结构化筛选 + 有界关键词召回
  → 相关性排序、去重、Top-K
  → 角色相关字段投影 + 独立记忆长度预算
  → 与当前任务/协议/schema 一起通过整包上下文准入
  → 保存检索引用、版本及实际输入快照
```

- 权限过滤先于相关性排序；默认可读取授权的用户/项目记忆、本会话记忆和有权访问的产物记忆，不能检索所有会话后再依靠 Prompt 隔离。
- 第一版使用英文词与中文双字片段的确定性关键词匹配，无 FTS5、embedding 或外部向量服务。`MEMORY_TOP_K=5`、`MEMORY_MAX_TOKENS=4000`、`MEMORY_SCAN_LIMIT=200` 已生效；分别允许 1～100、2～100000、1～999。扫描上限按已授权 namespace 分配，命中/省略/扫描受限统计写入快照；召回不是事实核验，也不承诺语义召回率。
- 用户/项目偏好作为通用展示约束参与排序；规则和 `memory_required_ids` 指定的条目是必要项。必要项超过 Top-K、扫描或容量上限时 blocked；可选项按完整条目省略和去重，不截成半条规则。必要 ID 可以由宿主调用 `create_initial_state(..., memory_required_ids=[...])` 或 Web `POST /api/tasks` 的同名数组传入；客户端不能传身份、授权目录、写入计划或投影。未知/未授权必要 ID 不作为空结果忽略。
- 记忆预算使用与 `memory.py` 一致的计量口径；当前仍是 UTF-8 字节估算单位，不宣称真实 Token 计数。优先保留当前目标、权限、验收标准和协议，记忆只使用分配到的容量；必要约束放不下时结构化阻塞，不静默删掉约束。
- 检索条目必须包含来源、版本、时效与验证状态，低可信经验仅供参考。历史工具结果不能满足本轮 `required_tools` 的成功证据门禁；实时任务仍调用当前工具。
- 被删除、撤销授权、已替代或过期的条目不得作为当前有效记忆注入；历史引用只能在明确的历史查看场景下使用。记录召回数量、投影大小与省略原因，不能把“检索不到”解释为事实不存在。

验收：不同用户/项目/会话不串数据；授权的用户/项目条目能跨任务复用；中文任务能召回关键记忆；无结果、低预算、过期和冲突场景行为明确，检索后整包准入仍合规。

#### L1.5 已实现：LangGraph、Worker 与恢复流程

宿主 `GraphMemoryRuntime` 组合 `MemoryService` 与 SQLite；CLI/Web 共用 `build_graph()`，开启才增加记忆节点，关闭不建库、不增加自动记忆流程。旧 checkpoint 没有绑定时不偷偷启用跨任务读取。实现位置如下：

| 接入点 | 当前行为 |
|---|---|
| 图初始化与 Router | 绑定服务端身份和检索政策；按任务读取必要的用户/项目偏好与约束 |
| Orchestrator | 读取相关项目事实、本会话目标与决策，形成自足子任务；simple 路径也必须覆盖 |
| `dispatch_workers()` | 根据目标、角色、直接依赖与权限生成 `retrieved_memories`，加入 `WorkerInputState`，不传完整主图或其他分支记忆 |
| Worker / Synthesis | 仅使用预算内记忆与直接上游；产物全文按授权引用读取，不自动灌入全部旧版本 |
| Reviewer | 只接收适用验收规则与必要历史引用，明确区分历史知识和本轮工具证据，不重复注入完整记忆库 |
| 任务后写入步骤 | 生成候选并调用受控写入流程，记录提交身份、结果和失败状态 |
| checkpoint 恢复 | 保存命中 ID/版本、输入快照和固化写入计划；重新校验有效性，失效项剔除/阻塞，不悄悄改用最新内容 |

`MultiAgentState` 保存 `memory_binding`、`retrieved_memories`、按任务/attempt 分组的 `worker_memories`、写入计划/结果与提示，不保存整库。`WorkerInputState` 只接收自己的投影和宿主核验绑定；绑定/数据库拒绝路径不进入模型 Prompt。产物引用只授予本 Worker 旧产物和直接上游的具体版本/哈希，不授予其他分支或文件读取权。实际使用副本写入 Worker 结果，计划快照原样保留用于审计。

每次模型节点/Worker 执行前重校验已有快照：过期、停用、删除、版本变化或宿主身份变化不能直接重放。可选项失效剔除，必要项失效 structured blocked；已有快照不重新检索替换，尚未调度的 Worker 在首次准备时才按当时有效记录取用。原独立预算与自动提取授权固化到 memory_policy，恢复不能扩大原额度或给旧任务补开授权，撤销当前自动提取开关则停止候选提交。库不可用但没有必要约束时清空可选投影并提示；必要约束/ID 则阻塞。工具/动作/错误预算不清零，时间沿用已有恢复刷新规则。关闭功能保留兼容节点名以处理旧 pending checkpoint，但新运行不执行记忆节点。

写入采用两个节点：`stage_memory_writes` 先将完整候选/预期前驱/确定性事件键与时间固化到 checkpoint，`commit_memory_writes` 再提交 SQLite。若 SQLite 成功后进程退出，恢复重放原计划复用提交，不新增版本；已有回执不等于重新激活已删除记忆。候选失败/额度不足不撤销已通过的用户交付，CLI/Web 单独展示记忆结果。没有新增模型调用，累计 Token 计费仍关闭；该协议只覆盖记忆提交，不修复外部工具副作用的 T1 缺口。

Python 子进程拒绝受保护库目录及其祖先改写、普通文件 IO、`sqlite3.connect` 和 SQLite URI，保护默认路径及宿主自定义库父目录；模型不能覆盖 injected 拒绝列表。关闭功能、旧 pending Worker 缺字段或可选读取失败时，宿主仍合并原有与当前配置的拒绝路径，不因降级暴露数据库。路径/审计不是恶意代码 OS 沙箱，原生扩展或内省绕过、现存硬链接等仍需 S1。Web 热加载也显式排除配置数据库及辅助文件。

若后续增加 Agent 主动检索/读取产物工具，必须通过已有 dispatcher、参数 schema、角色与任务白名单、预算和路径保护，不属于默认整库授权。

验收：simple/team 都可使用必要记忆；并行 Worker 不获得无关分支记忆；定向返工保留旧产物基线；恢复保持引用/版本和已消耗预算，库更新、删除与写入重放均有回归。

#### L1.6 记忆管理、纠错与删除

**已实现的入口**：Web 左下角“记忆”和标题栏“记忆管理”。身份由宿主配置提供；会话范围要求存在同绑定的会话，产物范围只接受该会话已持久化交付的精确版本/哈希。列表与历史均分页，候选/过期/停用/删除项可管理，但不会因此进入模型输入。

| 接口 | 用途 |
|---|---|
| `GET /api/memories/settings` | 是否启用、宿主身份、自动候选提取状态及保留告知；关闭时不打开数据库 |
| `GET /api/memories?scope=user` | 列表；支持 `scope/status/kind/conversation_id/limit/offset` |
| `GET /api/memories/{memory_id}` | 当前版本与历史；支持同作用域参数和版本翻页 |
| `POST /api/memories/{memory_id}/actions` | `confirm/correct/disable/enable/delete`，必须回显前驱引用与原正文、填写原因 |
| `GET /api/conversations/{id}/memory-impact` | 删除确认前预览私有/产物及关联用户/项目数量 |
| `DELETE /api/conversations/{id}?memory_cleanup=conversation` | 默认清理私有/产物；显式 `all_related` 才同时清理源自本会话的用户/项目条目 |

管理命令示例（`scope` 及会话参数放查询字符串；不接受客户端身份或 verified 声明）：

```json
{
  "action": "correct",
  "expected": {"memory_id": "实际条目ID", "version": 1, "content_hash": "实际64位SHA256"},
  "confirmed_content": "页面刚显示的完整原正文",
  "content": "完整新版正文",
  "reason": "用户明确调整输出偏好"
}
```

`content` 只用于纠错，其他操作不得携带；冲突返回 409，字段错误 400，存储不可用 503。候选确认仅支持不需要独立外部证据的类型；事实仍须可信宿主核验。偏好/规则纠错后可生效，事实/经验纠错后重新成为候选。启用只接受仍有验证状态且未过期的 disabled；deleted/superseded 不可复活。`revision_note` 保存公开修改原因，管理用户操作有来源定位，不保存私有推理。

**关联清理与恢复**：来源关联检查包含旧版本来源，避免纠错后遗漏已提升的知识；默认不清理无关跨作用域条目。最多扫描 10000 个当前头、一次清理 1000 条，超过时显式拒绝而非部分删除。SQLite 批量前驱校验及写入在同一事务中；之后删除 Redis 历史，失败可重试，**不承诺跨库原子提交**。删除最后一个单轮任务也清理其私有记忆。关闭长期记忆时不访问数据库，因此会话删除只处理任务/checkpoints；重新启用后，保留的私有行只能由宿主仓库管理，不会自动授予新会话。

后续模型输入重校验当前头、验证状态及时效；可选失效项剔除，必要项 blocked。Worker 工具循环逐调用校验，已经使用的记忆失效则停止，不重放含旧数据的历史。没有向量索引或独立长期记忆缓存；SQLite 当前头是有效性依据，历史 checkpoint 不是绕过此检查的入口。

**保留政策与限制**：本阶段只做逻辑删除，旧版本正文、用户原消息、旧摘要/交付、Redis checkpoint 和现有备份仍保留。没有自动物理擦除、到期清理、删除清单导入器或自动 ACL 设置；不得承诺所有副本即时删除。数据和在线备份放专用受保护目录，用户负责 OS 权限与保留期；备份只写新文件、恢复只写新路径。删除后备份保留 deleted 头；旧备份可能恢复 active，启用前必须人工对照现库删除记录、重新执行相关删除并核验，无法核验则不应作为在线库。普通文件复制不能替代 SQLite 在线备份。自动候选提取仍通过用户维护的 `MEMORY_AUTO_EXTRACT` 关闭；普通聊天不默认保存。Web 只限可信本地使用，namespace/跨站拒绝不是多人认证。

外部 embedding、物理清理与保留调度、自动删除清单协调、用户/项目注册认证为后续独立工作，不在本阶段冒充完成。

#### L1.7 分阶段交付与最终验收

实施顺序为 **L1.1～L1.6（代码及离线回归完成） → L1.7 真实全链路验收（由用户执行）**。具体步骤、任务原文、预期/实际记录表见 [MemoryAcceptance.md](MemoryAcceptance.md)，优先执行 A01～A06。产物级真实写入、精确中断窗口与物理删除须单独核验，未满足前置条件记为未验证。目前使用明确命令和可选确定性候选，不增加模型提取调用；认证注册、自动物理清理/备份删除协调及语义检索仍未完成。

最终验收至少包含：

1. 用户确认“默认中文、使用表格”，新任务可按授权偏好输出；明确改为自然语言后旧偏好不继续覆盖。
2. 同项目新会话能复用架构事实，其他项目/用户不能读取；会话私有城市列表不默认泄露到其他会话。
3. 20 轮以上对话与长中文/代码任务中，检索投影保持预算、来源与版本；容量不足时不破坏调用组或关键约束。
4. 过期天气不能当当前天气，人口数据保留年份/统计口径；仍按本轮 `required_tools` 取证。
5. 产物 v1/v2 引用可区分，来源与 SHA-256 可核对，读取权限不因记忆引用而扩大。
6. SQLite 重开与 Redis checkpoint 恢复分别可测；注入跨库提交中断、并行写入和节点重放，不丢版本、不重复提交、不重置资源。
7. 确认、冲突、纠错、删除、备份恢复、存储不可用与记忆功能关闭均有确定性结果；外部网页不能借记忆提取注入高优先级指令。

测试使用临时 SQLite 与 mock 外部服务，不读取 `.env`、不启动用户 Redis、不执行用户产物；实际 Redis 重启联调需用户授权。每步完成后更新 README 与 AGENTS，并记录真实运行的针对性/全量回归结果，不能沿用历史测试数量作为未来固定指标。

向量/混合语义检索属于后续可选增强：只有结构化与关键词检索的召回质量不足且有测试证据时再引入；向量只是候选索引，SQLite 仍决定权限、版本、时效与有效性，不自动上传历史数据。

### T1：工具调用可恢复，而不仅是图状态可恢复

目标：处理“工具已经执行，但 Worker 尚未写入 checkpoint 就中断”的窗口。当前 Redis checkpointer 无法独自解决这类外部副作用重复问题。

- 设计调用日志（invocation journal），以 `thread_id`、子任务、执行版本和逻辑操作 ID 标识一次操作；记录规范化参数哈希、工具版本、工作区作用域、状态、结果/产物引用及实际资源消耗。模型返回的 call ID 或相同参数本身都不足以证明是同一次业务操作。
- 执行前先记账/预留，执行后保存结果；相同运行的恢复复用已完成操作，定向返工或新轮次不得误命中旧结果。实时查询还需时效规则，不能把去重变成永久天气缓存。
- 显式区分未执行、执行中、已完成、失败和执行结果不确定；对“外部动作完成但日志未落盘”的写操作不能盲目重试，应依赖下游业务幂等键、状态核验或人工确认。
- 存储保持普通 Redis 3.2 / RESP2 兼容，设计并发占用、旧记录迁移和会话删除关联清理；复用结果不重复扣执行额度，已经消耗的额度也不能因恢复丢失。

验收：注入执行前、执行中、完成后但 checkpoint 前、结果落盘前等故障，覆盖并发重复请求、参数变化和有意返工。只有可证明已完成的相同逻辑操作才复用；不确定写操作进入明确待核验状态。新增调用日志本身不保证跨外部系统 exactly-once，文档必须保留这一边界。

### T2：在可恢复基础上增加可靠性

- 对 `retryable=true` 且操作语义允许的错误增加限次退避；参数错误、权限拒绝和不可重试业务错误不自动重试。每次实际调用都计入时间、工具次数与动作预算，不能在 dispatcher 内绕过资源控制。
- 将 `required_tools` 的“某工具调用成功过”提升为“每个要求的目标都有可追溯证据”；天气覆盖城市、行政区/国家、时间、单位与来源，同名地点必要时请求用户确认。
- 审批状态需写入 checkpoint，拒绝与取消都形成明确终态；UI 仅展示脱敏的工具名、目标范围、参数和风险。取消必须传到执行后端，不能只改变按钮状态。
- 增加 run / Worker / tool-call 三级关联记录，观察错误码、延迟、重试、配额和证据覆盖，不记录模型私有思维链或凭据。

验收：临时失败能在额度内恢复，永久错误不会循环；三城市只取得两个有效城市结果时不能自动成功；拒绝审批或取消后不产生新的副作用。自动恢复/重试必须建立在 T1 的去重与不确定状态处理之上。

### R1 / W1：后续推理、记忆与交互完善

- **推理**：增加 Plan Validator 检查明确可验证的 DAG、工具权限、输出契约和资源可达性；语义覆盖另行评估。限制 Re-plan 次数，复用仍有效的已完成分支，不能无限重新规划。
- **热加载**：退出前标记 interrupted，重连后明确恢复状态；有 T1 保障后再启用自动续跑。内存模式只能提示上下文丢失，不承诺跨重启恢复。
- **长会话 UI**：时间线虚拟化、导航过滤、滚动锚定、取消与历史分支；保持现有唯一滚动区和 URL 任务定位，并补浏览器端到端验证。
- **长期语义记忆**：按 L1 独立工作包推进，不与单次上下文溢出修复混用，也不能把全部工具日志直接向量化。

各开发包均应独立提交实现、针对性测试、全量回归结果和两份文档更新；新增测试后重新记录实际数量，不沿用历史测试数量或通过结论作为未来固定指标。

动态 Agent 注册应等专业 Agent 数量明显增加后再引入；A2A 应等 Agent 真正拆成跨团队、跨框架的独立服务后再采用，避免为当前单进程架构增加无效复杂度。

## 技术栈

- LangGraph / LangChain / langchain-openai
- Pydantic 结构化输出
- FastAPI / Uvicorn / Jinja2 / SSE
- Redis（可选 LangGraph checkpoints、pending writes 与 Web 历史）
- pandas / openpyxl / ddgs / BeautifulSoup
- pytest
