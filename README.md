# Andy（Baymax_pro）

个人管理助手，管三件事：**学习、工作、生活**。
桌面窗口 + 对话 + function calling，能力按"注册表"扩展。

---

## 现在能做什么

| 能力 | 怎么用 | 底层 |
|---|---|---|
| 记任务 | "把软考冲刺记到学习里，12 月 5 号截止" | `task_create` 工具 → `tasks` 表 |
| 拆任务 | "帮我拆一下挑战杯答辩准备" | `task_breakdown` → 子任务 |
| 改进度 | "算法题那项做到一半了" | `task_list` 找 id → `task_update` |
| 定时提醒 | "每天 19:30 提醒弟弟写作业" | `reminder_create` → 心跳调度器 |
| 问今天 | "今天还有什么要紧的" | `overview_today` + 三线看板 |
| 记长期事实 | "以后十点后别提醒我学习" | `memory_save` → `semantic_facts` |
| 换说法找往事 | "我那个证的事怎么样了" | embedding-3 语义检索 + 关键词，RRF 融合 |
| 手动管理 | 左侧看板：新建/开始/完成/编辑/删除；提醒页：新建/停用/测试 | PyQt6 |

时间规则：说"提醒"但没给时间时，Andy 会先反问，不会瞎猜。

---

## 当前用户

| 项 | 值 |
|---|---|
| 用户 | **Wen**（首位用户，2026-11-25 建档） |
| 助手 | **Andy** |
| 档案内容 | 名字、启用日期、助手名、备注；同时写一条语义事实 `name=Wen`，每轮注入 |
| 改名 / 查看 | `python scripts/init_user.py --name 新名字`、`python scripts/init_user.py --show` |

档案是幂等的：重复启动不会覆盖已有资料。

---

## 快速开始

```powershell
# 1. 依赖（已装在 conda env: baymax）
D:\prgramf\Anaconda3\envs\baymax\python.exe -m pip install -r requirements.txt

# 2. 密钥：复制 .env.example 为 .env，填 ZHIPU_API_KEY
#    本机 .env 已配好（被 .gitignore 排除，不会入库）

# 3. 启动
cd D:\my_project\PythonProject\Baymax_pro
D:\prgramf\Anaconda3\envs\baymax\python.exe main.py
```

其他入口：

```powershell
python main.py --cli        # 终端对话
python main.py --selftest   # 窗口自检，3.5 秒后自动退出，截图存 logs/
python scripts/smoke_core.py            # 核心链路（独立库 data/smoke_<时间>）
python scripts/smoke_core.py --no-llm   # 只测本地逻辑，不调模型、不联网
python scripts/smoke_gui.py             # GUI 全流程 + 截图（独立库）
python scripts/check_retrieval.py       # 检索对照：关键词 vs 向量 vs 混合（独立库）
python scripts/backfill_embeddings.py --status   # 查看向量索引状态
python -m pytest tests -q               # 23 项单测
```

---

## 界面

- **左：三线看板** — 学习 / 工作 / 生活 / 提醒 四个页签；表格列：任务、截止、进度、状态。
  双击行可编辑；优先级"高"显示红色，逾期显示橙色。
- **右：对话** — 流式输出；快捷按钮：今日概览 / 新建任务 / 新建提醒。
- **顶部状态条** — 今日到期、逾期、进行中、提醒数。
- **提醒浮层** — 到点在右下角弹出，同时写入对话流。

---

## 目录

```
Baymax_pro/
├─ main.py              入口（GUI / CLI / 自检）
├─ config.py            配置与路径（全部基于 BASE_DIR）
├─ console.py           日志双写（终端 + logs/）
├─ prompts.py           人设 + 处理范式（稳定前缀）
├─ core/
│  ├─ db.py             连接 / 建表 / 通用查询（含 event_vectors）
│  ├─ embedding.py      embedding-3 调用、余弦、向量编解码、查询缓存
│  ├─ memory.py         事件流水、事实蒸馏、上下文组装、混合检索、补索引
│  ├─ tasks.py          三线任务 CRUD、子任务、今日概览
│  ├─ scheduler.py      提醒规则、心跳调度、启动补发
│  ├─ tools.py          8 个工具的 schema + 注册表
│  ├─ brain.py          流式对话 + 工具循环 + 时间硬拦截
│  └─ rag.py            （预留）
├─ desktop/
│  ├─ main_window.py    三线看板 + 对话 + 提醒
│  └─ theme.py          浅色主题
├─ docs/                PRD / WBS / MEMORY_DESIGN / DECISIONS / PROGRESS
├─ scripts/             smoke_core.py / smoke_gui.py / check_retrieval.py / backfill_embeddings.py
├─ tests/               pytest
└─ data/                SQLite（不入库）
```

