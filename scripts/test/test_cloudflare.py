"""过 Cloudflare 整页墙，并用拿到的 cf_clearance 重放验证。

    python scripts/test/test_cloudflare.py                            # 默认 nairaland
    python scripts/test/test_cloudflare.py https://example.com
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curl_cffi import requests as cffi
from patchright.sync_api import sync_playwright
from pypasser.antibot import cloudflare

from bypass.config import CHROME_PATH, PROXY, SOLVE_TIMEOUT

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.nairaland.com/"
args = ["--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled"]
if PROXY:
    args.append(f"--proxy-server={PROXY}")   # 和后面重放走同一个出口，cf_clearance 才认

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, executable_path=CHROME_PATH, args=args)
    page = browser.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=SOLVE_TIMEOUT * 1000)
    print("落地页:", page.title())

    result = cloudflare.solve(page, timeout=SOLVE_TIMEOUT)
    jar = {c["name"]: c["value"] for c in page.context.cookies()}
    ua = page.evaluate("() => navigator.userAgent")
    browser.close()

print("is_solved:", result.is_solved)
print("cf_clearance:", "cf_clearance" in jar)
if "cf_clearance" not in jar:
    sys.exit("没拿到 cf_clearance：IP 软封，或者这页根本没有墙")

# 重放：cookie + UA 发一个普通请求，200 就说明这条路真的通了
resp = cffi.get(url, cookies=jar, headers={"User-Agent": ua}, impersonate="chrome",
                timeout=30, proxies={"http": PROXY, "https": PROXY} if PROXY else None)
print("重放:", resp.status_code, len(resp.content), "字节")
sys.exit(0 if resp.status_code == 200 else 1)
