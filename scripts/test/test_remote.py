"""通过 CDP 接管正在跑的常驻浏览器（需要 playdriver 起着）。

    python scripts/test/test_remote.py                              # 列页面
    python scripts/test/test_remote.py https://example.com          # 导航
    python scripts/test/test_remote.py https://example.com "#submit"  # 导航 + 点击

连不上：Chrome 136+ 只绑 127.0.0.1，跨机要 socat 或 ssh -L（见 README）。
"""
import sys

from patchright.sync_api import sync_playwright

url = sys.argv[1] if len(sys.argv) > 1 else None
selector = sys.argv[2] if len(sys.argv) > 2 else None

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    for page in context.pages:
        print(f"  {page.title()!r}  {page.url[:70]}")
    page = context.pages[0] if context.pages else context.new_page()

    if url:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        print("导航后:", page.url)
    if selector:
        page.click(selector)
        print("已点击:", selector)
    print("标题:", page.title())
# 不调 browser.close()：这是别人的浏览器，断开连接就够了
