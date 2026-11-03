"""Playdriver 入口：转发服务 + 常驻浏览器 + 手机画面监控，同一个端口同时提供。

  python -u scripts/run.py                     # 默认：转发 + 浏览器 + /vnc + /cookies + /adbs
  python -u scripts/run.py --no-browser        # 不起浏览器（省 300MB 上下），/cookies 返回 503
  python -u scripts/run.py --no-ui             # 不起手机画面监控，/adbs 返回 503
  python -u scripts/run.py --no-vnc            # 不挂 noVNC 桌面，/vnc 返回 503
  python -u scripts/run.py --no-browser --no-ui --no-vnc   # 只做转发

这不是几种模式：四个组件本来就挂在同一个 app 上，`--no-xxx` 只是把对应组件
摘掉，其余一模一样。默认全开。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# 脚本所在目录即 scripts/，显式加进去，从任意 cwd 调用都能 import 到三个包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="转发服务 + 常驻浏览器 + 手机画面监控，默认全开",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不启动常驻浏览器，只做转发；/cookies 返回 503",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="不启动手机画面监控；/adbs 返回 503",
    )
    parser.add_argument(
        "--no-vnc",
        action="store_true",
        help="不挂 noVNC 桌面（/vnc 返回 503）；6080 那条老路不受影响",
    )
    args = parser.parse_args()

    # 浏览器相关依赖（patchright / PyPasser）只在这条路径上导入
    from resident.service import serve

    asyncio.run(
        serve(
            with_browser=not args.no_browser,
            with_ui=not args.no_ui,
            with_vnc=not args.no_vnc,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
