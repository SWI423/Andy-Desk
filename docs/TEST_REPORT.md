# Andy 功能测试与修复报告

> 测试日期：2026-11-25 · 被测版本：MVP + 本轮质量加固（提交见 `git log -1`）
> 环境：Windows 11 / conda `baymax` / Python 3.10.21；对话 `glm-4-flash`；向量 `embedding-3`（2048 维）
> 原则：每个结论都有脚本输出或数据库状态做证据；测试全部使用独立库，不污染正式 `data/baymax.db`

---

## 一、一句话结论

主链路已经可用：**离线功能审计 50/50、单元测试 95 项全过、真实模型任务评测 19/19、11 轮多轮对话 4/4、GUI 冒烟 PASS、11 项自检（配好 `.env` 与 ASR 模型时全 OK）**。本轮另加 MCP 7 项、语音与语种解绑 15 项、基本问答校准 16 项、语音输入 ASR 7 项、运行时事件协议 4 项专项测试，均通过。

本轮共修掉 **23 个真实缺陷**，其中 4 个属于“用户以为办好了、系统其实没办”的严重问题；记忆检索从 **R@1 70% 提升到 100%**，top3 噪声从 **0.70 降到 0.10**。修复过程中自己引入过 2 个回归（提醒查询被误判、建任务被强制调用导致截止时间丢失），都已在交付前修掉并纳入回归用例。另完成 **stdio MCP 客户端框架**、**TTS 语音播报**，以及**语种与音色分离设置**，验证见第三节 F。

---

## 二、测了什么、怎么测

| 测试 | 脚本 | 覆盖内容 | 结果 |
|---|---|---|---|
| 离线功能审计 | `scripts/functional_audit.py` | 8 张表与级联删除、任务增删改查/排序/拆解、事实蒸馏、上下文预算、工具契约、调度补发、时间拦截、JSON 解析、tool_call id 对齐，共 50 项 | **50/50 PASS** |
| 单元测试 | `pytest tests -q` | 原有 23 项 + 本轮 23 项回归 + MCP 7 项 + 语音与语种解绑 15 项 + 基本问答 16 项 + ASR 7 项 + 运行时事件 4 项 | **95 passed** |
| 记忆评测 | `scripts/memory_eval.py` | 24 条带时间戳语料 + 8 条噪声 + 10 个换说法问句；对比关键词/向量/混合三路，统计 R@1、R@3、MRR、噪声、耗时 | **混合 R@1 100%、R@3 100%、MRR 1.00、噪声 0.10** |
| 记忆扩展复核 | `scripts/eval_memory.py --variant hybrid` | 另一套 39 条语料 + 15 个概括式/指代式问句，防止只在 10 问句上“刷分” | **hit@1 100%、hit@3 100%、MRR 1.00** |
| 任务评测 | `scripts/task_eval.py` | 18 个真实模型场景、19 个断言：建任务/相对日期/查询/完成/开始/进度/拆解/提醒/每日提醒/重复/别名/偏好/身份/过去时间/一句话两件事 | **19/19 PASS** |
| 多轮对话流程 | `scripts/chat_flow.py` | 11 轮连续对话：建任务→拆解→开始→完成→记过敏→设提醒→闲聊多轮→跨轮问过敏→跨轮问答辩进展 | **4/4 PASS**，第 11 轮能查出准确状态 |
| GUI 冒烟 | `scripts/smoke_gui.py` | 建任务→发消息→模型调工具→提醒浮层→截图；聊天快照改用 `ChatView.plain_text()` | **PASS**，新版左右分列气泡截图 `logs/gui_flow.png` |
| 运行时事件 | `tests/test_agent_runtime.py` | 事件顺序、turn_id、轨迹写入、取消标记、brain 工具事件 | **4/4 PASS** |
| 窗口自检 | `main.py --selftest` | 对话核心/记忆/任务/调度/工具/数据库/向量层/MCP/语音/语音输入 10 个模块 + 模型配置，共 11 项 | **配好 `.env` 与 ASR 模型时全 OK**；开发副本缺 key 时仅“对话核心”FAIL，截图 `logs/selftest_window.png` |
| MCP 链路 | `tests/test_mcp.py` + `scripts/mcp_demo_server.py` | 启动 demo server、握手、工具发现、只读调用、写入拒绝/确认、坏命令隔离、无配置 | **7/7 PASS** |
| 语音与语种 | `tests/test_voice.py` + 真实合成 | 清洗、截断、音色路由、语种与音色独立、旧值迁移、跨语种提示、整段一次合成、队列、edge 失败降级 SAPI、健康检查；6 个选定音色真实合成 | **15/15 PASS**；6 个音色全部合成成功（`logs/voice_check/`） |
| 语种实跑 | `brain.ask_baymax("你是谁？")` 切档 | `en-GB` / `en-HK` / `en-US` / `en-CA` / `zh-TW`；中文提问 | **PASS**：英文档均返回英文，`zh-TW` 返回繁體中文；`en-GB` 回复 “I am Wen's personal assistant, Andy.” |
| 基本问答校准 | `tests/test_basic_answers.py` + `ask_baymax` 实跑 | 中英星期/日期/时间本地确定性回答；任务句不误拦截；中英错误星期/时间按本机时钟纠正；非今日日期不改；条件句不误改 | **16/16 PASS**；`今天星期几`、`What day is it?` 均不调模型 |
| 并发写 | 8 线程同时建任务 | SQLite 单写锁是否丢数据/报错 | **0 错误、8 条全在、无重复** |

