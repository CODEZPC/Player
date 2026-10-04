"""歌词编辑器（V1.8.0 新增模块）—— 打轴制作逐字 / 整行 LRC 歌词。

入口：歌词选项区「编辑歌词」按钮（`app._open_lyric_editor`）；
面板以 `place` 叠加在歌曲选择区（文件夹列表 / 歌曲列表 / 信息条）之上。

布局（两栏 + 底部按钮）：
- A 时间戳区（左窄列）：上方大字=当前播放时间（实时刷新），
  下方小字=选中行时间戳（未打轴显示 --:--.---）
- B 歌词列表（右侧大区）：单击选中；双击内联编辑（Enter 确认 / Esc 取消）；
  每行显示「时间戳  文本」
- 底部按钮网格：两行五列（操作名+键位）

十项操作：
  a 添加行（在选中行上方）   b 上一句 ↑/W   c 打轴 F/J（记录当前播放时间
  并跳下一行，末行停留覆盖）   d 下一句 ↓/S   e 删除行   f 撤销 Z
  g 导入（txt/lrc；非逐字型时询问是否按字展开转换）   h 完成 ESC（关闭前
  询问：有歌曲→保存到同名 .lrc 并刷新主界面；无歌曲→询问是否导出）
  i 导出（另存为 .lrc，未打轴行以 [00:00.000] 占位）
  j 重做 Y

约定：撤销/重做为全表快照（上限 50 步）；展开逻辑与 LRC-TRANS 一致
（按空格分词、逐字递增、全角空格填充），原行时间戳复制到其所有子行。
"""

import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from utils import read_text_file

FULLWIDTH_SPACE = "\u3000"

# 行首单时间标签：[mm:ss]、[mm:ss.xx]、[mm:ss.xxx]
_TIME_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")


# ======================================================================
# 纯逻辑辅助（可在无 GUI 环境下单测）
# ======================================================================

def expand_prefix_lines(text: str) -> list[str]:
    """把一行歌词按字展开为前缀递增的逐字行（全角空格填充）。

    与 LRC/LRC-TRANS 工具的输出一致：按空格分词，逐字揭示，
    未揭示部分用全角空格占位。
    """
    words = text.strip().split()
    if not words:
        return []
    widths = [len(word) for word in words]
    revealed = [0] * len(words)
    output: list[str] = []
    for wi, word in enumerate(words):
        for _ in word:
            revealed[wi] += 1
            parts: list[str] = []
            for wj, part_word in enumerate(words):
                count = revealed[wj]
                if count == 0:
                    part = FULLWIDTH_SPACE * widths[wj]
                else:
                    part = (part_word[:count]
                            + FULLWIDTH_SPACE * (widths[wj] - count))
                parts.append(part)
            output.append(" ".join(parts))
    return output


def _norm_text(text: str) -> str:
    """去除普通/全角空白，用于前缀比较。"""
    return text.replace(FULLWIDTH_SPACE, "").replace(" ", "").strip()


def is_prefix_expanded(texts: list[str]) -> bool:
    """判断行列表是否为逐字展开型（存在相邻行的前缀关系）。

    与 app._build_lyric_stem 的中间态判定一致：某行去空白后是下一行的
    严格前缀，即为逐字展开的中间态。
    """
    for i in range(len(texts) - 1):
        cur = _norm_text(texts[i])
        nxt = _norm_text(texts[i + 1])
        if cur and nxt.startswith(cur) and nxt != cur:
            return True
    return False


def _split_ms(t: float) -> tuple[int, int, int]:
    """秒 → (分, 秒, 毫秒)。"""
    total = int(round(max(0.0, float(t)) * 1000))
    minutes, rem = divmod(total, 60000)
    seconds, ms = divmod(rem, 1000)
    return minutes, seconds, ms


def fmt_time(t: float | None) -> str:
    """秒 → M:SS.mmm；未打轴（None）→ --:--.---。"""
    if t is None:
        return "--:--.---"
    minutes, seconds, ms = _split_ms(t)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def fmt_lrc_time(t: float | None) -> str:
    """秒 → LRC 时间标签 [mm:ss.xxx]；未打轴（None）→ [00:00.000]。"""
    minutes, seconds, ms = _split_ms(0.0 if t is None else t)
    return f"[{minutes:02d}:{seconds:02d}.{ms:03d}]"


