# 大白 Baymax_pro

> AI 健康陪伴桌面宠物

## 快速开始

```bash
# 1. 激活 conda 环境
conda activate baymax

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制环境变量模板
cp .env.example .env
# 编辑 .env 填入你的智谱 API Key

# 4. 运行
python main.py
```

## 项目结构

```
Baymax_pro/
├── main.py              # 入口
├── config.py            # 配置
├── prompts.py           # 提示词
├── console.py           # 日志
├── core/                # 核心逻辑
│   ├── brain.py         # 对话核心
│   ├── memory.py        # 记忆/数据库
│   ├── rag.py           # 知识库检索
│   └── tools.py         # 工具注册
├── desktop/             # 桌宠 UI
│   └── main_window.py
├── data/                # 数据库文件
├── logs/                # 日志文件
├── tests/               # 测试
└── docs/                # 文档
    ├── PRD.md           # 产品需求+技术选型
    ├── WBS.md           # 任务拆解
    └── PROGRESS.md      # 开发进度
```

## Git 提交约定

- `feat:` 新功能
- `fix:` 修复 bug
- `docs:` 文档更新
- `refactor:` 重构
- `test:` 测试
- `chore:` 工程化
