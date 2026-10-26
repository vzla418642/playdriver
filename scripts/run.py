"""Playdriver 入口：转发服务 + 常驻浏览器，同一个端口同时提供。

  python -u scripts/run.py               # 默认：转发 + 浏览器 + /cookies
  python -u scripts/run.py --no-browser  # 只做转发，不起浏览器（省 300MB 上下）

这不是两种模式：/cookies 和转发本来就挂在同一个 app 上，浏览器只是多挂的一个
组件。默认全开，`--no-browser` 只是把那个组件摘掉，其余一模一样。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# 脚本所在目录即 scripts/，显式加进去，从任意 cwd 调用都能 import 到两个包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="转发服务 + 常驻浏览器，默认两个都开",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不启动常驻浏览器，只做转发；/cookies 返回 503",
    )
    args = parser.parse_args()

    # 浏览器相关依赖（patchright / PyPasser）只在这条路径上导入
    from resident.service import serve

    asyncio.run(serve(with_browser=not args.no_browser))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
