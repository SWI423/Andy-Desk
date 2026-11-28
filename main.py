"""
Andy（Baymax_pro） 主入口
用法:
    python main.py              启动桌面窗口
    python main.py --cli        终端对话（无 GUI 时可用）
    python main.py --selftest   无人值守自检：窗口起 3 秒后自动退出
"""

import argparse
import atexit
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
        ("向量层", "core.embedding"),
        ("MCP 客户端", "core.mcp_client"),
        ("语音层", "core.voice"),
        ("语音输入", "core.asr"),
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


def _cli_confirm_mcp(server: str, tool: str, args: dict) -> bool:
    """终端模式下写入型 MCP 工具必须逐次确认"""
    print(f"\n[MCP 写入请求] {server} / {tool}\n参数：{args}")
    try:
        return input("允许执行？(y/N) > ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run_cli(voice_on: bool = False):
    from core import brain, tasks, voice
    from core.profile import get_user_name
    name = get_user_name()
    if voice_on:
        voice.speaker.set_enabled(True)
    print(f"Andy CLI{' · ' + name if name else ''}。输入 exit 退出。")
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
        if voice_on:
            voice.speaker.stop()
        res = brain.ask_baymax(text, on_chunk=lambda s: print(s, end="", flush=True))
        print()
        if res.get("tools_called"):
            print(f"[工具] {', '.join(res['tools_called'])}")
        if voice_on and not res.get("error"):
            voice.speaker.speak(res.get("reply", ""))


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
    parser = argparse.ArgumentParser(description="Andy · 三线管理助手")
    parser.add_argument("--cli", action="store_true", help="终端模式")
    parser.add_argument("--selftest", action="store_true", help="窗口自检后自动退出")
    parser.add_argument("--voice", action="store_true", help="终端模式下开启语音播报")
    parser.add_argument("--lang", default=None,
                        help="回复语种：auto/zh-CN/zh-TW/en-US/en-GB/en-CA/en-HK")
    parser.add_argument("--voice-mode", default=None,
                        help="朗读音色：follow/zh-CN/zh-TW/en-US/en-GB/en-CA/en-HK/sapi")
    args = parser.parse_args()

    logger.info("=" * 44)
    logger.info("Andy（Baymax_pro） 启动")
    logger.info("=" * 44)
    ensure_dirs()
    from core import db
    db.init_db()
    from core.profile import ensure_first_user
    boot = ensure_first_user()
    logger.info(f"用户档案：{boot['name']}（{'新建' if boot['created'] else '已存在'}）")

    from config import EMBED_ENABLED, EMBED_MODEL
    from core import memory
    if EMBED_ENABLED:
        missing = memory.missing_vector_count()
        logger.info(f"向量检索：{EMBED_MODEL}，待补 {missing} 条")
        memory.kick_backfill()
    else:
        logger.info("向量检索已关闭（EMBED_ENABLED=0），使用关键词检索")

    from core import mcp_client
    if args.cli:
        mcp_client.set_confirm_callback(_cli_confirm_mcp)
    mcp_client.start_all()
    atexit.register(mcp_client.shutdown)
    from core import voice
    atexit.register(voice.speaker.shutdown)
    if args.lang:
        voice.set_language_mode(args.lang)
        logger.info(f"语种：{voice.language_label()}")
    if args.voice_mode:
        voice.set_voice_mode(args.voice_mode)
        logger.info(f"音色：{voice.voice_label()}")
    if not args.cli or args.voice:
        voice.speaker.warmup()
    st = mcp_client.get_manager().status()
    if not st["enabled"]:
        logger.info("MCP：已关闭（MCP_ENABLED=0）")
    elif not st["configured"]:
        logger.info("MCP：未配置 mcp_servers.json，仅使用内置工具")
    else:
        logger.info("MCP：%s 个 server，%s 个工具", len(st["servers"]), len(st["tools"]))
        for err in st["errors"]:
            logger.warning("MCP 连接失败：%s", err)

    health_report()

    if args.cli:
        run_cli(voice_on=args.voice)
    else:
        run_gui(selftest=args.selftest)


if __name__ == "__main__":
    main()
