import nodriver as uc

async def main():
    # 1. 启动，指向虚拟显示器 :99
    browser = await uc.start(
        headless=False,
        no_sandbox=True,
        browser_executable_path="/usr/bin/google-chrome",
        browser_args=[
            '--disable-dev-shm-usage',
            '--start-maximized',
            '--disable-blink-features=AutomationControlled'
        ]
    )

    # 2. 打开 example.com
    tab = await browser.get("https://example.com", new_tab=True, new_window=False)
    print(f"[test.py] 已打开: {tab.url}")

    # 3. 静默等待30秒，你去 6080 就能看到画面
    print("[test.py] 等待30秒...")
    await tab.sleep(30)

    # 4. 关闭
    print("[test.py] 关闭浏览器")
    await browser.stop()

if __name__ == "__main__":
    uc.loop().run_until_complete(main())