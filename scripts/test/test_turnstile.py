"""过嵌入式 Turnstile widget，打印拿到的 cf-turnstile-response。

    python scripts/test/test_turnstile.py                             # 默认 turnstile-test
    python scripts/test/test_turnstile.py https://example.com/login

token 是一次性的、由宿主页面自己提交，所以这个脚本只能证明 widget 被解开了，
不代表拿到登录态。要登录态请走 /vnc 人工登录一次 + GET /cookies?domain=...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from patchright.sync_api import sync_playwright
from pypasser.captcha import turnstile

from bypass.config import CHROME_PATH, PROXY

url = sys.argv[1] if len(sys.argv) > 1 else "https://turnstile-test.vercel.app/"
args = ["--no-sandbox", "--disable-dev-shm-usage"]
if PROXY:
    args.append(f"--proxy-server={PROXY}")

# args.append(f"--proxy-server=http://192.168.51.55:7890")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, executable_path=CHROME_PATH, args=args)
    page = browser.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    print("落地页:", page.title())

    # 整页墙走 test_cloudflare.py，这里只处理表单里那个 widget
    if not page.query_selector(".cf-turnstile, iframe[src*='challenges.cloudflare.com']"):
        sys.exit("这页没有 Turnstile widget")

    result = turnstile.solve(page, timeout=30)
    token = result.response_token
    if not token:   # managed 模式下 token 也可能已经写进隐藏 input
        node = page.query_selector("input[name='cf-turnstile-response']")
        token = node.get_attribute("value") if node else None
    browser.close()

print("is_solved:", result.is_solved)
print("token:", (token[:12] + "…") if token else "（空）")
sys.exit(0 if token else 1)
