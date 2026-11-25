"""
大白 Baymax_pro 主入口
用法:
    python main.py              启动桌面窗口
    python main.py --cli        终端对话（无 GUI 时可用）
    python main.py --selftest   无人值守自检：窗口起 3 秒后自动退出
"""

import argparse
import sys

from config import check_config, ensure_dirs
from console import logger


def health_report():
    logger.info("--- 模块健康检查 ---")
    ok_all = True
    for name, importer in (
        ("对话核心", "core.brain"),
        ("记忆模块", "core.memory"),
        ("任务模块", "core.tasks"),
        ("调度模块", "core.scheduler"),
        ("工具系统", "core.tools"),
        ("数据库", "core.db"),
    ):
        try:
            mod = __import__(importer, fromlist=["health_check"])
            ok = mod.health_check()
            logger.info(f"{name}: {'OK' if ok else 'FAIL'}")
            ok_all = ok_all and ok
        except Exception as e:
            logger.info(f"{name}: FAIL ({e})")
            ok_all = False
    logger.info(f"模型配置: {'OK' if check_config() else '缺少 ZHIPU_API_KEY'}")
    return ok_all


def run_cli():
    from core import brain, tasks
    print("大白 CLI。输入 exit 退出。")
    print(tasks.overview_text())
    while True:
        try:
            text = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("exit", "quit"):
            break
        if not text:
            continue
        res = brain.ask_baymax(text, on_chunk=lambda s: print(s, end="", flush=True))
        print()
        if res.get("tools_called"):
            print(f"[工具] {', '.join(res['tools_called'])}")


def run_gui(selftest: bool = False):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    from desktop.main_window import BaymaxWindow
    from desktop.theme import STYLE

    app = QApplication(sys.argv)
    app.setApplicationName("Baymax")
    app.setStyleSheet(STYLE)
    window = BaymaxWindow()
    window.show()

    if selftest:
        import os
        from config import LOG_DIR
        shot = os.path.join(LOG_DIR, "selftest_window.png")

        def capture():
            window.grab().save(shot)
            logger.info(f"自检截图已保存: {shot}")

        QTimer.singleShot(1500, capture)
        QTimer.singleShot(3500, app.quit)
        logger.info("自检模式：窗口已显示，3.5 秒后自动退出")

    sys.exit(app.exec())


def main():
    parser = argparse.ArgumentParser(description="大白 · 三线管理助手")
    parser.add_argument("--cli", action="store_true", help="终端模式")
    parser.add_argument("--selftest", action="store_true", help="窗口自检后自动退出")
    args = parser.parse_args()

    logger.info("=" * 44)
    logger.info("大白 Baymax_pro 启动")
    logger.info("=" * 44)
    ensure_dirs()
    from core import db
    db.init_db()
    health_report()

    if args.cli:
        run_cli()
    else:
        run_gui(selftest=args.selftest)


if __name__ == "__main__":
    main()
