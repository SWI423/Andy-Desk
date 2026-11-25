"""
记忆检索质量评测（真实 embedding-3，独立库）
用法：python scripts/memory_eval.py
输出：关键词 / 向量 / 混合三路的 Recall@1、Recall@3、MRR、top3 噪声率、耗时
另含：时间衰减对照、助理回声排除、小语料冷启动
"""

import os
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/memeval_{datetime.now():%H%M%S}")

CORPUS = [
    # (天数前, 文本)
    (100, "软考报名的时候选了软件设计师，考试在11月8号"),
    (95, "软考复习计划定的是每天两小时"),
    (80, "我把挑战杯答辩稿的第一版写完了"),
    (60, "挑战杯答辩定在下周三下午两点"),
    (55, "我对花生过敏，食堂别打带花生的菜"),
    (50, "妈妈说她周末回家吃饭"),
    (45, "下周要交课程设计文档"),
    (40, "周三要交软件设计师的证，别忘了打印"),
    (35, "今天好累，不想动"),
    (30, "弟弟的作业要每天检查"),
    (28, "我想买一个新的机械键盘"),
    (25, "编译器实验报告卡在寄存器分配那一步"),
    (20, "老师说答辩要控制在八分钟以内"),
    (18, "晚上十一点以后别提醒我学习"),
    (15, "我在准备蓝桥杯 Java B 组"),
    (12, "暑期实习投了字节和腾讯"),
    (10, "背单词用的是墨墨，每天五十个"),
    (8, "模电实验报告还没写"),
    (6, "周末想去正佳广场看电影"),
    (5, "宿舍电费这个月超了"),
    (4, "数据结构作业还剩两道题"),
    (3, "图书馆四楼靠窗的位置比较安静"),
    (2, "我把高数错题整理成了三页"),
    (1, "腾讯暑期实习已经进二面了"),
]

# 问句 -> 期望命中的语料片段（可多个，命中任一条算对）
QUERIES = [
    ("那个证的事怎么样了", ["软件设计师的证"]),
    ("我下周有什么要交的", ["课程设计文档"]),
    ("中午吃饭要注意什么", ["花生过敏"]),
    ("今天状态怎么样", ["今天好累"]),
    ("软考复习到哪了", ["软考复习计划"]),
    ("答辩要多久", ["八分钟"]),
    ("我在投哪些实习", ["暑期实习投了"]),
    ("单词怎么背的", ["墨墨"]),
    ("键盘买了吗", ["机械键盘"]),
    ("实验报告卡住了", ["寄存器分配", "模电实验报告"]),
]

NOISE_SENTENCES = [
    "今天天气不错",
    "快递到了记得拿",
    "下周可能降温",
    "手机电量不太够",
    "晚上吃什么还没想好",
    "今天走了八千步",
    "自习室人有点多",
    "耳机线又缠住了",
]


def _hit(items, expects):
    for it in items:
        if any(e in it["content"] for e in expects):
            return it
    return None


def evaluate(memory, mode, queries):
    r1 = r3 = 0
    rr = 0.0
    noise = 0
    total = 0
    t0 = time.perf_counter()
    for q, expects in queries:
        total += 1
        res = memory.search_events(q, top_k=3, mode=mode, exclude_recent=0)
        if not res:
            continue
        first = res[0]
        if any(e in first["content"] for e in expects):
            r1 += 1
            rr += 1.0
        else:
            pos = _hit(res, expects)
            if pos:
                idx = res.index(pos)
                rr += 1.0 / (idx + 1)
                noise += idx          # 前面的都是噪声
        if _hit(res, expects):
            r3 += 1
        noise += sum(1 for it in res if not any(e in it["content"] for e in expects)
                     and it["content"] not in NOISE_SENTENCES)
    ms = (time.perf_counter() - t0) * 1000 / max(total, 1)
    return {"recall1": r1 / total, "recall3": r3 / total, "mrr": rr / total,
            "noise": noise / total, "ms": ms}


def main():
    import numpy as np
    import config
    from core import db, memory
    from core import embedding

    db.init_db()
    print("评测库:", db.DB_PATH)
    print("模型:", config.EMBED_MODEL, "| 输出维度待首次调用确认")

    # 写入语料（时间可控，用于测衰减）
    ids = {}
    for days, text in CORPUS:
        ts = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rid = db.execute(
            "INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
            ("eval", "user", text, ts, 0.0))
        ids[text] = rid
    for text in NOISE_SENTENCES:
        db.execute(
            "INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
            ("eval", "user", text, datetime.now().isoformat(timespec="seconds"), 0.0))

    r = memory.backfill_vectors(limit=100)
    print(f"向量化：成功 {r['embedded']} 条，失败 {r['failed']}，剩余 {r['remaining']}")

    # 助理回声：同一句话被 Andy 复述，不应抢走用户原话
    db.execute("INSERT INTO chat_events(session_id, role, content, ts, importance) VALUES(?,?,?,?,?)",
               ("eval", "assistant", "记住你对花生过敏，以后食堂不打带花生的菜",
                datetime.now().isoformat(timespec="seconds"), 0.0))

    print("\n问句对比（top3）")
    print(f"{'问句':<18}{'关键词':<10}{'向量':<10}{'混合':<10}")
    for q, expects in QUERIES:
        mark = []
        for mode in ("keyword", "vector", "auto"):
            res = memory.search_events(q, top_k=3, mode=mode, exclude_recent=0)
            hit = _hit(res, expects)
            mark.append("命中" if hit else "未中")
        print(f"{q:<18}{mark[0]:<10}{mark[1]:<10}{mark[2]:<10}")

    print("\n指标（Recall@1 / @3 / MRR / top3 噪声 / 平均耗时）")
    for mode, name in (("keyword", "关键词"), ("vector", "向量"), ("auto", "混合")):
        m = evaluate(memory, mode, QUERIES)
        print(f"{name}: R@1={m['recall1']:.0%}  R@3={m['recall3']:.0%}  "
              f"MRR={m['mrr']:.2f}  噪声={m['noise']:.2f}  {m['ms']:.0f}ms/句")

    # 时间衰减：同题材新事应压过旧事
    old = memory.search_events("腾讯实习进展", top_k=3, exclude_recent=0)
    print("\n时间衰减检查：问「腾讯实习进展」top1 =", old[0]["content"] if old else "无",
          f"(期望命中『已经进二面』，实际 via={old[0]['via'] if old else '-'})")

    # 花生召回不应是助理复述
    peanut = memory.search_events("我是不是对花生过敏", top_k=3, exclude_recent=0)
    print("回声排除检查：问「我是不是对花生过敏」top1 role =",
          peanut[0]["role"] if peanut else "无",
          "| 内容 =", peanut[0]["content"] if peanut else "无")

    # 小语料冷启动：新库 3 条
    import tempfile
    if db._CONN is not None:
        db._CONN.close()
        db._CONN = None
    db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="memeval_small_"), "small.db")
    db.init_db()
    memory._VEC_CACHE.update({"key": None, "ids": None, "mat": None, "mean": None,
                              "centered": False})
    embedding._CACHE.clear()
    for text in ("我对花生过敏，别买带花生的零食", "周三要交软件设计师的证", "今天有点冷"):
        memory.log_event("user", text)
    memory.backfill_vectors(limit=10)
    small = memory.search_events("花生能不能吃", top_k=3, exclude_recent=0)
    print("冷启动检查（3 条语料）：top1 =", small[0]["content"] if small else "无",
          f"| via={small[0]['via'] if small else '-'}")


if __name__ == "__main__":
    main()
