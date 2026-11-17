"""PyInstaller 打包入口（等价于 uvicorn app.main:app --port 8787）。

打包：pyinstaller brain.spec  →  dist/elf-brain.exe（免 Python 运行）
"""

import sys

import uvicorn

# Windows 控制台默认 GBK，打印 emoji（digest 标题等）会 UnicodeEncodeError；
# 强制 UTF-8 后壳（Node 默认 utf8 解码）也能正确接收日志。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="info")
