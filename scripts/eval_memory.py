"""
记忆检索评测：固定语料 + 标注问句，输出 hit@1 / hit@3 / MRR
用法: python scripts/eval_memory.py [--variant hybrid|keyword|vector] [--with-assistant] [--exclude-recent N]
新建独立库 data/memeval_<时间>/，不写正式数据；每次跑完可复现指标。

语料为 39 条用户发言 + 可选 12 条助手回复（模拟真实对话里的助手噪声）。
标注问句 15 条，故意用“换说法/概括式/指代”，考察语义召回而不是字面命中。
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/memeval_{datetime.now():%H%M%S}")

# (语料, 期望命中的子串列表, 距今多少天)
CORPUS = [
    ("周三要交软件设计师的证，别忘了打印", ["软件设计师的证"], 3),
    ("我把软考冲刺记到学习里了，12月5号截止", ["软考冲刺"], 5),
    ("我对花生过敏，别推荐带花生的", ["花生过敏"], 20),
    ("今天好累，不想动", ["好累"], 1),
    ("模电实验报告下周四截止", ["模电实验报告"], 6),
    ("妈妈说周末回家吃饭", ["回家吃饭"], 4),
    ("弟弟每天19:30写作业，要盯着", ["弟弟每天19:30"], 9),
    ("下周要交课程设计文档", ["课程设计文档"], 2),
    ("挑战杯答辩在12月初，PPT还没做", ["挑战杯答辩"], 7),
    ("算法题刷到第30道了，还剩20道", ["算法题"], 3),
    ("408 真题第二遍还没开始", ["408 真题"], 10),
    ("数学一的高数部分要二刷", ["数学一"], 12),
    ("六级听力还是跟不上", ["六级听力"], 8),
    ("毕业论文选题还没定", ["毕业论文选题"], 15),
    ("周报每周五下班前交", ["周报"], 2),
    ("实习简历要改成 Java 后端方向", ["实习简历"], 11),
    ("下周二有个 Java 后端面试", ["Java 后端面试"], 4),
    ("项目验收会推到月底了", ["项目验收会"], 5),
    ("要给导师汇报这周进展", ["导师汇报"], 6),
    ("代码评审约在明天上午", ["代码评审"], 1),
    ("这个月房租 2800，月底交", ["房租"], 16),
    ("体检约在下周三早上空腹", ["体检"], 7),
    ("最近老是失眠，两点才睡", ["失眠"], 2),
    ("晚上跑步改成隔天一次", ["跑步"], 9),
    ("牛奶快没了，记得买", ["牛奶"], 3),
    ("快递放菜鸟驿站了，取件码 8823", ["快递"], 1),
    ("手机套餐下月换成 59 的", ["手机套餐"], 13),
    ("晚上十点后别提醒我学习", ["十点后别提醒"], 20),
    ("早上别叫我，我起不来", ["早上别叫"], 18),
    ("回答尽量短一点，别写清单", ["回答尽量短"], 25),
    ("妈妈下个月生日，想买个围巾", ["妈妈下个月生日"], 14),
    ("珠海到广州的票买好了", ["珠海到广州"], 8),
    ("电工证报名截止到12月10号", ["电工证"], 6),
    ("实验室门禁卡丢了，要去补办", ["门禁卡"], 5),
    ("大挑的申报书要改第三版", ["大挑的申报书"], 7),
    ("软著材料整理好了，等提交", ["软著材料"], 4),
    ("牙疼，吃了布洛芬好点", ["牙疼"], 2),
    ("周末想去正佳广场逛逛", ["正佳广场"], 3),
    ("下个月要交助学贷款材料", ["助学贷款"], 17),
]

# (问句, 可接受的期望子串)
QUERIES = [
    ("那个证的事怎么样了", ["软件设计师的证", "电工证"]),
    ("我下周有什么要交的", ["课程设计文档", "模电实验报告"]),
    ("中午吃饭要注意什么", ["花生过敏", "回家吃饭"]),
    ("今天状态怎么样", ["好累", "失眠"]),
    ("面试准备得怎样了", ["Java 后端面试"]),
    ("六级还考不考", ["六级听力"]),
    ("弟弟作业谁盯着", ["弟弟每天19:30"]),
    ("房租什么时候交", ["房租"]),
    ("体检约了没", ["体检"]),
    ("上次说要改简历来着", ["实习简历"]),
    ("软考复习到哪了", ["软考冲刺", "408 真题"]),
    ("答辩材料准备得怎么样", ["挑战杯答辩", "大挑的申报书"]),
    ("快递到哪了", ["快递"]),
    ("家里牛奶还有吗", ["牛奶"]),
    ("我是不是对什么过敏", ["花生过敏"]),
]

ASSISTANT_NOISE = [
    "已记入学习：软件设计师的证。截止时间我记在本周三，需要提前打印。",
    "好的，我记下了。软考冲刺属于学习域，12 月 5 日截止，优先级高。",
    "收到，花生过敏这条我写进长期记忆了，以后推荐吃的会避开。",
    "听起来今天挺累的，先歇十分钟，再挑一件最小的事做。",
    "模电实验报告下周四截止，需要我提前一天提醒你吗？",
    "周末回家吃饭我记下了，要不要顺路买点水果？",
    "弟弟写作业的事我记住了，每天 19:30 提醒你盯着。",
    "课程设计文档下周交，建议这周先把目录搭好。",
    "挑战杯答辩的 PPT 还没做，我可以帮你拆成几个小步骤。",
    "算法题还剩 20 道，按每天 5 道算，四天能刷完。",
    "408 真题第二遍可以放到下周开始，先把第一遍错题过一遍。",
    "周报每周五下班前交，我周五下午提醒你一次。",
]


def short(s: str, n: int = 22) -> str:
    return s if len(s) <= n else s[:n] + "…"


def seed(with_assistant: bool) -> list:
    """写入语料，返回 [(event_id, 期望子串列表)]"""
    from core import db, memory

    db.init_db()
    labeled = []
    base = datetime.now()
    for content, expects, days_ago in CORPUS:
        ts = (base - timedelta(days=days_ago)).isoformat(timespec="seconds")
        eid = db.execute(
            "INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
            ("default", "user", content, ts, 0.0))
        labeled.append((eid, expects))

    if with_assistant:
        # 助手回复掺在语料中，时间均匀分布，用来观察“助手噪声”对召回的影响
        for i, content in enumerate(ASSISTANT_NOISE):
            ts = (base - timedelta(days=(i % 10) + 1, hours=i)).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
                ("default", "assistant", content, ts, 0.0))
        from core import memory
        memory._VEC_CACHE.update({"key": None, "ids": None, "mat": None, "mean": None,
                                  "centered": False})
    return labeled


def evaluate(mode: str, exclude_recent: int = 0, top_k: int = 3):
    from core import memory

    hit1 = hit3 = 0
    mrr = 0.0
    misses = []
    for q, expects in QUERIES:
        got = memory.search_events(q, top_k=top_k, mode=mode, exclude_recent=exclude_recent)
        contents = [g["content"] for g in got]
        rank = None
        for i, c in enumerate(contents, start=1):
            if any(e in c for e in expects):
                rank = i
                break
        if rank == 1:
            hit1 += 1
        if rank:
            hit3 += 1
            mrr += 1.0 / rank
        else:
            misses.append((q, [short(c) for c in contents]))
    n = len(QUERIES)
    return {"mode": mode, "hit@1": hit1 / n, "hit@3": hit3 / n, "mrr": mrr / n,
            "n": n, "misses": misses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="all", choices=["all", "keyword", "vector", "hybrid"])
    ap.add_argument("--with-assistant", action="store_true")
    ap.add_argument("--exclude-recent", type=int, default=0)
    args = ap.parse_args()

    from core import db, memory

    labeled = seed(args.with_assistant)
    print(f"语料 {len(CORPUS)} 条用户发言" + (" + 12 条助手回复" if args.with_assistant else ""))
    print("建向量索引…")
    r = memory.backfill_vectors(batch=16, limit=200)
    print(f"入库 {r['embedded']} 条，失败 {r['failed']} 条，剩余 {r['remaining']} 条\n")

    modes = ["keyword", "vector", "hybrid"] if args.variant == "all" else [args.variant]
    results = []
    for m in modes:
        res = evaluate({"hybrid": "auto"}.get(m, m), exclude_recent=args.exclude_recent)
        res["label"] = m
        results.append(res)
        print(f"{m:8s} hit@1={res['hit@1']:.2f}  hit@3={res['hit@3']:.2f}  MRR={res['mrr']:.2f}")

    for res in results:
        if res["misses"]:
            print(f"\n[{res['label']}] 未命中 {len(res['misses'])} 条：")
            for q, top in res["misses"]:
                print(f"  {q} -> {top}")
    print(f"\n数据库: {db.DB_PATH}")


if __name__ == "__main__":
    main()
