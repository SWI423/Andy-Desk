# Andy（Baymax_pro） 记忆层设计（ADR-001）

> 状态：待评审
> 日期：2026-11-25
> 定位：架构决策记录（ADR），记录"为什么这么设计"而不只是"设计成什么"
> 参考来源：用户整理的《记忆优化方案》（分层记忆 / 混合上下文组装 / 意图感知检索 / 写路径管理）

---

## 1. 背景：为什么需要专门设计记忆层

### 1.1 三个必须解决的问题

| 问题 | 现象 | Andy的真实症状 |
|---|---|---|
| 截断失忆 | 窗口满了丢信息，且丢掉的是关键信息无人察觉 | 早前说过"周三有软考"，十轮后Andy完全不提，提醒也没建 |
| O(n²) 成本 | 每轮把全部历史重发，成本随轮数线性翻倍累积 | 对话到 100 轮时首 token 延迟明显变长，免费额度也扛不住 |
| 注意力稀释 | 窗口塞满无关历史，关键信息被淹没（lost in the middle） | 用户说"今天好累"，Andy却答上一个话题的作业细节 |

### 1.2 一个关键认知

> **上下文窗口是内存，不是硬盘。**

模型有 200K 窗口不代表应该塞满 200K。塞得越满，注意力越稀释、延迟越高、成本越大。
记忆层的职责是：**每轮只把正确的少量信息送进窗口**，其余留在磁盘按需取用。

### 1.3 本设计的边界

- 只解决"应用层记忆"，不涉及模型底层（NSA 稀疏注意力、KV Cache 等属于厂商侧，我们只能利用不能实现）
- 面向单机、单用户、SQLite + 免费 API 的规模，不为未来百万级记忆做过度设计

---

## 2. 三层记忆模型

### 2.1 分层定义

| 层 | 定义 | 存储位置 | 写入时机 | 规模量级 |
|---|---|---|---|---|
| **L1 工作记忆** | 最近若干轮对话原文 | 进程内存（list） | 每轮追加，超长滚动丢弃 | 6-8 轮 |
| **L2 情景记忆** | 带时间戳的事件流水（对话、任务变更、健康事件） | `chat_events` 表，append-only | 每轮/每次事件 | 无限增长 |
| **L3 语义记忆** | 从事件蒸馏出的稳定事实（身份、偏好、基线、决策） | `semantic_facts` 表 + `user_profile` | 检测到事实性陈述时 | 数十~数百条 |

### 2.2 与本项目三大领域（学习/工作/生活）的对应

L3 语义记忆按领域分区，这是本次定位调整后新增的设计：

| 领域 | 典型稳定事实 | 用途 |
|---|---|---|
| study | 考试日期、课程基线、学习时段偏好 | 排学习任务时不与现实冲突 |
| work | 实习/项目截止、汇报习惯 | 拆解工作指令时引用上下文 |
| life | 作息、饮食偏好、家人事项、健康基线 | 生活类提醒与健康建议 |

> 为什么要分区：避免"生活类闲聊"把工作类关键事实挤掉，检索时可按领域过滤，召回更准、注入更省。

### 2.3 关键设计决策：先蒸馏事实，再允许摘要

这是本设计相对参考方案补充的最重要一条。

**参考方案的问题**：它把"滚动摘要"和"蒸馏语义记忆"并列，没规定先后顺序。
**我们的风险**：摘要是**有损压缩**。一旦"周三上午 9 点软考"被压成"用户提过一些考试的事"，后续排任务、建提醒全部失真，而且**无法恢复**。

**决策：写路径顺序固定为**

```
对话事件 → 规则检测/事实抽取 → 先写 L3（结构化、无损）
                             → 再允许该段被摘要压缩
```

**理由**：鸡毛蒜皮的细节可以丢，可执行的事实不能丢。事实落在结构化表里，压缩只影响"叙述性内容"。

### 2.4 关键设计决策：稳定前缀前置

把几乎不变的内容放在 prompt 最前面，易变的放最后：

```
① SYSTEM_PROMPT（人设 + 铁律）        ← 永不变
② 工具 schema（function calling 定义） ← 随版本变，频率低
③ FEWSHOT 示例                        ← 随版本变，频率低
④ 用户画像 / 三线领域基线              ← 很少变
⑤ 今日与近 3 天状态（任务/提醒/健康趋势） ← 每天变 1-2 次
⑥ 检索召回片段                        ← 每轮变
⑦ 最近 6-8 轮对话原文                  ← 每轮变
⑧ 用户当前输入                        ← 每轮变
```

