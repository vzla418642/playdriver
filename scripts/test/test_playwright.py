# scripts/test_playwright.py
from patchright.sync_api import sync_playwright
import time
import os

print("启动 Playwright...")

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,  # 必须 False，不然 6080 看不到
        # executable_path="/usr/bin/google-chrome",
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--start-maximized",
            "--proxy-server=http://192.168.51.55:7890"
            # f"--display={os.environ.get('DISPLAY', ':99')}",
        ]
    )
    
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US"
    )
    
    page = context.new_page()
    page.goto("https://www.google.com/recaptcha/api2/demo")
    
    print(f"已打开: {page.url}")
    print("等待30秒，你去 http://localhost:6080 看画面...")
    
    time.sleep(30)
    
    print("关闭")
    browser.close()