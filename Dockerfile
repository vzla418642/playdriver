FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99

# 合并安装 + 清理 + tini。
# ffmpeg 必须留着：pydub 的 AudioSegment.from_mp3() 会走 mediainfo_json() 调 ffprobe，
# 而 pip 的 imageio-ffmpeg 只打包了 ffmpeg、没有 ffprobe。PyPasser 的 ensure_ffmpeg()
# 只设置 AudioSegment.converter，管不到 prober，所以缺 ffprobe 会报
# "AudioDecodeError: ... No such file or directory: 'ffprobe'"。
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates ffmpeg \
        xvfb x11vnc websockify novnc tini fonts-noto-cjk \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && ffmpeg -version > /dev/null && ffprobe -version > /dev/null \
    && install -m 0755 -d /etc/apt/keyrings \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && chmod 777 /tmp

WORKDIR /app
COPY requirements.txt .
# 装完再卸 playwright：PyPasser 只在 `if TYPE_CHECKING:` 下引用它（browsers/playwright.py），
# 运行期不 import，真正干活的是 patchright。不能改用 --no-deps —— 那会把 httpx/vosk/
# numpy/pillow 一起跳过，得手工补回来。卸载时 pip 会警告 camoufox 缺依赖，忽略即可。
# 最后那句是守门员：pypasser/__init__.py 会急切导入 captcha 下所有子包，缺东西当场报错。
RUN pip install -r requirements.txt \
 && python -c "from pypasser.antibot import cloudflare; from pypasser.captcha import turnstile; print('pypasser ok')"

# 构建时下载并验证 reCAPTCHA V2 默认语音模型；Vosk 解压后自动删除 zip。
# 默认 root 用户的缓存目录：/root/.cache/vosk/vosk-model-small-en-us-0.15。
# 单独成层，修改业务脚本不会重新下载；失败直接终止构建。
RUN python -c "from pathlib import Path; from vosk import Model; name = 'vosk-model-small-en-us-0.15'; model = Model(model_name=name); cache = Path.home() / '.cache/vosk'; assert (cache / name / 'am/final.mdl').is_file(); assert not (cache / (name + '.zip')).exists()"

COPY start.sh /usr/local/bin/start.sh
COPY scripts/ ./scripts/
RUN chmod +x /usr/local/bin/start.sh

# 9222（CDP）故意不写进 EXPOSE：Chrome 只绑容器回环，-p 9222:9222 本来就不通，
# 写上去只会误导；而且 docker run -P 会把它一起发布出去。要用 CDP 就加
# --network host，详见 README「外部脚本控制（CDP）」。
EXPOSE 6080 5900 8000

# 关键：start.sh 变 ENTRYPOINT，用户传什么就执行什么
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start.sh"]
# 默认：转发服务 + 常驻浏览器 + noVNC 桌面（/vnc）+ 手机画面监控（/adbs），
# 同一个端口 8000 全部可用；6080 是 websockify 的老入口，留着但不必再用。
# 只要转发（不起浏览器、省 200-400MB）：
#   CMD ["python", "-u", "scripts/run.py", "--no-browser", "--no-ui"]
CMD ["python", "-u", "scripts/run.py"]