**理由**：
1. 部分商用 API 提供前缀/上下文缓存（是否支持、是否计费需按厂商文档核对）。稳定前缀放在最前，若服务端支持缓存则可直接命中，降低延迟与成本。
2. 即使服务端不支持缓存，固定顺序也能减少我们自己的组装抖动，便于 A/B 对比和排错。
3. 这是零成本优化，没有理由不做。

> 待核实项：智谱 GLM 系列当前是否提供 prefix cache 及其计费方式。落地前查官方文档，不在文档里假设。

---

## 3. 读路径：每轮上下文如何组装

### 3.1 组装流程

```
用户输入
  ↓
① 意图判定（规则优先，不做每轮 LLM 调用）
   - 含任务/提醒词 → 拉起任务与提醒快照
   - 含健康词 → 拉起近 3 天健康趋势 + 医疗知识检索
   - 含"上次/之前/我们说好" → 强制走记忆检索
  ↓
② 取 L3 事实（按领域过滤 + confidence ≥ 0.6）
  ↓
③ 取今日状态（今日任务、今日提醒、未完成任务数）
  ↓
④ 检索 L2 相关事件（向量 top_k=3，阈值 0.5 以下丢弃）
  ↓
⑤ 取 L1 最近 6-8 轮原文
  ↓
⑥ 按 token 预算裁剪 → 拼成 prompt → 调模型
```

### 3.2 Token 预算（记忆部分上限 2500 token）

| 段 | 预算 | 超限处理 |
|---|---|---|
| ④ 画像/三线基线 | 150 | 只放 confidence 最高的若干条 |
| ⑤ 今日状态 | 200 | 只放汇总与计数，不放明细 |
| ⑥ 检索召回 | 600 | top_k=3，每条截断至 200 字 |
| ⑦ 最近原文 | 1500 | 从最旧开始丢弃 |

**预算必须用代码硬约束，不能指望模型自己"少说点"**——这与 V1 里"时间硬拦截"是同一个教训：提示词管不住的，代码要管住。

### 3.3 意图判定为什么用规则而不是 LLM

- 每轮多一次模型调用 → 延迟 +300ms 以上，桌面助手体验直接崩
- 规则（关键词 + 轻量分类）能覆盖大多数显式意图
- 真正模糊时才回退给模型判断（可选，默认关闭）

---

## 4. 写路径：什么值得存

### 4.1 每轮都写（零判断，成本极低）

```sql
INSERT INTO chat_events(session_id, role, content, ts, importance)
```

importance 由规则打分（0~1），不调用模型。

### 4.2 蒸馏到 L3 的触发条件（规则优先）

| 触发类型 | 示例输入 | 写入 predicate |
|---|---|---|
| 身份 | "我叫 Wendy" | `name` |
| 健康基线 | "我对青霉素过敏" | `allergy` |
| 慢性问题 | "我有偏头痛史" | `chronic` |
| 时间锚点 | "周三上午 9 点软考" | `schedule_anchor` |
| 偏好 | "我不喜欢被说教" | `preference` |
| 领域基线 | "我一般晚上 8 点学英语" | `habit_study` |
| 显式要求 | "帮我记住……" | 直接写入，不再判断 |

**为什么先规则后模型**：规则覆盖大部分高价值事实，成本 0、延迟 0、可解释。
**什么时候才用模型抽**：规则漏了的模糊表达（如"我这阵子老是熬夜到很晚"）。实现上做成**可选的批量补抽**（会话结束时跑一次），不做每轮调用。

### 4.3 去重与更新

```
新事实 → 算 embedding → 与同 predicate 事实比对
  相似度 > 0.9 且主体一致 → 更新旧记录（content、confidence、updated_at）
  否则                    → 新增
```

例外：`schedule_anchor` 允许多条并存（多个考试/截止日期），按时间排序后注入。

### 4.4 摘要压缩

| 项 | 规则 |
|---|---|
| 触发 | 会话事件超过 40 条，或会话结束，或用户显式"总结一下" |
| 保留 | 决策、承诺、健康事件、情绪转折、未完成事项 |
| 丢弃 | 寒暄、重复确认、工具原始返回 |
| 防重复 | 摘要记录 `covered_until_event_id`，只压未覆盖区间 |
| 落库 | `session_summary` 表 |

