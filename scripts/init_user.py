"""
首次用户建档 / 查看档案
用法：
    python scripts/init_user.py --name Wen
    python scripts/init_user.py --show
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, memory
from core.profile import ensure_first_user, get_profile_summary


def main():
    parser = argparse.ArgumentParser(description="Andy 用户档案")
    parser.add_argument("--name", default="Wen", help="用户名字")
    parser.add_argument("--show", action="store_true", help="只查看，不建档")
    args = parser.parse_args()

    db.init_db()
    if not args.show:
        r = ensure_first_user(args.name)
        print(f"建档：{r['name']}（{'新建' if r['created'] else '已存在，未覆盖'}）")

    print("\n档案：")
    for k, v in get_profile_summary().items():
        print(f"  {k}: {v}")

    facts = memory.get_facts(limit=10)
    print("\n语义事实：")
    for f in facts:
        print(f"  [{f['domain']}] {f['predicate']} = {f['object']}")


if __name__ == "__main__":
    main()