说明：

- 审计与评测各自新建 `data/audit_*`、`data/memeval_*`、`data/taskeval_*`、`data/chatflow_*`，跑完即弃；`data/` 已 gitignore。
- 记忆评测会真实调用 embedding（约 30 条/轮），任务评测与多轮流程会真实调用模型，不是 mock。

---

## 三、修了哪些问题

### A. 会造成“假办好”的问题（最高优先级）

| # | 现象 | 根因 | 修法 | 验证 |
|---|---|---|---|---|
| A1 | 多轮对话里说“明天下午两点提醒我”，Andy 回“已设置”，但提醒表是空的；说“做完了”，任务状态没变；有时只说“每天早上 8:30 叫你”，同样没写进库 | `glm-4-flash` 在多轮历史下会跳过工具直接“报喜/许诺”，`tool_choice` 也不一定被服务端强制执行 | 三重保险：①动作意图明确时首轮锁定对应工具；②回复声称“已设置/已记下/已标记”，或出现“每天叫你/我会提醒你”这类承诺，且本轮没有成功写操作时，**丢掉历史、只带当前这句重试**；提醒意图锁了工具却零调用也直接重试；③重试仍不调工具，就如实说“没写进系统，请再说一次”，绝不骗用户 | `test_fake_claim_is_caught_and_retried`（首轮为“每天 8:30 叫你”承诺）、`test_fake_claim_without_tool_is_not_believed`；11 轮流程第 6 轮真实落库 |
| A2 | 长期记忆怎么都写不进去 | `memory_save` 的 schema 字段叫 `object`，Python 函数参数写成 `obj`，模型按 schema 传参直接 TypeError | 函数改为接收 `object`，保留 `obj` 兼容 | `test_memory_save_tool_object_field` |
| A3 | 截止时间写“下周”这种解析不了的值，任务照建、模型还说“记好了” | `create_task` 解析失败静默变 `None` | 返回 `due_warning`，提示词要求模型请用户补日期 | `test_task_due_warning`、任务评测场景 10 |
| A4 | 同一件事说两遍，任务表出现两条 | 建任务没有重复识别 | 同领域同标题的未完成任务直接复用，返回 `duplicate=True`，提示词要求说明“之前记过” | `test_task_duplicate_not_created`、任务评测场景 12 |

### B. 记忆机制（本轮重点）

