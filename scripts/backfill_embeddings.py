"""
历史对话补向量 / 查看索引状态
用法:
    python scripts/backfill_embeddings.py --status        # 只看状态
    python scripts/backfill_embeddings.py                 # 补到上限（默认 400 条）
    python scripts/backfill_embeddings.py --limit 1000 --batch 32
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    parser = argparse.ArgumentParser(description="Andy 向量索引维护")
    parser.add_argument("--limit", type=int, default=None, help="本次最多补多少条")
    parser.add_argument("--batch", type=int, default=None, help="每批多少条")
    parser.add_argument("--status", action="store_true", help="只看状态，不写入")
    args = parser.parse_args()

    import config
    from core import db, memory, embedding

    db.init_db()
    print(f"数据库: {db.DB_PATH}")
    print(f"模型:   {config.EMBED_MODEL}（启用={config.EMBED_ENABLED}）")

    total = db.query_one(
        "SELECT COUNT(*) AS c FROM chat_events WHERE role IN ('user','assistant')")["c"]
    indexed = db.query_one(
        "SELECT COUNT(*) AS c FROM event_vectors WHERE model=?", (config.EMBED_MODEL,))["c"]
    missing = memory.missing_vector_count()
    print(f"对话事件: {total} 条 / 已建向量: {indexed} 条 / 待补: {missing} 条")

    if args.status:
        return
    if missing == 0:
        print("无需补向量")
        return
    if not config.EMBED_ENABLED:
        print("EMBED_ENABLED=0，跳过")
        return

    r = memory.backfill_vectors(batch=args.batch, limit=args.limit)
    print(f"本次补 {r['embedded']} 条，失败 {r['failed']} 条，剩余 {r['remaining']} 条")
    if r["failed"]:
        print("有失败，检查网络/额度后重跑即可（已建索引的不会重复处理）")
    elif r["remaining"]:
        print("还有剩余，再跑一次继续补")
    else:
        print("全部补齐")


if __name__ == "__main__":
    main()