---

## 架构（一句话版）

```
PyQt6 窗口
   ↓ 用户输入
brain：时间拦截 → 上下文组装 → 模型（流式）→ 工具循环
   ↓                              ↓
memory（事件/事实/向量检索）      tools 注册表
   ↓                              ↓
SQLite（tasks / reminders / chat_events / semantic_facts / event_vectors）
   ↑
scheduler（30s 心跳 → 到点回调 → 窗口浮层）
```

记忆层细节见 `docs/MEMORY_DESIGN.md`（ADR-001 + 落地记录 ADR-004~008）。
决策的详细说明（场景、两种做法、函数卡片、实例数据、升级位置）见 `docs/DECISIONS.md`。

---

## 怎么加一个新能力（扩展性）

以"查天气"为例，只改一个文件：

1. `core/tools.py` 的 `TOOLS` 里加 schema：
   ```python
   {"type": "function", "function": {
       "name": "weather_query", "description": "查某地天气",
       "parameters": {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}}}
   ```
2. 写实现函数 `_tool_weather_query(city)`，返回 dict。
3. `TOOL_REGISTRY` 里登记 `"weather_query": _tool_weather_query`。

对话层、窗口层都不用动。要接你那个 FastAPI 项目，把实现函数换成 `requests.get(...)` 即可（PRD 里的 S2 路径）。

---

## 已知限制（试用前先看）

- **程序关着不会提醒**：提醒靠进程内心跳；要做到关机也提醒，需要系统计划任务或常驻服务。
- **触发精度 30 秒**，且启动时只补发一次汇总（"错过"与"刚到点"合并成一条），不逐条补。
- **检索依赖 embedding 接口**：接口不可用时自动退回关键词，换说法会漏；阈值 0.10 是本地小语料标定值，语料变大需复核（`scripts/check_retrieval.py`）。
- **检索不做时间衰减/rerank**：三个月前和昨天权重一样，前 3 条可能同题挤占。
- **不做医疗诊断**，身体不适只给常识建议。
- **不自动发微信/邮件**：只生成内容，发送由你本人确认。
- **单用户、无登录**；数据在本地 `data/baymax.db`。
- 对话依赖网络与智谱 API；断网时本地看板/提醒仍可用，对话会提示失败原因。

---

## 验证记录（2026-11-25）

| 检查 | 方式 | 结果 |
|---|---|---|
| 单测 23 项 | `pytest tests -q` | 全部通过（17 项原有 + 6 项向量） |
| 向量索引 | `scripts/check_retrieval.py` | 8 条语料全部建索引；换说法"证的事"由向量召回 |
| 检索降级 | 单测 `test_fallback_when_embedding_down` | 向量不可用时仍按关键词召回，不报错 |
| 数据层打通 | `smoke_core.py --no-llm` | 建任务/更新/概览/拆解/提醒/记忆/工具直调全部正常 |
| 模型 + 工具 | `smoke_core.py`（真调 API） | `overview_today`、`reminder_create` 真实触发 |
| 时间拦截 | 同一脚本 | "提醒我交周报" → 反问时间，不调模型 |
| 多轮工具（先查再改） | `scripts/check_multi_tool.py` | "模电实验报告我做完了" → 按标题定位并置 done |
| GUI 全流程 | `smoke_gui.py` | 建任务 → 发问 → 回 `task_list` 结果 → 提醒浮层 → 截图 |
| 窗口自检 | `main.py --selftest` | 7 模块健康检查 OK，窗口正常退出 |

未做：大规模回归、并发压测、长时间挂机提醒验证（按约定留到 Demo 验收后统一测试）。
