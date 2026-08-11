"""音乐播放器入口。"""

import os
import sys
import tkinter as tk

# 确保运行时目录为脚本所在目录，以便正确加载 Mp.ico 等资源
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import LrcPlayerApp


def main() -> None:
    root = tk.Tk()
    app = LrcPlayerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