| # | 现象 | 根因 | 修法 | 效果 |
|---|---|---|---|---|
| B1 | “我对花生过敏”抽不出“花生” | 事实规则只取关键词**后面**的文字 | 事实规则改成四元组，增加 `before` 方向；剥离“我/对/吃了/记一下/是不是”等口语前缀 | 过敏原稳定入库 |
| B2 | 问“我是不是对花生过敏”反而写出一条新事实，内容还是“是不是对花生” | 问句被当成陈述句抽取 | 问句（是不是/是否/吗/？）一律不写事实 | `test_question_not_written_as_fact` |
| B3 | “记一下，我对花生过敏”记成“记一下” | 去标点时取了第一段，没取关键词所在那段 | 按方向取最近一段，再逐层剥前缀 | `test_fact_extract_after_filler` |
| B4 | 助理复述用户的话会挤掉用户原话 | 检索池混入 `assistant` 事件 | 只索引/召回 `user` + `system`，助理回声不参与 | `test_assistant_echo_not_recalled`；回声检查 top1 是用户原话 |
| B5 | 最近的高分事件会把老记忆全部滤掉 | `best` 分数把“最近、不可召回”的事件也算进去，cutoff 被抬高 | `best` 只在“可召回的非最近”集合里取 | `test_best_score_ignores_recent_events` |
| B6 | 冷启动只有 3 条语料时完全召回不到 | 小语料做去均值，方向被抹平 | 条数 `< EMBED_MIN_CORPUS(8)` 时不做中心化，改用原始单位向量，阈值 0.45 | `test_small_corpus_still_recalls`；冷启动检查命中 |
| B7 | 问“今天状态”被“今天天气不错”压过；问“中午吃饭”被“晚上吃什么”压过 | 纯语义分对短句区分力弱，最近闲聊还占时间优势 | 排序改为“语义为主 + RRF/时间小幅修正”，再加主题加权：健康类 +0.08，过敏安全 +0.16 | R@1 70%→80%→90%→**100%** |
| B8 | 12 条偏好会把“称呼”挤出上下文，问“我是谁”答错 | 事实按更新时间取前 12 条 | 先单独取 `name` 事实，再用其余事实补足 12 条 | `test_name_never_crowded_out` |
| B9 | 事实注入是英文键，模型容易读错 | 渲染直接用 `predicate` | 改为中文标签（称呼/过敏源/偏好/习惯…） | 身份场景答“你是 Wen” |
| B10 | 同义重复句占满 top3 | 没有近义去重 | 去标点后相同/包含/相似度 ≥0.85 视为同一条，保留排序靠前者 | 噪声 0.30→0.10 |
| B11 | 说“算法题做到一半了”，进度记成 50，状态还是“待办” | 模型只传了 `progress`，没传 `status`，数据层没做语义补全 | 进度 >0 且未显式给状态、原为待办时，自动转“进行中”并记事件 | `test_progress_auto_starts_task`、任务评测场景 7 |

记忆评测最终数字：

| 路线 | R@1 | R@3 | MRR | top3 噪声 | 单句耗时 |
|---|---|---|---|---|---|
| 关键词 | 60% | 70% | 0.65 | 0.20 | <1ms |
| 向量 | 90% | 100% | 0.95 | 0.20 | ~1ms |
| 混合 | **100%** | **100%** | **1.00** | **0.10** | ~1ms |

扩展复核（`eval_memory.py`，39 条语料、15 问句，问法更概括）：**hit@1 100%、hit@3 100%、MRR 1.00**。两套语料都满分，说明不是只对 10 个问句过拟合；但仍不等于真实聊天不会错。

### C. 任务、提醒与调度

