"""reCAPTCHA V2 测试：python scripts/test/test_recaptchav2.py [URL]。"""
import os
import sys

from patchright.sync_api import sync_playwright
from pypasser.captcha import recaptcha
from pypasser.captcha.recaptcha.v2.audio import DEFAULT_MODEL
from pypasser.speech import load_model

CHROME_PATH = "/usr/bin/google-chrome"
PROXY = "http://192.168.51.55:7890"  # 不使用代理时设为 ""
SOLVE_TIMEOUT = 60

if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY

url = sys.argv[1] if len(sys.argv) > 1 else "https://2captcha.com/demo/recaptcha-v2"

# 在创建挑战前下载并缓存同一个识别模型，避免首轮验证期间等待模型下载。
print("预加载语音模型…", flush=True)
load_model(name=DEFAULT_MODEL)
print("语音模型已就绪", flush=True)

with sync_playwright() as pw:
    browser = pw.chromium.launch(
        headless=False, executable_path=CHROME_PATH,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        **({"proxy": {"server": PROXY}} if PROXY else {}),
    )
    try:
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=SOLVE_TIMEOUT * 1000)
        print("落地页:", page.title())
        # 不让后续下载异常掩盖首次转换、识别或验证失败。
        result = recaptcha.v2.solve(page, timeout=SOLVE_TIMEOUT, attempts=1)
        print("is_solved:", result.is_solved)
        print("token_present:", bool(result.response_token))
    finally:
        browser.close()

sys.exit(0 if result.is_solved and result.response_token else 1)