---

## 5. 检索：三阶段演进（不一步到位）

| 阶段 | 方案 | 依赖 | 触发上线条件 |
|---|---|---|---|
| **阶段1（MVP）** | 纯向量检索 top_k=3，阈值 0.5 | numpy + embedding-3 | 随 M5 一起做 |
| **阶段2** | 向量 + 关键词混合，**SQLite FTS5** 提供关键词侧，RRF 融合 | 零新增依赖（FTS5 内置） | 记忆条数 > 1000 或出现"明明存过却召不回" |
| **阶段3** | 启发式重排：相似度 × 时间衰减 × 重要度 × 领域匹配 | 纯本地计算 | 召回量升到 10 条以上 |

**为什么阶段2 用 FTS5 而不是引入 BM25 库/Elasticsearch**：FTS5 是 SQLite 内置全文索引，建一行虚拟表即可，自带 BM25 排序，单机项目性价比最高。

**为什么先不做 LLM 重排**：多一次模型调用，延迟 +200~400ms；top_k=3 的候选本来少，重排收益有限。等候选数量上去了再考虑。

### 5.1 RRF 融合公式（阶段2 用）

```
score(d) = Σ_i  1 / (k + rank_i(d))     k 取 60（常用经验值）
```

把向量排名与关键词排名融合，避免单一检索器把好结果排到后面。

---

## 6. 数据模型（DDL 草案）

```sql
-- ===== L2 情景记忆 =====
CREATE TABLE IF NOT EXISTS chat_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,              -- user / assistant / tool / system
    content     TEXT    NOT NULL,
    ts          TEXT    NOT NULL,              -- ISO8601，便于按日统计
    importance  REAL    NOT NULL DEFAULT 0.0,   -- 0~1，规则打分
    embedding   BLOB,                          -- numpy float32 tobytes
    summarized  INTEGER NOT NULL DEFAULT 0     -- 是否已被摘要覆盖
);
CREATE INDEX IF NOT EXISTS idx_events_session_ts ON chat_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_summarized ON chat_events(summarized);

-- 关键词索引（阶段2 启用）
CREATE VIRTUAL TABLE IF NOT EXISTS chat_events_fts
    USING fts5(content, content='chat_events', content_rowid='id');

-- ===== L3 语义记忆 =====
CREATE TABLE IF NOT EXISTS semantic_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject       TEXT NOT NULL,               -- 通常为 user
    predicate     TEXT NOT NULL,               -- name/allergy/chronic/schedule_anchor/preference/habit_study/habit_work/habit_life
    object        TEXT NOT NULL,
    domain        TEXT,                        -- study / work / life / general
    confidence    REAL NOT NULL DEFAULT 0.8,
    source_event_id INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_pred ON semantic_facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_domain ON semantic_facts(domain);

-- ===== 摘要层 =====
CREATE TABLE IF NOT EXISTS session_summary (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    summary       TEXT NOT NULL,
    covered_until_event_id INTEGER,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summary_session ON session_summary(session_id);

-- ===== 任务与提醒（新定位核心，与记忆层共享时间轴）=====
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    domain      TEXT NOT NULL,                 -- study / work / life
    status      TEXT NOT NULL DEFAULT 'todo',   -- todo/doing/done/cancelled
    priority    INTEGER NOT NULL DEFAULT 2,     -- 1高 2中 3低
    due_at      TEXT,
    progress    INTEGER NOT NULL DEFAULT 0,     -- 0~100
    parent_id   INTEGER,                        -- 子任务
    source      TEXT NOT NULL DEFAULT 'manual', -- manual/ai/mcp
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_events (        -- 进度轨迹，append-only
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    event_type TEXT NOT NULL,                   -- created/progress/status/postponed/note
    payload    TEXT,
    ts         TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    rule_type     TEXT NOT NULL,                -- once/daily/weekly/cron
    rule_expr     TEXT,                         -- "07:30" / "0 9 * * 1" 等
    next_fire_at  TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    missed_count  INTEGER NOT NULL DEFAULT 0,   -- 关机期间错过的次数
    task_id       INTEGER,                      -- 关联任务（可空）
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminder_next ON reminders(next_fire_at, enabled);
```

