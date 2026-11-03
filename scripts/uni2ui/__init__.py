"""uni2ui —— 手机画面监控台：MJPEG 推流 + 点滑按键 / 直接打字。

嵌入 playdriver 时挂在前缀 `/adbs` 下（装配见 `scripts/routes.py`），
也可以独立运行：`python -u uni2ui/app.py --port 8000`。

内部模块一览（依赖单向，箭头指向被依赖方）：

    app        HTTP 路由 / 鉴权
    registry   设备表 + 抓帧器池 + agent 通道池   → discovery / capture / agent
    control    点滑按键 / 文字                    → agent
    capture    抓帧（MJPEG）                      → agent
    agent      ATX agent 的探测 / 拉起 / JSON-RPC
    discovery  只读 adb 设备表
    config     环境变量（UNI2UI_*）

包里刻意不在 `__init__` 里导入任何东西：导入 `uni2ui` 不该顺带把 fastapi、
adbutils 全拉起来。
"""
