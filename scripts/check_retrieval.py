"""
检索对照实验：关键词 vs 向量 vs 混合
用法: python scripts/check_retrieval.py
会新建独立测试库 data/embedcheck_<时间>/，不写正式数据
"""

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATA_DIR", f"data/embedcheck_{datetime.now():%H%M%S}")

SEED = [
    "周三要交软件设计师的证，别忘了打印",
    "我把软考冲刺记到学习里了，12月5号截止",
    "我对花生过敏，别推荐带花生的",
    "今天好累，不想动",
    "模电实验报告下周四截止",
    "妈妈说周末回家吃饭",
    "弟弟每天19:30写作业，要盯着",
    "下周要交课程设计文档",
]

QUERIES = [
    "那个证的事怎么样了",        # 换说法：关键词可能失效
    "我下周有什么要交的",        # 概括式提问
    "中午吃饭要注意什么",        # 指向过敏，但没有“花生”二字
    "今天状态怎么样",            # 指向情绪事件
]


def short(s: str, n: int = 18) -> str:
    return s if len(s) <= n else s[:n] + "…"


def main():
    from core import db, memory

    db.init_db()
    for text in SEED:
        memory.log_event("user", text)

    print("建向量索引…")
    r = memory.backfill_vectors(batch=16, limit=100)
    print(f"入库 {r['embedded']} 条，失败 {r['failed']} 条，剩余 {r['remaining']} 条\n")

    print(f"{'问句':<22}{'关键词 top1':<26}{'向量 top1':<26}{'混合 top1'}")
    print("-" * 96)
    for q in QUERIES:
        rep = memory.retrieval_report(q, exclude_recent=0)   # 合成小语料，不排除最近窗口
        kw = rep["keyword"][0] if rep["keyword"] else None
        vec = rep["vector"][0] if rep["vector"] else None
        hyb = rep["hybrid"][0] if rep["hybrid"] else None

        k_txt = f"{short(kw['content'])} {kw['keyword_score']:.2f}" if kw else "（无）"
        v_txt = f"{short(vec['content'])} {vec['vector_score']:.2f}" if vec else "（无）"
        h_txt = f"{short(hyb['content'])} [{hyb['via']}]" if hyb else "（无）"
        print(f"{q:<22}{k_txt:<26}{v_txt:<26}{h_txt}")

    print(f"\n数据库: {db.DB_PATH}")
    print("说明：分数为原始相似度（关键词=字覆盖比例，向量=余弦）；[via] 表示该结果来自哪一路或两路都命中。")


if __name__ == "__main__":
    main()