**关键设计点说明**

| 设计 | 原因 |
|---|---|
| `chat_events` append-only | 出问题可回溯；摘要与删除都是派生操作，不改原始流水 |
| `embedding` 存 BLOB | 比存文本省空间、读取快；numpy 直接 `frombuffer` |
| `covered_until_event_id` | 防止同一段被反复压缩，摘要不会互相覆盖 |
| `domain` 字段 | 支持按学习/工作/生活过滤检索，减少注入量 |
| `task_events` 与 `chat_events` 分离 | 任务变更需要结构化统计（如"这周推迟了几次"），和自由对话文本不同构 |
| `missed_count` | 电脑关机错过的提醒要能补发并留痕，而不是静默丢失 |
| `next_fire_at` 预先算好 | 调度器每次只查"最近要触发的"，避免全表扫描 |

---

## 7. 明确不做（防过度设计）

| 不做 | 原因 |
|---|---|
| TextGrad 等自动 prompt 优化 | 个人项目无标注数据，无法形成闭环；手动迭代 + badcase 记录更实际 |
| Chroma / FAISS / Milvus | 现有规模几百~几千条，numpy 余弦相似度毫秒级，引入向量库只增复杂度 |
| 每轮 LLM 打分 + LLM 重排 | 两次额外模型调用，延迟翻倍，小规模收益不明显 |
| 全量历史塞入上下文 | 稀释注意力、增加成本，实测通常劣于裁剪方案 |
| 多用户记忆隔离 | 本期单用户，多用户是 V2 以后的事 |

---

## 8. 落地节奏与验收指标

### 8.1 任务拆分（对应 WBS 的 M5）

| 编号 | 内容 | 验收标准 |
|---|---|---|
| M5a | 建表 + 每轮写 `chat_events` | 连续对话 10 轮后表内有 ≥10 条记录，`ts` 顺序正确，重启后仍在 |
| M5b | 规则蒸馏 + 去重写 `semantic_facts` | 说"我青霉素过敏"→ 产生 `allergy` 记录；重复说三次仍只有一条（updated_at 变化） |
| M5c | 上下文组装（④⑤⑥⑦）+ token 预算 | 组装后记忆部分 < 2500 token；画像、今日状态、召回片段三者都在最终 prompt 中 |
| M5d | 摘要压缩 + `covered_until_event_id` | 触发一次摘要后，`summarized` 正确置位，重复触发不会二次压缩同一区间 |

### 8.2 可量化验收指标

| 指标 | 目标 | 测量方式 |
|---|---|---|
| 记忆组装耗时 | < 50ms | 组装函数前后打点 |
| 每轮记忆 token | < 2500 | 组装结果长度统计 |
| 关键事实召回率 | ≥ 90% | 抽 10 条已存事实，用相关问题召回，人工判定 |
| 画像重复率 | 0 条 | 同 predicate 下相似度 >0.9 的记录数 |
| 摘要后事实丢失率 | 0 条 | 摘要前后比对 `semantic_facts` 数量 |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 免费模型对长 prompt 注意力差 | 召回内容被忽略 | 严格 token 预算 + 关键信息放段落末尾（近因效应） |
| embedding 相似度阈值不适配中文 | 召回噪声高 | 用真实语料校准阈值（0.4~0.6 间实测），不照搬英文经验值 |
| 事实抽取规则误判 | 画像被污染 | `confidence` 分级 + 低置信不注入 + 提供人工删除入口 |
| 摘要模型跑偏 | 丢失决策 | 先蒸馏事实再摘要；摘要 prompt 限定"只保留决策/承诺/未完成事项" |
| SQLite 并发写 | 桌面多线程写冲突 | 单写连接 + WAL 模式；所有写走同一个封装函数 |

---

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-25 | 初版。基于用户《记忆优化方案》重构，补充"先蒸馏后摘要""稳定前缀前置""按领域分区"三条决策；新增任务/提醒表以配合产品定位调整 |
| 2026-11-25 | 追加 §11 落地记录：MVP 实际实现与原方案的偏差及原因（ADR-003~007） |

---

## 11. 落地实现记录（MVP 实际实现）

### 11.1 文件映射