| # | 现象 | 根因 | 修法 | 验证 |
|---|---|---|---|---|
| C1 | 父任务拆 6 步，“今日到期”一下多出 6 条 | 子步骤继承父任务截止时间 | 子步骤 `due_at=None`，拆解上限统一为 6 | `test_steps_cap_and_no_due_inherit` |
| C2 | “明天提醒我交周报”不追问几点，模型自己编 09:00 | 只检查有没有日期，没检查钟点 | 新增 `_CLOCK_PAT`：HH:MM、X点、中文数字点、半小时、一刻、整点；只有日期也算没给时间 | `test_date_only_reminder_needs_clock`、任务评测场景 10 |
| C3 | 错过 2 小时的每日提醒，`missed_count` 记 0 | 旧公式按整天差减 1 | 改为 `int(gap/周期)+1` | `test_reminder_missed_count_same_day` |
| C4 | 补发文案出现“补发：启动时补发提醒（…）” | 调度层和界面各加了一次前缀 | 调度层只给内容，界面统一加“⏰ 补发” | `test_catch_up_text_has_no_duplicate_prefix` |
| C5 | 工具调用 id 为空时，assistant 与 tool 消息对不上 | 直接用服务端返回的空 id | 统一生成 `call_{idx}`，两边严格一致 | 审计“工具调用 id 与回填一致” |
| C6 | 模型反复调同一个工具，重复写库 | 没有同轮去重 | 同工具同参数只执行一次，只缓存成功结果；全是重复调用就直接收口 | 审计 + 单测 |

### D. 界面

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| D1 | 勾选“显示已完成”后，“未完成 N 项”把已完成也算进去 | 计数复用了含已完成的列表 | 单独用 `include_done=False` 计数 |
| D2 | 提醒页没选中就点开关会崩（`None["enabled"]`） | 没判空 | 空选/记录不存在时给提示 |

### E. 修复过程中自己引入、已当场消除的回归

| 现象 | 原因 | 处理 |
|---|---|---|
| 查“我设了哪些提醒”时，回复“您设置了一个提醒”被误判成假报喜 | 拦截规则过宽，查询类回复也命中 | 只有本轮确实要求写入（提醒/更新/记一下）才启用拦截 |
| 强制模型调 `task_create` 后，“明天要交周报”的截止时间丢了 | 强制工具打乱了模型自己排 due/domain | 建任务不再强制，交回模型；仍由假报喜兜底 |

### F. 新增能力：MCP、语音与语种

这两块不是“修 bug”，是新能力，单独列出验证范围，避免和上面的回归混在一起。