def parse_lines(text: str) -> list[tuple[float | None, str]]:
    """逐行解析歌词文本 → [(时间或 None, 文本)]。

    - 跳过空行与 [offset:...] 行；
    - 行首时间标签解析为秒；无时间标签的行保留（时间 None）。
    """
    out: list[tuple[float | None, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("[offset:"):
            continue
        m = _TIME_LINE_RE.match(line)
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append((t, m.group(3).strip()))
        else:
            out.append((None, line))
    return out


# ======================================================================
# LyricEditor
# ======================================================================

class LyricEditor:
    """歌词编辑器面板（自包含；app.lyric_editor 为实例）。"""

    UNDO_LIMIT = 50           # 撤销快照上限
    TICK_MS = 100             # A 区当前时间刷新间隔

    def __init__(self, app, parent: tk.Frame) -> None:
        # 延迟导入：app 在本模块导入时尚未定义这些常量
        from app import BG_COLOR, FG_COLOR, ACCENT_COLOR, SUBTLE_COLOR
        self.app = app
        self.parent = parent
        self.BG_COLOR = BG_COLOR
        self.FG_COLOR = FG_COLOR
        self.ACCENT_COLOR = ACCENT_COLOR
        self.SUBTLE_COLOR = SUBTLE_COLOR

        # ---- 数据与状态 ----
        self.lines: list[dict] = []     # {"text": str, "time": float | None}
        self.sel = -1
        self._undo: list[list[dict]] = []
        self._redo: list[list[dict]] = []
        self._editing = False
        self.entry: tk.Entry | None = None
        self._edit_index = -1
        self.visible = False
        self._after_id = None
        self._bind_ids: list[tuple[str, str]] = []
        # 时间显示专用等宽字体（数字不跳动，宽度可控不截断）
        self.time_font = self.app._pick_font(
            "Jetbrains Mono", self.app._font_size(16))

        self._build_ui()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _px(self, v: int, min_px: int = 0) -> int:
        return self.app._px(v, min_px)

    def _build_ui(self) -> None:
        """构建面板（初始隐藏，show 时 place 叠加）。"""
        self.frame = tk.Frame(self.parent, bg=self.BG_COLOR,
                              highlightthickness=1,
                              highlightbackground=self.SUBTLE_COLOR)

        # ---- 上半：A 时间戳区（左） + B 歌词列表（右）----
        top = tk.Frame(self.frame, bg=self.BG_COLOR)
        top.pack(fill="both", expand=True,
                 padx=self._px(8), pady=(self._px(8), self._px(4)))

        a_col = tk.Frame(top, bg=self.BG_COLOR, width=self._px(132, 96))
        a_col.pack(side="left", fill="y")
        a_col.pack_propagate(False)
        tk.Label(a_col, text="当前时间", bg=self.BG_COLOR, fg=self.FG_COLOR,
                 font=self.app.info_font, anchor="w").pack(
            anchor="w", pady=(self._px(4), 0))
        self.cur_time_var = tk.StringVar(value="00:00.000")
        tk.Label(a_col, textvariable=self.cur_time_var, bg=self.BG_COLOR,
                 fg=self.ACCENT_COLOR,
                 font=self.time_font, anchor="w").pack(
            anchor="w", pady=(0, self._px(8)))
        tk.Label(a_col, text="选中行时间戳", bg=self.BG_COLOR,
                 fg=self.FG_COLOR, font=self.app.info_font,
                 anchor="w").pack(anchor="w")
        self.sel_time_var = tk.StringVar(value="--:--.---")
        tk.Label(a_col, textvariable=self.sel_time_var, bg=self.BG_COLOR,
                 fg=self.FG_COLOR, font=self.app.info_font, anchor="w").pack(
            anchor="w")

        b_col = tk.Frame(top, bg=self.BG_COLOR)
        b_col.pack(side="left", fill="both", expand=True,
                   padx=(self._px(8), 0))
        list_inner = tk.Frame(b_col, bg=self.BG_COLOR)
        list_inner.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_inner, bg=self.BG_COLOR, fg=self.FG_COLOR,
            font=self.app.list_font,
            selectbackground=self.ACCENT_COLOR,
            selectforeground=self.BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_inner, command=self.listbox.yview,
                           style="LRC.Vertical.TScrollbar")
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", self._on_double_click)
        # ↑/↓ 在列表控件层拦截（避免与 Listbox 原生移动双重触发）
        self.listbox.bind("<Up>", lambda e: (self.prev_line(), "break")[1])
        self.listbox.bind("<Down>", lambda e: (self.next_line(), "break")[1])

        # ---- 下半：按钮网格（两行五列，操作名+键位）----
        btn_grid = tk.Frame(self.frame, bg=self.BG_COLOR)
        btn_grid.pack(fill="x", padx=self._px(8),
                      pady=(0, self._px(8)))
        ops = (
            ("添加行", self.add_line),
            ("上一句 ↑W", self.prev_line),
            ("打轴 FJ", self.stamp),
            ("下一句 ↓S", self.next_line),
            ("删除行", self.delete_line),
            ("撤销 Z", self.undo),
            ("导入", self.import_file),
            ("完成 ESC", self.finish),
            ("导出", self.export_file),
            ("重做 Y", self.redo),
        )
        for i, (text, cmd) in enumerate(ops):
            row, col = divmod(i, 5)
            b = tk.Button(
                btn_grid, text=text, command=cmd,
                bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
                font=self.app.button_font_sm,
                activebackground=self.ACCENT_COLOR,
                activeforeground=self.BG_COLOR,
                relief="flat", padx=self._px(4), pady=self._px(4))
            b.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
        for col in range(5):
            btn_grid.columnconfigure(col, weight=1)

    # ------------------------------------------------------------------
    # 显隐
    # ------------------------------------------------------------------
    def show(self) -> None:
        """打开编辑器：载入当前歌曲歌词并叠加显示。"""
        if self.visible:
            return
        self.visible = True
        self._load_current()
        self.frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.frame.lift()
        self._bind_keys()
        self.listbox.focus_set()
        self._tick()

    def hide(self) -> None:
        """关闭编辑器（内容丢弃；保存/导出由 finish 负责）。"""
        if not self.visible:
            return
        self.visible = False
        self._cancel_edit()
        self.frame.place_forget()
        self._unbind_keys()
        if self._after_id is not None:
            try:
                self.app.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    # ------------------------------------------------------------------
    # 载入 / 刷新
    # ------------------------------------------------------------------
    def _load_current(self) -> None:
        """载入当前歌曲歌词；非逐字型时询问是否展开。"""
        self._cancel_edit()
        self._undo.clear()
        self._redo.clear()
        src: list[tuple[float, str]] = []
        if self.app.lrc_lines and self.app.lrc_path is not None:
            src = list(self.app.lrc_lines)
        self.lines = [{"text": t, "time": ts} for ts, t in src]
        self.sel = 0 if self.lines else -1
        self._refresh_list()
        if self.lines and not is_prefix_expanded(
                [l["text"] for l in self.lines]):
            if messagebox.askyesno(
                    "编辑歌词",
                    "当前歌词不是逐字（前缀）歌词。\n"
                    "是否转换为逐字歌词（按字展开）？",
                    parent=self.app.root):
                self._push_undo()
                self.lines = self._expand_pairs(
                    [(l["time"], l["text"]) for l in self.lines])
                self.sel = 0 if self.lines else -1
                self._refresh_list()

    @staticmethod
    def _expand_pairs(pairs) -> list[dict]:
        """把 [(时间, 文本)] 逐行展开为逐字行（时间复制到所有子行）。"""
        out: list[dict] = []
        for ts, text in pairs:
            for sub in expand_prefix_lines(text):
                out.append({"text": sub, "time": ts})
        return out

    def _row_text(self, line: dict) -> str:
        return f"{fmt_time(line['time'])}  {line['text']}"

    def _refresh_list(self) -> None:
        """全量重建列表（导入/撤销/重做/增删后调用）。"""
        self._cancel_edit()
        self.listbox.delete(0, "end")
        for line in self.lines:
            self.listbox.insert("end", self._row_text(line))
        if self.lines:
            self.sel = max(0, min(self.sel, len(self.lines) - 1))
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.sel)
            self.listbox.see(self.sel)
        else:
            self.sel = -1
        self._update_sel_time()

    def _update_row(self, idx: int) -> None:
        """刷新单行显示（打轴/文本编辑后调用）。"""
        if not (0 <= idx < len(self.lines)):
            return
        self.listbox.delete(idx)
        self.listbox.insert(idx, self._row_text(self.lines[idx]))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)

    def _select(self, idx: int) -> None:
        """选中指定行（越界钳制）。"""
        if not self.lines:
            self.sel = -1
            self._update_sel_time()
            return
        idx = max(0, min(int(idx), len(self.lines) - 1))
        self.sel = idx
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._update_sel_time()

    def _on_select(self, _event=None) -> None:
        """列表选中变化（用户单击/键盘）→ 同步选中索引。"""
        if self._editing:
            return
        sel = self.listbox.curselection()
        if sel:
            self.sel = sel[0]
            self._update_sel_time()

    def _update_sel_time(self) -> None:
        """刷新 A 区下方「选中行时间戳」。"""
        if 0 <= self.sel < len(self.lines):
            self.sel_time_var.set(fmt_time(self.lines[self.sel]["time"]))
        else:
            self.sel_time_var.set("--:--.---")

    # ------------------------------------------------------------------
    # A 区实时刷新
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if not self.visible:
            return
        try:
            self.cur_time_var.set(fmt_time(self.app._current_time()))
        except Exception:
            pass
        self._after_id = self.app.root.after(self.TICK_MS, self._tick)

    # ------------------------------------------------------------------
    # 十项操作
    # ------------------------------------------------------------------
    def add_line(self) -> None:
        """添加行：在选中行上方插入空行并选中（无选中时插入顶部）。"""
        self._push_undo()
        idx = self.sel if self.sel >= 0 else 0
        self.lines.insert(idx, {"text": "", "time": None})
        self._refresh_list()
        self._select(idx)

    def prev_line(self) -> None:
        """上一句。"""
        if not self.lines:
            return
        self._select(self.sel - 1)

    def stamp(self) -> None:
        """打轴：记录当前播放时间到选中行，并跳转下一行（末行停留覆盖）。"""
        if self._editing or not self.lines or self.sel < 0:
            return
        self._push_undo()
        self.lines[self.sel]["time"] = self.app._current_time()
        self._update_row(self.sel)
        self._update_sel_time()
        if self.sel < len(self.lines) - 1:
            self._select(self.sel + 1)

    def next_line(self) -> None:
        """下一句。"""
        if not self.lines:
            return
        self._select(self.sel + 1)

    def delete_line(self) -> None:
        """删除选中行。"""
        if not self.lines or self.sel < 0:
            return
        self._push_undo()
        del self.lines[self.sel]
        self._refresh_list()
        self._select(self.sel)

    def undo(self) -> None:
        """撤销。"""
        if not self._undo:
            return
        self._redo.append([dict(d) for d in self.lines])
        self.lines = self._undo.pop()
        self._refresh_list()

    def redo(self) -> None:
        """重做。"""
        if not self._redo:
            return
        self._undo.append([dict(d) for d in self.lines])
        self.lines = self._redo.pop()
        self._refresh_list()

    def import_file(self) -> None:
        """导入 txt/lrc：非逐字（前缀）歌词时询问是否转换为逐字歌词。"""
        if self._editing:
            return
        path = filedialog.askopenfilename(
            parent=self.app.root, title="导入歌词",
            filetypes=[("歌词/文本", "*.lrc *.txt"),
                       ("LRC 歌词", "*.lrc"),
                       ("文本文件", "*.txt"),
                       ("所有文件", "*.*")])
        if not path:
            return
        content = read_text_file(path)
        if content is None:
            messagebox.showerror("导入歌词", "无法读取文件。",
                                 parent=self.app.root)
            return
        pairs = parse_lines(content)
        if not pairs:
            messagebox.showwarning("导入歌词", "文件中没有可用的歌词行。",
                                   parent=self.app.root)
            return
        expand = False
        if not is_prefix_expanded([t for _, t in pairs]):
            expand = messagebox.askyesno(
                "导入歌词",
                "该文件不是逐字（前缀）歌词。\n"
                "是否转换为逐字歌词（按字展开）？",
                parent=self.app.root)
        self._push_undo()
        if expand:
            self.lines = self._expand_pairs(pairs)
        else:
            self.lines = [{"text": t, "time": ts} for ts, t in pairs]
        self.sel = 0 if self.lines else -1
        self._refresh_list()

    def finish(self) -> None:
        """完成：关闭前询问保存；无歌曲时询问导出。"""
        if self._editing:
            return
        if not self.lines:
            self.hide()
            return
        audio = self.app.audio_path
        if audio:
            name = os.path.splitext(os.path.basename(audio))[0] or "未命名"
            ans = messagebox.askyesnocancel(
                "完成编辑",
                f"是否保存到歌曲同名歌词文件？\n{name}.lrc",
                parent=self.app.root)
            if ans is None:
                return
            if ans and not self._save_to_song(audio):
                return
        else:
            ans = messagebox.askyesnocancel(
                "完成编辑",
                "当前没有播放歌曲，是否导出歌词到文件？",
                parent=self.app.root)
            if ans is None:
                return
            if ans and not self.export_file():
                return
        self.hide()

    def export_file(self) -> bool:
        """导出：另存为文件（未打轴行以 [00:00.000] 占位）。返回是否成功。"""
        audio = self.app.audio_path
        default_dir = os.path.dirname(audio) if audio else ""
        default_name = ((os.path.splitext(os.path.basename(audio))[0]
                         or "lyrics") + ".lrc") if audio else "lyrics.lrc"
        path = filedialog.asksaveasfilename(
            parent=self.app.root, title="导出歌词",
            defaultextension=".lrc",
            initialdir=default_dir or None,
            initialfile=default_name,
            filetypes=[("LRC 歌词", "*.lrc"),
                       ("文本文件", "*.txt"),
                       ("所有文件", "*.*")])
        if not path:
            return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._to_lrc_text())
        except OSError as e:
            messagebox.showerror("导出歌词", f"写入失败：{e}",
                                 parent=self.app.root)
            return False
        return True

    # ------------------------------------------------------------------
    # 保存 / 导出辅助
    # ------------------------------------------------------------------
    def _to_lrc_text(self) -> str:
        """生成 LRC 文本（未打轴行占位 [00:00.000]）。"""
        return "".join(f"{fmt_lrc_time(l['time'])}{l['text']}\n"
                       for l in self.lines)

    def _save_to_song(self, audio_path: str) -> bool:
        """保存到歌曲同名 .lrc 并刷新主界面歌词。返回是否成功。"""
        target = os.path.splitext(audio_path)[0] + ".lrc"
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self._to_lrc_text())
        except OSError as e:
            messagebox.showerror("保存歌词", f"写入失败：{e}",
                                 parent=self.app.root)
            return False
        try:
            self.app._load_lrc_file(target)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------
    # 撤销栈
    # ------------------------------------------------------------------
    def _push_undo(self) -> None:
        """记录全表快照（上限 UNDO_LIMIT），清空重做栈。"""
        self._undo.append([dict(d) for d in self.lines])
        if len(self._undo) > self.UNDO_LIMIT:
            del self._undo[0]
        self._redo.clear()

    # ------------------------------------------------------------------
    # 双击内联编辑
    # ------------------------------------------------------------------
    def _on_double_click(self, event) -> None:
        if not self.lines:
            return
        idx = self.listbox.nearest(event.y)
        if 0 <= idx < len(self.lines):
            self._begin_edit(idx)

    def _begin_edit(self, idx: int) -> None:
        """在列表行上覆盖 Entry 开始编辑（Enter 确认 / Esc 取消）。"""
        self._commit_edit()
        try:
            box = self.listbox.bbox(idx)
        except tk.TclError:
            return
        if not box:
            return
        _x, y, _w, h = box
        self._edit_index = idx
        self._editing = True
        self.entry = tk.Entry(
            self.listbox, bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
            font=self.app.list_font, relief="flat",
            highlightthickness=1, highlightbackground=self.ACCENT_COLOR,
            insertbackground=self.FG_COLOR)
        self.entry.insert(0, self.lines[idx]["text"])
        self.entry.select_range(0, "end")
        self.entry.place(x=0, y=y, width=self.listbox.winfo_width(),
                         height=h)
        self.entry.focus_set()
        self.entry.bind("<Return>", self._on_edit_commit)
        self.entry.bind("<KP_Enter>", self._on_edit_commit)
        self.entry.bind("<Escape>", self._on_edit_cancel)
        self.entry.bind("<FocusOut>", lambda e: self._commit_edit())
        # 空格手动插入并阻断传播（避免同时触发主窗口播放/暂停）
        self.entry.bind("<space>", self._on_edit_space)

    def _on_edit_commit(self, _event=None) -> str:
        self._commit_edit()
        return "break"

    def _on_edit_cancel(self, _event=None) -> str:
        self._cancel_edit()
        return "break"

    def _on_edit_space(self, _event=None) -> str:
        if self.entry is not None:
            self.entry.insert("insert", " ")
        return "break"

    def _commit_edit(self) -> None:
        """提交编辑内容（有变化时记录撤销）。"""
        if not self._editing or self.entry is None:
            return
        idx = self._edit_index
        text = self.entry.get()
        self._editing = False
        try:
            self.entry.destroy()
        except tk.TclError:
            pass
        self.entry = None
        self._edit_index = -1
        if 0 <= idx < len(self.lines) and text != self.lines[idx]["text"]:
            self._push_undo()
            self.lines[idx]["text"] = text
            self._update_row(idx)
        try:
            self.listbox.focus_set()
        except tk.TclError:
            pass

    def _cancel_edit(self) -> None:
        """放弃编辑（不保存）。"""
        if not self._editing:
            return
        self._editing = False
        if self.entry is not None:
            try:
                self.entry.destroy()
            except tk.TclError:
                pass
            self.entry = None
        self._edit_index = -1

    # ------------------------------------------------------------------
    # 快捷键（root 绑定，关闭时解绑；编辑中忽略）
    # ------------------------------------------------------------------
    _ROOT_KEYS = ("<w>", "<W>", "<s>", "<S>", "<f>", "<F>",
                  "<j>", "<J>", "<z>", "<Z>", "<y>", "<Y>", "<Escape>")

    def _bind_keys(self) -> None:
        """编辑器打开期间把快捷键挂在 root 上（列表 ↑/↓ 单独在控件层）。"""
        root = self.app.root
        mapping = {
            "<w>": self._key_prev, "<W>": self._key_prev,
            "<s>": self._key_next, "<S>": self._key_next,
            "<f>": self._key_stamp, "<F>": self._key_stamp,
            "<j>": self._key_stamp, "<J>": self._key_stamp,
            "<z>": self._key_undo, "<Z>": self._key_undo,
            "<y>": self._key_redo, "<Y>": self._key_redo,
            "<Escape>": self._key_finish,
        }
        for seq in self._ROOT_KEYS:
            try:
                bid = root.bind(seq, mapping[seq], add="+")
                self._bind_ids.append((seq, bid))
            except tk.TclError:
                pass

    def _unbind_keys(self) -> None:
        for seq, bid in self._bind_ids:
            try:
                self.app.root.unbind(seq, bid)
            except tk.TclError:
                pass
        self._bind_ids.clear()

    def _key_prev(self, _event=None) -> str:
        if not self._editing:
            self.prev_line()
        return "break"

    def _key_next(self, _event=None) -> str:
        if not self._editing:
            self.next_line()
        return "break"

    def _key_stamp(self, _event=None) -> str:
        if not self._editing:
            self.stamp()
        return "break"

    def _key_undo(self, _event=None) -> str:
        if not self._editing:
            self.undo()
        return "break"

    def _key_redo(self, _event=None) -> str:
        if not self._editing:
            self.redo()
        return "break"

    def _key_finish(self, _event=None) -> str:
        if not self._editing:
            self.finish()
        return "break"
