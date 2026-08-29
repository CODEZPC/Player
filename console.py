"""音乐播放器控制台：置顶窗口（输出区 + 输入区），解析并执行指令。

指令集：
  open <path>                      打开文件或文件夹
  play <序号|歌名>                 播放列表中第 val 首 / 歌名为 name 的歌
  song step <秒> / song time <分> <秒> / song change [t=1]
  song pause / song stop
  set volume / set mode / set rate / set lrc-offset / set balance
  set loudness / set rate-keep；set <obj> reset / set reset
  app topmost/floatlayer/exit
  interlude add/clear/insert/remove/move（<song> 视为 <序号|歌名>）
  console clear / console exit
"""

import os
import shlex
import tkinter as tk
from tkinter import font as tkfont

from utils import format_time


def _parse_bool(s: str):
    """解析布尔文本（true/1/on/yes 等），无法解析返回 None。"""
    s = (s or "").strip().lower()
    if s in ("true", "1", "on", "yes"):
        return True
    if s in ("false", "0", "off", "no"):
        return False
    return None


class PlayerConsole:
    """控制台窗口：置顶、Jetbrains Mono 字体、输出区/输入区、指令解析执行。"""

    MODE_MAP = {"list": 0, "single": 1, "only": 2, "random": 3}
    HELP = (
        "可用指令：\n"
        "  open <path>                       打开文件或文件夹\n"
        "  play <序号|歌名>                  播放第 N 首 / 按歌名（前缀唯一匹配，同名多目录则询问）\n"
        "  song step <秒>                    前进 N 秒\n"
        "  song time <分> <秒>               跳转到 分:秒\n"
        "  song change [t=1]                 切换到下 t 首歌\n"
        "  song pause / song stop            暂停 / 停止\n"
        "  set volume <0~100>                音量(%)\n"
        "  set mode <list|single|only|random>  播放模式\n"
        "  set rate <0.01~10>                倍速（0.01 步进）\n"
        "  set lrc-offset <±100000ms>        歌词偏移（毫秒）\n"
        "  set balance <-1~1>                声道平衡（0.01 步进）\n"
        "  set loudness <0~3>                响度增益（0.01 步进）\n"
        "  set rate-keep <true/false>        保音高\n"
        "  set pitch <-12~12>                音高移调（半音，变调不变速）\n"
        "  set <obj> reset / set reset       重置单项 / 全部重置\n"
        "  app topmost <true/false>          窗口置顶开关\n"
        "  app floatlayer                    开关歌词浮层\n"
        "  app exit                          退出程序\n"
        "  interlude add <song...>           添加多首到插播\n"
        "  interlude clear                   清空插播\n"
        "  interlude insert <a> <b>          把 a 插入到插播中 b 之前\n"
        "  interlude remove <song>           从插播移除一首\n"
        "  interlude move <song> <change|to|top|bottom> [num]  移动插播位置\n"
        "  console clear / console exit      清空输出 / 关闭控制台\n"
        "  help                              显示本帮助\n"
        "Tab 补全指令"
    )

    # ------------------------------------------------------------------
    # 初始化与 UI
    # ------------------------------------------------------------------

    def __init__(self, app) -> None:
        self.app = app
        self._pending_candidates: list[int] | None = None  # play 歧义待选列表
        self._build_ui()
        self._print("Music Player Pro Console 已就绪。输入 help 查看指令。")
        self._print("")

    def _build_ui(self) -> None:
        from app import BG_COLOR, FG_COLOR, ACCENT_COLOR, SUBTLE_COLOR

        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title("Music Player Pro Console")
        win.geometry("660x520")
        win.minsize(420, 520)
        win.attributes("-topmost", True)
        win.configure(bg=BG_COLOR)

        font = self._pick_console_font(11)
        font_sm = self._pick_console_font(10)

        # ---- 输出区（上）----
        out_frame = tk.Frame(win, bg=BG_COLOR)
        out_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.output = tk.Text(
            out_frame, bg=BG_COLOR, fg=FG_COLOR, font=font,
            relief="flat", highlightthickness=0, wrap="word",
            state="disabled", cursor="arrow")
        self.output.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(out_frame, command=self.output.yview,
                              bg=SUBTLE_COLOR, troughcolor=BG_COLOR)
        scroll.pack(side="right", fill="y")
        self.output.config(yscrollcommand=scroll.set)

        # ---- 输入区（下）：输入框 + 「发送 ↵」----
        in_frame = tk.Frame(win, bg=BG_COLOR)
        in_frame.pack(fill="x", padx=8, pady=(0, 8))

        self.entry = tk.Entry(
            in_frame, bg="#2B2F37", fg=FG_COLOR, font=font,
            insertbackground=FG_COLOR, relief="flat", highlightthickness=1,
            highlightbackground=SUBTLE_COLOR, highlightcolor=ACCENT_COLOR)
        self.entry.pack(side="left", fill="x", expand=True)

        self.send_btn = tk.Button(
            in_frame, text="发送 ↵", command=self._send,
            bg=SUBTLE_COLOR, fg=FG_COLOR, font=font_sm,
            activebackground=ACCENT_COLOR, activeforeground=BG_COLOR,
            relief="flat", padx=12, pady=4)
        self.send_btn.pack(side="right", padx=(6, 0))

        # 事件绑定
        self.entry.bind("<Return>", lambda e: self._send())
        self.entry.bind("<Tab>", self._on_tab)
        self.entry.bind("<FocusIn>", self._disable_ime)
        self.send_btn.bind("<FocusIn>", self._disable_ime)
        win.protocol("WM_DELETE_WINDOW", self._hide)

        self.entry.focus_set()

    def _pick_console_font(self, size: int) -> tkfont.Font:
        """选择 Jetbrains Mono（缺省回退 Consolas）。"""
        available = {f.lower() for f in tkfont.families(self.app.root)}
        family = "Jetbrains Mono" if "jetbrains mono" in available else "Consolas"
        return tkfont.Font(family=family, size=size)

    # ------------------------------------------------------------------
    # 窗口控制
    # ------------------------------------------------------------------

    def toggle(self) -> None:
        """显示/隐藏控制台。"""
        if self.win.winfo_viewable():
            self._hide()
        else:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            self.entry.focus_set()
            self._disable_ime()

    def _hide(self) -> None:
        self.win.withdraw()

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def _print(self, text: str = "") -> None:
        self.output.config(state="normal")
        self.output.insert("end", text + "\n")
        self.output.config(state="disabled")
        self.output.see("end")

    # ------------------------------------------------------------------
    # 发送与执行
    # ------------------------------------------------------------------

    def _send(self) -> None:
        """取输入框内容执行，回显指令与结果；有未决选择时先处理选择。"""
        cmd = self.entry.get().strip()
        self.entry.delete(0, "end")
        if not cmd:
            return
        self._print(f"> {cmd}")
        if self._pending_candidates is not None:
            result = self._handle_pending(cmd)
        else:
            try:
                result = self._exec(cmd)
            except Exception as exc:  # 解析/执行异常兜底，避免卡死窗口
                result = f"错误: {exc}"
        if result:
            self._print(str(result))

    def _exec(self, cmd: str) -> str:
        """解析并执行单条指令，返回输出信息。"""
        parts = shlex.split(cmd)
        if not parts:
            return ""
        head = parts[0].lower()

        if head == "help":
            return self.HELP
        if head == "open":
            return self._cmd_open(cmd, parts)
        if head == "play":
            return self._cmd_play(parts)
        if head == "song":
            return self._cmd_song(parts)
        if head == "set":
            return self._cmd_set(parts)
        if head == "app":
            return self._cmd_app(parts)
        if head == "interlude":
            return self._cmd_interlude(parts)
        if head == "console":
            return self._cmd_console(parts)
        oth = self.other(head)
        if oth:
            return oth
        return f"未知指令: {head}（输入 help 查看）"

    def other(self, cmd: str) -> str:
        if cmd == "hello":
            return "hello!"
        return None

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------

    def _cmd_open(self, cmd: str, parts: list) -> str:
        """open <path>：文件夹后台扫描 / 文件直接加载。"""
        if len(parts) < 2:
            return "用法: open <路径>"
        path = cmd[len(parts[0]):].strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1]
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            self.app._start_scan(path)
            return f"已开始扫描文件夹: {path}"
        if os.path.isfile(path):
            if not self.app.engine.ready:
                return "音频后端不可用"
            if not self.app._load_audio_file(path):
                return f"加载失败: {path}"
            self.app._auto_load_lrc(path)
            return f"已打开: {os.path.basename(path)}"
        return f"路径不存在: {path}"

    # ------------------------------------------------------------------
    # play
    # ------------------------------------------------------------------

    def _cmd_play(self, parts: list) -> str:
        """play <序号|歌名>：播放第 val 首 / 名字为 name 的歌。

        歌名按「不含扩展名的文件名」匹配：唯一匹配（含前缀）直接播放；
        同名不同目录等多候选时列出并询问序号。
        """
        if len(parts) < 2:
            return "用法: play <序号|歌名>"
        app = self.app
        if not app.audio_items:
            return "歌曲列表为空"

        # 数字 → 序号（从 1 起）
        try:
            val = int(parts[1])
        except ValueError:
            val = None
        if val is not None:
            idx = val - 1
            if idx < 0 or idx >= len(app.audio_items):
                return f"序号超出范围（1~{len(app.audio_items)}）"
            return self._play_index(idx)

        return self._play_by_name(parts[1])

    def _play_index(self, idx: int) -> str:
        """播放指定索引并返回信息。"""
        app = self.app
        if idx < 0 or idx >= len(app.audio_items):
            return "序号超出范围"
        app._load_track_by_index(idx, autoplay=True)
        name = app.audio_items[idx].get("display") or ""
        return f"已播放第 {idx + 1} 首: {name}"

    @staticmethod
    def _song_base(item: dict) -> str:
        """歌曲匹配名：不含扩展名的文件名（小写）。"""
        raw = item.get("display") or item.get("path") or ""
        return os.path.splitext(os.path.basename(raw))[0].lower()

    def _play_by_name(self, q: str) -> str:
        """按歌名匹配：精确优先，其次前缀；唯一则播放，多候选则询问。"""
        app = self.app
        q = q.strip().lower()
        items = app.audio_items

        exact = [i for i, it in enumerate(items) if self._song_base(it) == q]
        if exact:
            if len(exact) == 1:
                return self._play_index(exact[0])
            return self._ask_play(exact, q)

        prefix = [i for i, it in enumerate(items)
                  if self._song_base(it).startswith(q)]
        if not prefix:
            return f"未找到: {q}"
        if len(prefix) == 1:
            return self._play_index(prefix[0])
        return self._ask_play(prefix, q)

    def _ask_play(self, indices: list, q: str) -> str:
        """列出候选并挂起选择（下次输入视为序号）。"""
        app = self.app
        lines = [f"“{q}” 匹配到 {len(indices)} 首："]
        for n, i in enumerate(indices, 1):
            lines.append(f"  {n}. {app.audio_items[i].get('display') or ''}")
        lines.append("请输入序号选择，或输入 0 取消。")
        self._pending_candidates = indices
        return "\n".join(lines)

    def _handle_pending(self, cmd: str) -> str:
        """处理 play 歧义的序号选择输入。"""
        indices = self._pending_candidates
        self._pending_candidates = None
        try:
            n = int(cmd.strip())
        except ValueError:
            return "输入无效，已取消选择"
        if n <= 0:
            return "已取消"
        if n > len(indices):
            return f"序号超出范围（1~{len(indices)}），已取消"
        return self._play_index(indices[n - 1])

    # ------------------------------------------------------------------
    # song
    # ------------------------------------------------------------------

    def _cmd_song(self, parts: list) -> str:
        """song step/time/change/pause/stop。"""
        if len(parts) < 2:
            return "用法: song <step|time|change|pause|stop> [...]"
        sub = parts[1].lower()
        app = self.app

        if sub == "step":
            if len(parts) < 3:
                return "用法: song step <秒>"
            if not app.audio_path or app.duration is None:
                return "未加载歌曲"
            try:
                n = int(parts[2])
            except ValueError:
                return "参数错误: step 需要整数秒"
            target = max(0.0, min(app.duration, app._current_time() + n))
            app._seek_to(target)
            return (f"已前进 {n} 秒 → {format_time(target)} / "
                    f"{format_time(app.duration)}")

        if sub == "time":
            if len(parts) < 4:
                return "用法: song time <分> <秒>"
            if not app.audio_path or app.duration is None:
                return "未加载歌曲"
            try:
                m = int(parts[2])
                s = int(parts[3])
            except ValueError:
                return "参数错误: time 需要整数分/秒"
            target = max(0.0, min(app.duration, m * 60 + s))
            app._seek_to(target)
            return (f"已跳转到 {format_time(target)} / "
                    f"{format_time(app.duration)}")

        if sub == "change":
            t = 1
            if len(parts) >= 3:
                try:
                    t = int(parts[2])
                except ValueError:
                    return "参数错误: change 需要整数 t"
            if not app.audio_items:
                return "歌曲列表为空"
            cur = app.current_song_index if app.current_song_index is not None else -1
            idx = (cur + t) % len(app.audio_items)
            app._load_track_by_index(idx, autoplay=True)
            name = app.audio_items[idx].get("display") or ""
            return f"已切换到第 {idx + 1} 首: {name}"

        if sub == "pause":
            app._toggle_play_pause()
            if app.is_playing and not app.is_paused:
                return "已恢复播放"
            if app.is_paused:
                return "已暂停"
            return "已停止"

        if sub == "stop":
            app._stop()
            return "已停止"

        return f"未知 song 子指令: {sub}"

    # ------------------------------------------------------------------
    # set
    # ------------------------------------------------------------------

    def _cmd_set(self, parts: list) -> str:
        """set <obj> <值|reset> / set reset。"""
        if len(parts) < 2:
            return "用法: set <obj> <值|reset> 或 set reset"
        obj = parts[1].lower()

        if obj == "reset":
            outs = [self._cmd_set([parts[0], o, "reset"])
                    for o in ("volume", "mode", "rate", "lrc-offset",
                              "balance", "loudness", "rate-keep", "pitch")]
            return "已重置全部:\n" + "\n".join(outs)

        if len(parts) < 3:
            return f"用法: set {obj} <值|reset>"
        if parts[2].lower() == "reset":
            return self._set_reset_one(obj)

        app = self.app

        if obj == "volume":
            try:
                v = int(round(float(parts[2])))
            except ValueError:
                return "参数错误: volume 需要 0~100 的数值"
            v = max(0, min(100, v))
            app.engine.set_volume(v / 100.0)
            app._vol_var.set(v)
            app._update_vol_preview()
            return f"音量: {v}%"

        if obj == "mode":
            name = parts[2].lower()
            if name not in self.MODE_MAP:
                return "参数错误: mode ∈ {list, single, only, random}"
            from app import PLAY_MODES
            idx = self.MODE_MAP[name]
            label, mode = PLAY_MODES[idx]
            app.play_mode_index = idx
            app.play_mode = mode
            app.mode_btn.config(text=f"模式: {label}")
            return f"模式: {label}"

        if obj == "rate":
            try:
                t = float(parts[2])
            except ValueError:
                return "参数错误: rate 需要 0.01~10 的数值"
            t = max(0.01, min(10.0, round(t * 100) / 100))
            app.engine.set_speed(t)
            app._speed_var.set(t)
            app._speed_preview.config(text=f"{t:.2f}x")
            return f"倍速: {t:.2f}x"

        if obj == "lrc-offset":
            try:
                ms = int(round(float(parts[2])))
            except ValueError:
                return "参数错误: lrc-offset 需要整数毫秒"
            ms = max(-100000, min(100000, ms))
            units = ms / 10.0  # 内部单位：10ms
            app.lrc_offset = units
            app._lrc_offset_var.set(units)
            if abs(ms) < 10000:
                text = f"{ms}ms"
            elif abs(ms) < 99995:
                text = f"{(ms / 1000):.2f}s"
            else:
                text = "MAX"
            app._lrc_offset_val.config(text=text,
                                       fg=app._lrc_offset_color(units))
            app._sync_lyrics(app._current_time())
            return f"歌词偏移: {ms:+d}ms"

        if obj == "balance":
            try:
                t = float(parts[2])
            except ValueError:
                return "参数错误: balance 需要 -1~1 的数值"
            t = max(-1.0, min(1.0, round(t * 100) / 100))
            app.engine.set_balance(t)
            app._balance_var.set(t)
            app._balance_val.config(text=f"{t:+.2f}")
            return f"声道平衡: {t:+.2f}"

        if obj == "loudness":
            try:
                t = float(parts[2])
            except ValueError:
                return "参数错误: loudness 需要 0~3 的数值"
            t = max(0.0, min(3.0, round(t * 100) / 100))
            app.engine.set_gain(t)
            app._gain_var.set(t)
            app._gain_val.config(text=f"{t:.2f}x", fg=app._gain_color(t))
            app._update_vol_preview()
            return f"响度增益: {t:.2f}x"

        if obj == "rate-keep":
            on = _parse_bool(parts[2])
            if on is None:
                return "参数错误: rate-keep ∈ {true, false, 1, 0, on, off}"
            app.engine.set_pitch_fix(on)
            app._pitch_btn.config(text="保音高: 开" if on else "保音高: 关")
            return f"保音高: {'开' if on else '关'}"

        if obj == "pitch":
            try:
                t = int(round(float(parts[2])))
            except ValueError:
                return "参数错误: pitch 需要 -12~12 的整数"
            t = max(-12, min(12, t))
            app.engine.set_pitch_shift(t)
            app._pitch_var.set(t)
            app._pitch_preview.config(text=f"{t:+d}",
                                      fg=app._pitch_color(t))
            return f"音高: {t:+d} 半音"

        return f"未知 set 对象: {obj}（输入 help 查看）"

    def _set_reset_one(self, obj: str) -> str:
        """重置单个 set 对象为默认值。"""
        defaults = {
            "volume": "100",
            "mode": "list",
            "rate": "1.0",
            "lrc-offset": "0",
            "balance": "0",
            "loudness": "1.0",
            "rate-keep": "false",
            "pitch": "0",
        }
        if obj not in defaults:
            return f"未知 set 对象: {obj}（输入 help 查看）"
        return self._cmd_set(["set", obj, defaults[obj]])

    # ------------------------------------------------------------------
    # app
    # ------------------------------------------------------------------

    def _cmd_app(self, parts: list) -> str:
        """app topmost / floatlayer / exit。"""
        if len(parts) < 2:
            return "用法: app <topmost|floatlayer|exit>"
        sub = parts[1].lower()
        app = self.app

        if sub == "topmost":
            if len(parts) < 3:
                return "用法: app topmost <true|false>"
            on = _parse_bool(parts[2])
            if on is None:
                return "参数错误: topmost ∈ {true, false, 1, 0, on, off}"
            app.always_on_top = on
            app.root.attributes("-topmost", on)
            app.topmost_btn.config(text="置顶: 开" if on else "置顶: 关")
            return f"置顶: {'开' if on else '关'}"

        if sub == "floatlayer":
            if app._lyric_overlay is not None:
                app._close_lyric_overlay()
                return "已关闭歌词浮层"
            if not app.audio_path:
                return "未加载歌曲，无法打开歌词浮层"
            app._open_lyric_overlay()
            return "已打开歌词浮层"

        if sub == "exit":
            app.on_close()
            return ""  # 窗口已销毁，不再输出

        return f"未知 app 子指令: {sub}"

    # ------------------------------------------------------------------
    # interlude
    # ------------------------------------------------------------------

    def _cmd_interlude(self, parts: list) -> str:
        """interlude add/clear/insert/remove/move（<song> 视为 <序号|歌名>）。"""
        if len(parts) < 2:
            return "用法: interlude <add|clear|insert|remove|move> [...]"
        sub = parts[1].lower()
        app = self.app

        if sub == "add":
            if len(parts) < 3:
                return "用法: interlude add <song1> [song2 ...]"
            added = 0
            for ref in parts[2:]:
                r = self._resolve_song(ref)
                if isinstance(r, str):
                    return r
                _idx, item = r
                app.interlude_items.append(dict(item))
                added += 1
            self._refresh_interlude()
            return (f"已添加 {added} 首到插播"
                    f"（共 {len(app.interlude_items)} 首）")

        if sub == "clear":
            n = len(app.interlude_items)
            app.interlude_items.clear()
            self._refresh_interlude()
            return f"已清空插播（{n} 首）"

        if sub == "insert":
            if len(parts) < 4:
                return "用法: interlude insert <song1> <song2>（song1 插到 song2 之前）"
            r1 = self._resolve_song(parts[2])
            if isinstance(r1, str):
                return r1
            r2 = self._resolve_song(parts[3])
            if isinstance(r2, str):
                return r2
            _i1, item1 = r1
            _i2, item2 = r2
            pos = self._find_in_interlude(item2)
            if pos is None:
                return "song2 不在插播队列中"
            app.interlude_items.insert(pos, dict(item1))
            self._refresh_interlude()
            return (f"已把 {self._show_name(item1)} 插入到 "
                    f"{self._show_name(item2)} 之前（第 {pos + 1} 位）")

        if sub == "remove":
            if len(parts) < 3:
                return "用法: interlude remove <song>"
            r = self._resolve_song(parts[2])
            if isinstance(r, str):
                return r
            _idx, item = r
            pos = self._find_in_interlude(item)
            if pos is None:
                return "该歌曲不在插播队列中"
            del app.interlude_items[pos]
            self._refresh_interlude()
            return f"已从插播移除: {self._show_name(item)}"

        if sub == "move":
            if len(parts) < 4:
                return "用法: interlude move <song> <change|to|top|bottom> [num]"
            r = self._resolve_song(parts[2])
            if isinstance(r, str):
                return r
            _idx, item = r
            cur = self._find_in_interlude(item)
            if cur is None:
                return "该歌曲不在插播队列中"
            mode = parts[3].lower()
            n = len(app.interlude_items)
            try:
                num = int(parts[4]) if len(parts) > 4 else 0
            except ValueError:
                return "参数错误: num 需要整数"
            if mode == "change":
                new = cur + num
            elif mode == "to":
                new = num - 1  # 目标位（1 起）
            elif mode == "top":
                new = 0
            elif mode == "bottom":
                new = n - 1
            else:
                return "参数错误: move 模式 ∈ {change, to, top, bottom}"
            new = max(0, min(n - 1, new))
            if new == cur:
                return "位置未变化"
            items = app.interlude_items
            it = items.pop(cur)
            items.insert(new, it)
            self._refresh_interlude()
            return f"已把 {self._show_name(item)} 移动到第 {new + 1} 位"

        return f"未知 interlude 子指令: {sub}"

    def _resolve_song(self, ref: str):
        """解析 <song>（num|name）为主列表 (索引, item)。

        数字 → 主列表 1 起序号；名字 → 不含扩展名的文件名精确/前缀匹配。
        唯一 → 返回 (idx, item)；否则返回错误提示字符串。
        """
        app = self.app
        if not app.audio_items:
            return "歌曲列表为空"
        try:
            val = int(ref)
        except ValueError:
            val = None
        if val is not None:
            idx = val - 1
            if idx < 0 or idx >= len(app.audio_items):
                return f"序号超出范围（1~{len(app.audio_items)}）"
            return idx, app.audio_items[idx]

        q = ref.strip().lower()
        items = app.audio_items

        def base(it: dict) -> str:
            raw = it.get("display") or it.get("path") or ""
            return os.path.splitext(os.path.basename(raw))[0].lower()

        exact = [i for i, it in enumerate(items) if base(it) == q]
        hits = exact if exact else [i for i, it in enumerate(items)
                                    if base(it).startswith(q)]
        if len(hits) == 1:
            return hits[0], items[hits[0]]
        if not hits:
            return f"未找到: {q}"
        cands = "\n".join(f"  {n}. {items[i].get('display') or ''}"
                          for n, i in enumerate(hits, 1))
        return (f"“{q}” 匹配到 {len(hits)} 首，请用序号或更精确的名字：\n"
                f"{cands}")

    def _find_in_interlude(self, item: dict) -> int | None:
        """在插播队列中按 path 查找歌曲位置，找不到返回 None。"""
        target = item.get("path")
        for i, it in enumerate(self.app.interlude_items):
            if it.get("path") == target:
                return i
        return None

    def _refresh_interlude(self) -> None:
        """刷新插播列表显示与按钮状态。"""
        self.app._refresh_interlude_list()
        self.app._refresh_il_buttons()

    @staticmethod
    def _show_name(item: dict) -> str:
        """歌曲显示名（文件名）。"""
        return os.path.basename(item.get("display")
                                or item.get("path") or "")

    # ------------------------------------------------------------------
    # console
    # ------------------------------------------------------------------

    def _cmd_console(self, parts: list) -> str:
        """console clear / exit。"""
        if len(parts) < 2:
            return "用法: console <clear|exit>"
        sub = parts[1].lower()
        if sub == "clear":
            self.output.config(state="normal")
            self.output.delete("1.0", "end")
            self.output.config(state="disabled")
            return "输出区已清空"
        if sub == "exit":
            self._hide()
            return "控制台已关闭"
        return f"未知 console 子指令: {sub}"

    # ------------------------------------------------------------------
    # Tab 补全
    # ------------------------------------------------------------------

    def _on_tab(self, _event: tk.Event) -> str:
        """Tab 补全：按当前词位置补全已知关键字（最长公共前缀）。"""
        try:
            idx = self.entry.index("insert")
            text = self.entry.get()
            before = text[:idx]
            token = before.split()[-1] if before.strip() else ""
            prefix = before[:len(before) - len(token)]
            candidates = self._complete_candidates(text, token)
            if not candidates:
                return "break"
            common = os.path.commonprefix(candidates)
            if common:
                self.entry.delete(0, "end")
                self.entry.insert(0, prefix + common)
        except Exception:
            pass
        return "break"

    def _complete_candidates(self, text: str, token: str) -> list:
        """按当前位置返回候选关键字列表。"""
        words = text.split()
        n = len(words) if text.strip() else 0
        if n <= 1:
            return [w for w in ("app", "console", "help", "interlude",
                                "open", "play", "set", "song")
                    if w.startswith(token)]
        if words[0] == "song" and n == 2:
            return [w for w in ("step", "time", "change", "pause", "stop")
                    if w.startswith(token)]
        if words[0] == "set" and n == 2:
            return [w for w in ("volume", "mode", "rate", "lrc-offset",
                                "balance", "loudness", "rate-keep", "pitch",
                                "reset")
                    if w.startswith(token)]
        if words[0] == "app" and n == 2:
            return [w for w in ("topmost", "floatlayer", "exit")
                    if w.startswith(token)]
        if words[0] == "interlude" and n == 2:
            return [w for w in ("add", "clear", "insert", "remove", "move")
                    if w.startswith(token)]
        if words[0] == "console" and n == 2:
            return [w for w in ("clear", "exit")
                    if w.startswith(token)]
        return []

    # ------------------------------------------------------------------
    # 输入法切换（Windows：聚焦时关闭 IME，最佳尝试）
    # ------------------------------------------------------------------

    def _disable_ime(self, _event: tk.Event | None = None) -> None:
        """输入框聚焦时通过 ImmSetOpenStatus 关闭 IME（英文输入）。

        仅 Windows 有效；个别系统/输入法可能不生效，不影响其他功能。
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            imm32 = ctypes.windll.imm32
            hwnd = user32.GetFocus()
            hime = imm32.ImmGetContext(hwnd)
            if hime:
                imm32.ImmSetOpenStatus(hime, False)
                imm32.ImmReleaseContext(hwnd, hime)
        except Exception:
            pass