| 设计项 | 落地文件 | 说明 |
|---|---|---|
| 建表 / 连接 / 事务 | `core/db.py` | WAL + 单连接 + 全局写锁；`init_db()` 幂等 |
| L2 事件流水 | `core/memory.py::log_event` | 用户/AI 每条都落，append-only |
| L3 事实蒸馏 | `core/memory.py::extract_facts` | 规则抽取 + 同 predicate 去重 |
| 上下文组装 | `core/memory.py::build_context` | facts > overview > recall，按预算裁剪 |
| 任务层 | `core/tasks.py` | 三线、子任务、进度、task_events 轨迹、今日概览 |
| 提醒层 | `core/scheduler.py` | once/daily/weekly + 启动补发 + missed_count |
| 工具层 | `core/tools.py` | 8 个工具的 schema + 注册表 + 兜底错误收敛 |
| 对话核心 | `core/brain.py` | 稳定前缀 → 动态上下文 → 历史 → 当前输入；工具循环 ≤4 轮 |
| 桌面层 | `desktop/main_window.py` | 三线看板 + 提醒页 + 流式对话 + 提醒浮层 |
| 用户档案 | `core/profile.py` | 首位用户建档（幂等）；名字落 `user_profile` + 一条 `name` 事实 |

### 11.2 与原方案的偏差（ADR-003 ~ ADR-007）

**ADR-003：MVP 检索用"字符重合度打分"，不逐条调 embedding。**
- 原方案：向量 top_k=3。
- 实际：`memory.search_events()` 对最近 300 条事件做中文字符/英文词重合度打分，阈值 0.35，取 top 3，并排除已注入的最近窗口。
- 理由：每条消息都调 embedding 会多一次 API 往返（延迟、额度），而当前语料量小、问题多为短期事务；规则检索零成本、可解释。
- 升级触发条件：事实召回率 <90%，或事件量 >2000 条 → 接 `embedding-3` + 本地缓存 + RRF 融合（原方案第二阶段）。
- 代价：跨句同义改写（"交报告" vs "提交文档"）召回不到。

**ADR-004：调度用"30 秒心跳轮询 reminders 表"，不用每条提醒一个 APScheduler job。**
- 理由：单一事实来源。若 jobstore 与业务表各存一份状态，重启后容易出现"表里有、job 没有"或重复触发。现在 APScheduler 只负责心跳，下次触发时间由 `next_fire_at` 一个字段决定。
- 补发：启动时把 `next_fire_at` 已过的提醒合并成一条"错过提醒"通知，最多列 5 条，避免刷屏。
- 代价：最小触发精度 30 秒；程序关闭时不响（已在提醒页写明）。

**ADR-005：事实抽取先用规则，不做每轮 LLM 抽取。**
- 规则表在 `memory.FACT_RULES`（称呼、过敏、慢性病、考试/截止锚点、偏好、作息）。
- 理由：每轮多一次 LLM 调用成本高；规则误抽可预测、可测试。
- 升级：规则漏抽时，用后台异步任务补抽（不阻塞当轮对话），并在 `confidence` 上区分。

**ADR-006：对话直连 `openai` SDK，不用 LangChain。**
- V1 用 `langchain_openai.ChatOpenAI` + LCEL；V2 去掉这层。
- 理由：流式 + 工具调用的增量拼接、错误收敛、超时都需要自己可控；少一层框架，代码可读、可教学。
- 兼容：智谱接口与 OpenAI 兼容，`base_url` 指向 `open.bigmodel.cn/api/paas/v4`。

**ADR-007：工具层用"schema 列表 + 注册表"，作为扩展性主入口。**
- 新增一个能力 = `TOOLS` 加一条 schema + `TOOL_REGISTRY` 加一个函数，`brain` 不改。
- 未做 MCP / FastAPI 适配：按 PRD 的 ADR-002，本期不引入 HTTP 服务；后续把工具实现替换成"HTTP 调用"或"MCP client 调用"即可，schema 与对话层不变。

### 11.3 已落地的约束

- 时间硬拦截在调模型之前：说了"提醒"但没有任何时间词，直接反问，不消耗 token。
- 当前时间每轮注入，模型必须基于它换算相对日期。
- 所有工具异常收敛为 `{"ok": false, "error": ...}` 回给模型，不打断整轮。
- token 预算 2500，实测组装结果约 35~80 token（MVP 语料少，离上限很远）。
- 所有路径基于 `BASE_DIR`，项目目录搬迁不需要改代码。