| 能力 | 检查 | 结果 |
|---|---|---|
| MCP 握手 | `initialize` → `notifications/initialized` → `tools/list`，demo server 返回 3 个工具 | PASS |
| MCP 命名 | `echo` 暴露为 `mcp__demo__echo`；非法字符被替换，符合 OpenAI function name 规则 | PASS |
| MCP 只读 | `readOnlyHint=true` 的 `now` 不弹确认直接执行 | PASS |
| MCP 写入拒绝 | 全局未开 `MCP_ALLOW_WRITE` 时 `write_note` 返回拒绝，文件未变 | PASS |
| MCP 写入确认 | 全局 + server 都开、确认回调返回 True 后写入；返回 False 则不写 | PASS |
| MCP 隔离 | `command` 不存在时只记录错误，不抛异常、不影响其他 server | PASS |
| MCP 无配置 | `mcp_servers.json` 不存在时 `health_check` 仍为 True，内置工具正常 | PASS |
| 语音清洗 | 代码块、链接、URL、Markdown 符号、emoji 被去掉；空白压缩 | PASS |
| 语音截断 | 超过 `TTS_MAX_CHARS` 在句号处截断，不截半句 | PASS |
| 语音关闭 | `TTS_ENABLED=0` 时 `speak()` 直接返回 False，不合成 | PASS |
| 语音降级 | edge 抛异常时走 SAPI 分支，`last_backend` 标记正确 | PASS |
| 语音队列 | 连续 `speak()` 超过队列上限时丢最旧一条，保留新内容 | PASS |
| 音色路由 | 自动模式：中文文本选 `zh-CN-YunxiNeural`，英文文本选 `en-US-BrianMultilingualNeural`，参数 `+0%/+0Hz` | PASS |
| 语种与音色独立 | `set_language_mode` 只改回复语种，`set_voice_mode` 只改音色；`follow` 按语种选音色 | PASS |
| 旧值迁移 | 旧 `voice_mode=zh/en/auto/offline` 转成“对应语言 + follow/sapi”；同名绑定档转成“同语种 + follow” | PASS |
| 跨语种提示 | `zh-CN` 语种 + `en-GB` 音色提示“可能不自然”，但 `set_voice_mode` 仍返回 `en-GB`；不匹配不阻止保存 | PASS |
| 跨语言合成抽样 | 中文文本 + `en-GB`、英文文本 + `zh-CN`、中文文本 + `en-US` 各合成一段 | **3/3 合成成功**，样本 `logs/voice_cross/`；音质是否自然未主观评分，只证明接口不拒绝组合 |
| 语种强制 | `en-GB` 下中文提问得到英文回复；`zh-TW` 下得到繁體中文；5 个语种真实模型实跑均符合 | PASS |
| 整段合成 | `_play_edge()` 对完整回复只生成 1 个音频文件并播放 1 次，不再按句拆段 | PASS |
| 整段触发 | 窗口 `_on_chunk()` 不再触发 TTS；`_on_done()` 拿到完整 `reply` 后只调用一次 `speak(reply)` | PASS（代码审查 + 窗口烟测） |
| ASR 文本归一化 | 汉字间空格被去掉；英文单词间空格保留；多段停顿用逗号连接 | PASS |
| ASR 缺模型/缺依赖 | 模型目录不存在或 `am/` 不完整时返回可读错误，不崩窗口 | PASS |
| ASR 录音循环 | 假流 + 假识别器：`partial` 会回调，`AcceptWaveform` 结果与 `FinalResult` 会追加，停止事件能退出 | PASS |
| ASR 真实模型 | 下载完整 `vosk-model-small-cn-0.22`（43,898,754 字节）并解压；把中文 TTS 样本转成 16k 单声道 WAV 后识别 | PASS，输出“你好我是安迪”；未用真人麦克风复测 |
| ASR 麦克风参数 | `sounddevice` 找到 `麦克风阵列 (Realtek(R) Audio)`，16kHz/单声道/int16 参数校验通过 | PASS，未实际录音 |
| 真实合成（6 音色） | `zh-TW-HsiaoChenNeural`、`zh-CN-YunxiNeural`、`en-GB-RyanNeural`、`en-CA-LiamNeural`、`en-HK-SamNeural`、`en-US-BrianMultilingualNeural` | 全部成功，总耗时 13.52 秒；样本在 `logs/voice_check/` |
| 真实播放 | pygame 在真实声卡初始化 `mixer (44100, -16, 2)`，能加载并播放 mp3 | PASS |
| SAPI 可用 | 系统识别到 `Microsoft Huihui Desktop - Chinese (Simplified)`；英文文本按 `en` 选声音 | PASS |

---

## 四、修复前后对比

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 离线功能审计 | 40/50 | **50/50** |
| 单元测试 | 23 项 | **95 项全过**（含 MCP 7、语音与语种解绑 15、基本问答 16、ASR 7、运行时事件 4） |
| MCP / 语音与语种 / 基本问答 | 未实现 | **MCP 7/7、语音与语种 15/15、基本问答 16/16 通过**；6 个音色真实合成成功；语种与音色互不覆盖 |
| 记忆混合 R@1 | 70% | **100%** |
| 记忆 MRR | 0.85 | **1.00** |
| top3 噪声 | 0.70 | **0.10** |
| 任务评测 | 19/19（但存在假报喜隐患） | **19/19，且写入有保障** |
| 多轮流程 | 第 6 轮提醒不落库、第 11 轮状态答错 | **4/4 全过** |
| GUI | PASS | **PASS** |

---

## 五、还没解决、不要当成已解决

