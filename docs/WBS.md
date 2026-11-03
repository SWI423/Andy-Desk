# 大白 Baymax_pro 开发任务拆解书（WBS）

> 版本：v1.0.0  
> 日期：2026-11-03

---

## 任务拓扑图

```
M1 项目骨架初始化
├── M2 配置与日志模块
│   ├── M3 数据库 Schema 初始化
│   │   ├── M4 对话核心 brain.py
│   │   │   ├── M5 记忆模块 memory.py
│   │   │   │   ├── M6 知识库 RAG 模块
│   │   │   │   │   ├── M7 工具注册与调用循环
│   │   │   │   │   │   ├── M8 定时调度器 scheduler
│   │   │   │   │   │   │   ├── M9 桌宠窗口 desktop
│   │   │   │   │   │   │   │   └── M10 全链路验证
```

---

## 任务清单

| 编号 | 任务名 | 优先级 | 目标 | 输入 | 产出 | 验收标准 | 依赖 |
|---|---|---|---|---|---|---|---|
| M1 | 项目骨架初始化 | Must | 建目录、空文件、能 import | PRD | 目录结构 + 空文件 | `python -c "import core.brain"` 不报错 | 无 |
| M2 | 配置与日志模块 | Must | 集中管理配置，双写日志 | PRD | config.py + console.py | 日志同时输出到终端和文件 | M1 |
| M3 | 数据库 Schema 初始化 | Must | 建所有表 | 数据模型 | 建表脚本 + memory.py 空壳 | 数据库文件生成，表结构正确 | M1 |
| M4 | 对话核心 brain.py | Must | 能调智谱 API 流式对话 | PRD + 智谱 API key | ask_baymax() 函数 | 输入文字→流式输出回复 | M2, M3 |
| M5 | 记忆模块 memory.py | Must | 存用户画像、健康记录、提醒 | PRD | save/get 函数集合 | 存数据→查得回来 | M3 |
| M6 | 知识库 RAG 模块 | Must | 向量检索 medical/general 库 | PRD | search_knowledge() | 问"头疼怎么办"返回知识库内容 | M3 |
| M7 | 工具注册与调用循环 | Must | function calling 工具循环 | PRD + 5 个工具定义 | tools.py + brain 集成 | 模型决定调工具→执行→结果回灌 | M4, M5, M6 |
| M8 | 定时调度器 | Must | 提醒、久坐检测 | PRD | scheduler.py | 定时到点触发提醒 | M7 |
| M9 | 桌宠窗口 desktop | Must | PyQt6 桌宠 UI | PRD | desktop/main_window.py | 窗口出现，能拖动，能聊天 | M4 |
| M10 | 全链路验证 | Must | 所有功能跑通 | 全部模块 | 测试报告 | 16 项功能全部通过 | M9 |
| S1 | 语音 TTS | Should | 回复后自动播放语音 | 豆包 API key | tts 模块 | 文字→语音播放 | M10 |
| S2 | 语音 ASR | Should | 按住说话转文字 | 豆包 API key | asr 模块 | 说话→转文字正确 | M10 |
| S3 | 情绪识别与疏导 | Should | 识别情绪低落时共情 | PRD 人设 | prompt 优化 | 说"好累"→共情回复 | M10 |
| S4 | 用户健康画像 | Should | 记录基线信息 | PRD | profile 模块 | 建议时结合个人情况 | M5 |
| S5 | 微信代发提醒 | Should | 定时生成内容→打开微信→粘贴 | wxauto 库 | wechat.py | 粘贴好内容，不自动发送 | M8 |
| C1 | 桌面操作工具集 | Could | 打开程序、截屏、打开网页 | PRD | desktop_tools.py | 说"打开浏览器"能打开 | S5 |
| C2 | MCP 工具接入 | Could | 接外部 MCP 服务 | MCP 协议 | mcp_client.py | 能调外部 MCP 工具 | C1 |

---

## 里程碑与排期

| 里程碑 | 包含任务 | 预估 | 退出标准 |
|---|---|---|---|
| **MVP** | M1-M10 | 3-5 天 | 命令行能完整对话+健康+提醒，桌面窗口能跑 |
| **V1.0** | S1-S5 | 2-3 天 | 有语音、情绪识别、微信代发 |
| **V2.0** | C1-C2 | 3-5 天 | 桌面操作工具集、MCP 接入 |

---

## 关键路径

```
M1 → M2 → M3 → M4 → M7 → M8 → M9 → M10
```

这条链上的任务不能并行，必须按顺序做。其他任务（M5/M6/M9）可以在等待时穿插。
