FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99

# 合并安装 + 清理 + tini
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates \
        xvfb x11vnc websockify novnc tini fonts-noto-cjk \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && install -m 0755 -d /etc/apt/keyrings \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && chmod 777 /tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY start.sh /usr/local/bin/start.sh
COPY scripts/ ./scripts/
RUN chmod +x /usr/local/bin/start.sh

EXPOSE 6080 5900 8000

# 关键：start.sh 变 ENTRYPOINT，用户传什么就执行什么
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start.sh"]
# 默认：转发服务 + 常驻浏览器，同一个端口同时可用。
# 只要转发（不起浏览器、省 200-400MB）：
#   CMD ["python", "-u", "scripts/run.py", "--no-browser"]
CMD ["python", "-u", "scripts/run.py"]