1. **记忆评测是合成语料**：主评测 10 个问句/24 条语料，扩展复核 15 个问句/39 条语料，两套都满分；但不代表真实聊天不会错，主题加权词表也是人工整理的，遇到新领域要补词。
2. **假报喜是“拦住 + 重试 + 如实告知”，不是从根上让模型每次都调工具**；重试会多一次模型调用，极端情况下用户会收到“没写进系统，请再说一次”。
3. **自然语言时间仍由模型换算**，没有独立的日期解析器；“下下周的某个周五”这类表达仍可能算错。
4. **30 秒精度、关机不提醒**没有变化；真实到点触发只做过手动触发验证，没有挂机跨天。
5. **并发只测了 8 线程建任务**，没有长时间多写压测；SQLite 仍是单写锁。
6. **单用户、无鉴权、无多设备同步**；换电脑要拷 `data/baymax.db`。
7. **向量仍可能带噪声**，没有 rerank 模型；阈值是小语料标定值。
8. **标题模糊匹配**（“答辩稿那项”）匹配到多条时会反问编号，这是安全行为，但体验上还可以再优化（例如让用户点选候选）。
9. **MCP 只支持 stdio**：没有远程 HTTP/SSE、OAuth、断线重连；真实第三方 server 兼容性未验证，只在 demo 上跑通。
10. **MCP 工具描述来自外部 server**：默认不完全可信，恶意 server 可能诱导模型调用危险工具；目前靠“写入双开关 + 逐次确认”兜底，没有做工具级白名单。
11. **语音输入是离线小模型，不是云端大模型**：`vosk-model-small-cn-0.22` 已下载并用中文合成音验证通过，但还没用真人麦克风做端到端复测；嘈杂环境、口音、长句的识别率会下降，标点也不一定准确。TTS 改为整段合成后，长回复要等文字全部生成完才出声；`TTS_MAX_CHARS` 仍会截断超长内容；没有做词级同步。语种和音色已解绑，但跨语言朗读的实际音质没有逐个实测——按地区设计的音色念另一种语言可能不自然甚至合成失败，`en-US-BrianMultilingualNeural` 相对更稳；界面只提示，不替用户保证效果。语种切换能强制主流语言，但模型不保证 100% 不混入其他地区拼写。

---

## 六、复现命令

```powershell
cd D:\my_project\PythonProject\Baymax_pro
D:\prgramf\Anaconda3\envs\baymax\python.exe -m pytest tests -q
D:\prgramf\Anaconda3\envs\baymax\python.exe scripts\functional_audit.py
D:\prgramf\Anaconda3\envs\baymax\python.exe scripts\memory_eval.py
D:\prgramf\Anaconda3\envs\baymax\python.exe scripts\task_eval.py
D:\prgramf\Anaconda3\envs\baymax\python.exe scripts\chat_flow.py
D:\prgramf\Anaconda3\envs\baymax\python.exe scripts\smoke_gui.py
D:\prgramf\Anaconda3\envs\baymax\python.exe main.py --selftest
D:\prgramf\Anaconda3\envs\baymax\python.exe -m pytest tests\test_basic_answers.py tests\test_mcp.py tests\test_voice.py -q
```

预期：95 passed / 50 通过 / R@1 100% / 19 通过 / 4 通过 / PASS / 11 项检查（配好 `.env` 与 ASR 模型时全 OK）；MCP 7 + 语音与语种解绑 15 + 基本问答 16 + ASR 7 + 运行时事件 4 通过。

---

## 七、结论

这轮之后，Andy 在“记忆能不能想起来”“说了的事有没有真的写进去”“任务和提醒状态对不对”这三件核心事情上，比上一版可靠了一个档位；同时补上了“接外部工具”“把整段回复读出来”“离线语音输入”和“回复语种与朗读音色分别控制”四块扩展能力。剩下的主要是真实使用中的长尾表达、长时间挂机、多用户、远程 MCP、真人麦克风识别与跨语言朗读音质，不影响现在单机试用。
