# 1. 最简：跑容器自带的测试
docker run --rm -p 6080:6080 ghcr.io/vic2ray/playdriver:latest

# 2. 跑他自己的脚本，这才是你要的隐形
docker run --rm -p 6080:6080 -v C:\Users\Administrator\scripts:/app/scripts \
  ghcr.io/vic2ray/playdriver:latest python scripts/test.py

# 3. 跑任意命令都行
docker run --rm -p 6080:6080 -v C:\Users\Administrator\scripts:/app/scripts \
  ghcr.io/vic2ray/playdriver:latest python scripts/claude2api.py --headless=False

# 4. 交互调试
docker run --rm -it -p 6080:6080 ghcr.io/vic2ray/playdriver:latest bash