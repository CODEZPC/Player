"""歌词编辑器（V1.8.0 新增模块）—— 打轴制作逐字 / 整行 LRC 歌词。

入口：歌词选项区「编辑歌词」按钮（`app._open_lyric_editor`）；
面板以 `place` 覆盖「封面底边 → 窗口底部」的左栏+右栏区域（V1.8.8；
不覆盖右侧操作区），顶部预览行横跨全宽。

布局（上部两栏 + 底部按钮网格）：
- 顶行：当前应显示歌词预览（横跨全宽）
- A 时间戳区（左窄列）：上方大字=当前播放时间（实时刷新）；下方「选中行
  时间戳」**输入框**（M:SS.mmm，可直接输入 1:23.456 / 83.456，回车提交）
  + 「跳转」按钮
- B 歌词列表（右侧大区）：单击选中；双击内联编辑（Enter 确认 / Esc 取消）；
  每行显示「时间戳  文本」
- 底部按钮网格：两行七列（操作名+键位，共 14 项）

十四项操作（a~n）：
  a 添加行（在选中行上方）  b 导入（txt/lrc；非逐字型询问是否按字展开）
  c 上一句 ↑/W  d 打轴 F/J（记录当前播放时间并跳下一行，末行停留覆盖）
  e 下一句 ↓/S  f -0.05s  g +0.05s（微调选中行时间戳）
  h 删除行  i 导出（另存为 .lrc，未打轴行以 [00:00.000] 占位）
  j 撤销 Z  k 完成 ESC（关闭前询问：有歌曲→保存到同名 .lrc 并刷新主界面；
  无歌曲→询问是否导出）  l 重做 Y  m 上移  n 下移（调整选中行顺序，V1.8.8）

约定：撤销/重做为全表快照（上限 50 步）；展开逻辑：中文逐字、英文单词
整体揭示、未揭示部分不补占位（自然拼接），原行时间戳复制到其所有子行。
零宽字符（BOM/ZWSP 等，常见于网上下载歌词）在载入/解析/展开/编辑时清除，
避免展开结果出现“空行”（V1.8.5）。时间戳输入框聚焦期间编辑器快捷键屏蔽。
"""

import os
import re
import tkinter as tk
import unicodedata
from tkinter import filedialog, ttk

from utils import read_text_file

FULLWIDTH_SPACE = "\u3000"

# 英文/数字连续段：作为一个整体揭示单元（含撇号/连字符，如 don't、co-op）
_ASCII_UNIT_RE = re.compile(r"[A-Za-z0-9'’-]+")

# 零宽/不可见字符（网上下载的歌词常含此类字符：展开时会成为“空”揭示单元）
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")


def strip_invisible(text: str) -> str:
    """移除零宽字符（ZWSP/ZWNJ/ZWJ/LRM/RLM/BOM），避免展开产生“空行”。"""
    return _INVISIBLE_RE.sub("", text)

# 行首单时间标签：[mm:ss]、[mm:ss.xx]、[mm:ss.xxx]
_TIME_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")
# 翻译行（V1.11.0）：[tr mm:ss.xxx] 开头；保存时原样保留、不参与编辑
_TRANSLATION_LINE_RE = re.compile(
    r"^\s*\[\s*tr\s+(\d+):(\d+(?:\.\d+)?)\s*\]", re.IGNORECASE)


# ======================================================================
# 纯逻辑辅助（可在无 GUI 环境下单测）
# ======================================================================

def _split_units(word: str) -> list[str]:
    """把空格分隔的词拆为揭示单元。

    连续 ASCII 字母/数字（含 ' ’ -）为一个整体单元（英文按单词揭示）；
    **标点/符号跟随前一个单元**（`Hello,` → 单元 `Hello,`；
    `阿房宫，` → `阿`,`房`,`宫，`，V1.12.13），行首标点没有前一个单元时
    单独成单元；其余字符（中文等）每字符一个单元。
    """
    units: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            units.append("".join(buf))
            buf.clear()

    for ch in word:
        if ch.isspace():
            flush()          # 空白不产生揭示单元
        elif _ASCII_UNIT_RE.fullmatch(ch):
            buf.append(ch)
        elif unicodedata.category(ch)[0] in ("P", "S"):
            # 标点/符号：附着到前一个单元（中英文标点行为一致）
            flush()
            if units:
                units[-1] += ch
            else:
                units.append(ch)
        else:
            flush()
            units.append(ch)
    flush()
    return units


def expand_prefix_lines(text: str) -> list[str]:
    """把一行歌词展开为逐步揭示的行（与 LRC-TRANS 目标一致的逐字/逐词式）。

    - 中文（等非 ASCII 字符）逐字揭示；英文单词/数字整体揭示；
    - 未揭示部分**不补占位空格**（行宽随揭示增长，自然拼接）；
    - 逐步披露顺序：按词序、词内按单元序。

    例：“阿房宫”→[“阿”, “阿房”, “阿房宫”]；“Hello world”→[“Hello”, “Hello world”]。
    """
    words = strip_invisible(text).strip().split()
    if not words:
        return []
    word_units = [_split_units(w) for w in words]

    def build(last_w: int, last_n: int) -> str:
        """第 last_w 词揭示前 last_n 个单元，前面词全部揭示。"""
        parts: list[str] = []
        for wi in range(last_w + 1):
            units = word_units[wi]
            count = last_n if wi == last_w else len(units)
            parts.append("".join(units[:count]))
        return " ".join(parts)

    out: list[str] = []
    for wi, units in enumerate(word_units):
        for k in range(1, len(units) + 1):
            out.append(build(wi, k))
    return out


def _norm_text(text: str) -> str:
    """去除零宽字符与普通/全角空白，用于前缀比较。"""
    return strip_invisible(text).replace(FULLWIDTH_SPACE, "").replace(" ", "").strip()


def prefix_ratio(texts: list[str]) -> float:
    """相邻行对中满足「前缀关系」的比例（0.0~1.0；空行对不计入，V1.11.0）。

    与 app._build_lyric_stem 的中间态判定一致：某行去空白后是下一行的
    严格前缀（逐字展开中间态）即命中。
    """
    pairs = 0
    hits = 0
    for i in range(len(texts) - 1):
        cur = _norm_text(texts[i])
        nxt = _norm_text(texts[i + 1])
        if not cur or not nxt:
            continue          # 空行/占位行不参与比例计算
        pairs += 1
        if nxt.startswith(cur) and nxt != cur:
            hits += 1
    return (hits / pairs) if pairs else 0.0


def is_prefix_expanded(texts: list[str]) -> bool:
    """是否逐字展开型：相邻行前缀关系比例 ≥ 50%（V1.11.0）。

    原判定为「存在任意前缀对即 True」，过于宽松；现按比例判定。
    """
    return prefix_ratio(texts) >= 0.5


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
        line = strip_invisible(raw).strip()
        if not line or line.lower().startswith("[offset:"):
            continue
        if _TRANSLATION_LINE_RE.match(line):
            continue          # 翻译行单独保留，不进编辑列表（V1.11.0）
        m = _TIME_LINE_RE.match(line)
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            out.append((t, m.group(3).strip()))
        else:
            out.append((None, line))
    return out


def parse_time_input(text: str) -> float | None:
    """解析时间戳输入 → 秒；无效返回 None（V1.8.8）。

    接受：纯秒（83.456）与 M:SS.mmm / M:SS（1:23.456）；非负校验，
    带分钟时秒部分须 < 60（避免“1:90”之类歧义输入）。
    """
    s = strip_invisible(str(text)).strip()
    if not s:
        return None
    try:
        if ":" in s:
            m_part, s_part = s.split(":", 1)
            if not m_part or not s_part:
                return None
            minutes = int(m_part)
            seconds = float(s_part)
            if minutes < 0 or seconds < 0 or seconds >= 60:
                return None
            return minutes * 60.0 + seconds
        value = float(s)
        return value if value >= 0 else None
    except ValueError:
        return None


# ======================================================================
# LyricEditor
# ======================================================================

class LyricEditor:
    """歌词编辑器面板（自包含；app.lyric_editor 为实例）。"""

    UNDO_LIMIT = 50           # 撤销快照上限
    TICK_MS = 8             # A 区当前时间刷新间隔

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
        self._initial_lines: list[dict] | None = None  # 打开时快照（未更改判定）
        # 保留的原文件翻译行（[tr ...]，保存时原样带出；V1.11.0）
        self._kept_translation_lines: list[str] = []
        self._time_edit_active = False     # 时间戳输入框聚焦中（屏蔽快捷键）
        self._place_bind_id = None         # root <Configure> 绑定 id（V1.8.8）
        self._panel_geom: tuple | None = None  # 面板几何缓存（同值跳过 place）
        # 时间显示专用等宽字体（数字不跳动，宽度可控不截断）
        self.time_font = self.app._pick_font(
            "Jetbrains Mono", self.app._font_size(16))
        # 歌词文本专用等宽字体（列表与内联编辑统一）
        self.text_font = self.app._pick_font(
            "Jetbrains Mono", self.app._font_size(11))
        # 顶部预览行字体（与主窗口歌词行同族，小一号）
        self.preview_font = self.app._pick_font(
            "汉仪文黑-85W", self.app._font_size(14))

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

        # ---- 顶部：当前应显示歌词预览（横跨全宽，汉仪文黑-85W）----
        self.preview_var = tk.StringVar(value="")
        self.preview_label = tk.Label(
            self.frame, textvariable=self.preview_var,
            bg=self.BG_COLOR, fg=self.FG_COLOR,
            font=self.preview_font, anchor="w")
        self.preview_label.pack(fill="x", padx=self._px(8),
                                pady=(self._px(6), 0))

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
        # 时间戳输入框（V1.8.8：可直接输入 M:SS.mmm 或纯秒；回车提交）
        self.sel_time_var = tk.StringVar(value="--:--.---")
        self.sel_time_entry = tk.Entry(
            a_col, textvariable=self.sel_time_var, font=self.text_font,
            bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
            insertbackground=self.FG_COLOR, relief="flat",
            highlightthickness=0)
        self.sel_time_entry.pack(fill="x", pady=(self._px(2), 0))
        self.sel_time_entry.bind("<Return>", self._on_time_enter)
        self.sel_time_entry.bind("<Escape>", self._on_time_escape)
        self.sel_time_entry.bind("<FocusIn>", self._on_time_focus_in)
        self.sel_time_entry.bind("<FocusOut>", self._on_time_focus_out)
        # 选中行操作按钮（竖排：仅「跳转」；微调按钮已移入底部网格 f/g）
        btn_cfg = dict(bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
                       font=self.app.button_font_sm,
                       activebackground=self.ACCENT_COLOR,
                       activeforeground=self.BG_COLOR,
                       relief="flat", padx=self._px(4), pady=self._px(2))
        self.jump_btn = tk.Button(a_col, text="跳转",
                                  command=self.jump_to_line, **btn_cfg)
        self.jump_btn.pack(fill="x", pady=(self._px(8), 1))
        # 点击后把焦点还给歌词列表（便于继续键盘操作，避免空格双触发）
        self.jump_btn.bind("<ButtonRelease-1>",
                           lambda e: self.listbox.focus_set(), add="+")
        # 「手动转换」按钮（V1.11.0）：逐字判定 ≥50% 且 <100% 时显示，
        # 点击把非逐字（孤立）行展开，整表统一为逐字
        self.convert_btn = tk.Button(a_col, text="手动转换",
                                     command=self.convert_to_verbatim,
                                     **btn_cfg)
        self.convert_btn.bind("<ButtonRelease-1>",
                              lambda e: self.listbox.focus_set(), add="+")

        b_col = tk.Frame(top, bg=self.BG_COLOR)
        b_col.pack(side="left", fill="both", expand=True,
                   padx=(self._px(8), 0))
        list_inner = tk.Frame(b_col, bg=self.BG_COLOR)
        list_inner.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_inner, bg=self.BG_COLOR, fg=self.FG_COLOR,
            font=self.text_font,
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

        # ---- 下半：按钮网格（两行七列，操作名+键位；V1.8.8 扩为 14 项）----
        btn_grid = tk.Frame(self.frame, bg=self.BG_COLOR)
        btn_grid.pack(fill="x", padx=self._px(8),
                      pady=(0, self._px(8)))
        ops = (
            ("添加行", self.add_line),
            ("导入", self.import_file),
            ("上一句 ↑W", self.prev_line),
            ("打轴 FJ", self.stamp),
            ("下一句 ↓S", self.next_line),
            ("-0.05s", lambda: self.nudge_time(-0.05)),
            ("+0.05s", lambda: self.nudge_time(0.05)),
            ("删除行", self.delete_line),
            ("导出", self.export_file),
            ("撤销 Z", self.undo),
            ("完成 ESC", self.finish),
            ("重做 Y", self.redo),
            ("上移", self.move_up),
            ("下移", self.move_down),
        )
        for i, (text, cmd) in enumerate(ops):
            row, col = divmod(i, 7)
            b = tk.Button(
                btn_grid, text=text, command=cmd,
                bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
                font=self.app.button_font_sm,
                activebackground=self.ACCENT_COLOR,
                activeforeground=self.BG_COLOR,
                relief="flat", padx=self._px(4), pady=self._px(4))
            b.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
            # 点击后把焦点还给歌词列表（便于连续打轴/导航）
            b.bind("<ButtonRelease-1>",
                   lambda e: self.listbox.focus_set(), add="+")
        for col in range(7):
            btn_grid.columnconfigure(col, weight=1)

    # ------------------------------------------------------------------
    # 显隐
    # ------------------------------------------------------------------
    def show(self) -> None:
        """打开编辑器：锁定播放模式并载入当前歌曲歌词，覆盖下部区域显示。"""
        if self.visible:
            return
        self.visible = True
        self.app._lock_play_mode_single()   # 编辑期间锁定播放模式为「关闭」
        self._load_current()
        self._panel_geom = None
        self._place_panel()   # 封面底边 → 窗口底部（不覆盖操作区，V1.8.8）
        self.frame.lift()
        self._bind_keys()
        self._bind_place()
        self.listbox.focus_set()
        self._tick()

    def hide(self) -> None:
        """关闭编辑器（内容丢弃；保存/导出由 finish 负责）。"""
        if not self.visible:
            return
        self.visible = False
        self.app._unlock_play_mode()   # 恢复锁定前的播放模式
        self._cancel_edit()
        self.frame.place_forget()
        self._unbind_place()
        self._unbind_keys()
        if self._after_id is not None:
            try:
                self.app.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    # ------------------------------------------------------------------
    # 面板几何（V1.8.8：覆盖“封面底边 → 窗口底部”的左栏+右栏区域）
    # ------------------------------------------------------------------
    def _place_panel(self) -> None:
        """放置面板：顶边=封面底边；右边界=操作区左缘-列间距（不覆盖操作区）。"""
        app = self.app
        try:
            outer = app._outer
            cover = app.cover_frame
            op_col = app.op_col
            y0 = cover.winfo_rooty() - outer.winfo_rooty() + cover.winfo_height()
            w = op_col.winfo_x() - self._px(12)
            h = outer.winfo_height() - y0
        except (AttributeError, tk.TclError):
            return
        if w <= 0 or h <= 0:
            return
        geom = (0, y0, w, h)
        if geom == self._panel_geom:
            return
        self._panel_geom = geom
        try:
            self.frame.place(x=0, y=y0, width=w, height=h)
        except tk.TclError:
            pass

    def _bind_place(self) -> None:
        """窗口尺寸变化时重算面板几何（root <Configure>）。"""
        try:
            self._place_bind_id = self.app.root.bind(
                "<Configure>", self._on_root_configure, add="+")
        except tk.TclError:
            self._place_bind_id = None

    def _unbind_place(self) -> None:
        if self._place_bind_id is not None:
            try:
                self.app.root.unbind("<Configure>", self._place_bind_id)
            except tk.TclError:
                pass
            self._place_bind_id = None

    def _on_root_configure(self, event=None) -> None:
        """root 尺寸变化 → 重算面板几何。

        注意：Configure 事件到达时布局（outer/op_col 的 winfo 值）尚未重排，
        须先 `update_idletasks()` 完成布局再计算（否则读到旧值被同值跳过，
        V1.8.8 实测）。子控件事件冒泡（event.widget ≠ root）忽略。
        """
        if event is not None and getattr(event, "widget", None) is not self.app.root:
            return
        if not self.visible:
            return
        try:
            self.app.root.update_idletasks()
        except tk.TclError:
            return
        self._place_panel()

    # ------------------------------------------------------------------
    # 载入 / 刷新
    # ------------------------------------------------------------------
    def _load_current(self) -> None:
        """载入当前歌曲歌词；非逐字型时询问是否展开。"""
        self._cancel_edit()
        self._undo.clear()
        self._redo.clear()
        # 保留原文件翻译行（[tr ...]），保存写回时原样带出（V1.11.0）
        self._kept_translation_lines = self._read_translation_lines(
            self.app.lrc_path)
        src: list[tuple[float, str]] = []
        if self.app.lrc_lines and self.app.lrc_path is not None:
            src = list(self.app.lrc_lines)
        # 清理零宽字符（BOM/ZWSP 等；残留会在展开时成为“空行”，V1.8.5）
        self.lines = [{"text": strip_invisible(t), "time": ts}
                      for ts, t in src]
        self.sel = 0 if self.lines else -1
        # 记录原载入态快照（展开选择/任何编辑均使“未更改”判定失效）
        self._initial_lines = [dict(d) for d in self.lines]
        self._refresh_list()
        if self.lines and not is_prefix_expanded(
                [l["text"] for l in self.lines]):
            if self.app.show_dialog(
                    "编辑歌词",
                    "当前歌词不是逐字（前缀）歌词。\n"
                    "是否转换为逐字歌词（按字展开）？",
                    kind="question",
                    buttons=(("是", "yes"), ("否", "no"))) == "yes":
                self._push_undo()
                self.lines = self._expand_pairs(
                    [(l["time"], l["text"]) for l in self.lines])
                self.sel = 0 if self.lines else -1
                self._refresh_list()

    def _verbatim_group_flags(self) -> list[bool]:
        """标记每行是否属于逐字组（与前/后续行构成前缀关系；V1.11.0）。"""
        texts = [_norm_text(l["text"]) for l in self.lines]
        n = len(texts)
        flags = [False] * n
        for i in range(n - 1):
            cur, nxt = texts[i], texts[i + 1]
            if cur and nxt and nxt.startswith(cur) and nxt != cur:
                flags[i] = True
                flags[i + 1] = True
        return flags

    def _has_convertible_lines(self) -> bool:
        """是否存在可转换的孤立行（不属于逐字组且能拆出多行；V1.11.0）。"""
        flags = self._verbatim_group_flags()
        for i, line in enumerate(self.lines):
            if flags[i]:
                continue
            if len(expand_prefix_lines(line["text"])) > 1:
                return True
        return False

    def convert_to_verbatim(self) -> None:
        """手动转换（V1.11.0）：把可拆分的孤立行展开，尽量统一为逐字。

        已处于逐字组（与前/后续行构成前缀关系）的行保持原样；
        孤立行按 expand_prefix_lines 展开（时间戳复制到全部子行）；
        无法拆分（如单个英文单词）的行保持原样。
        """
        if not self.lines:
            return
        flags = self._verbatim_group_flags()
        out: list[dict] = []
        changed = False
        for i, line in enumerate(self.lines):
            if flags[i]:
                out.append(dict(line))
                continue
            subs = expand_prefix_lines(line["text"])
            if len(subs) > 1:
                out.extend({"text": s, "time": line["time"]} for s in subs)
                changed = True
            else:
                out.append(dict(line))
        if not changed:
            return
        self._push_undo()
        self.lines = out
        self.sel = 0 if self.lines else -1
        self._refresh_list()

    def _refresh_convert_btn(self) -> None:
        """刷新「手动转换」按钮显隐（V1.11.0）。

        条件：相邻行前缀比例 ∈ [50%, 100%) 且存在可拆分的孤立行
        （逐字组边界处的非前缀对属于正常结构，不算待转换内容）。
        """
        try:
            ratio = prefix_ratio([l["text"] for l in self.lines])
            if 0.5 <= ratio < 1.0 and self._has_convertible_lines():
                self.convert_btn.pack(fill="x", pady=(1, 1))
            else:
                self.convert_btn.pack_forget()
        except tk.TclError:
            pass

    @staticmethod
    def _read_translation_lines(lrc_path: str | None) -> list[str]:
        """读取 lrc 文件中的翻译行（[tr ...]，原样保留用于保存，V1.11.0）。"""
        if not lrc_path:
            return []
        try:
            text = read_text_file(lrc_path) or ""
        except Exception:
            return []
        return [ln.strip() for ln in text.splitlines()
                if _TRANSLATION_LINE_RE.match(ln)]

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
        self._refresh_convert_btn()

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
        """刷新 A 区下方「选中行时间戳」（回写输入框显示）。"""
        if 0 <= self.sel < len(self.lines):
            self.sel_time_var.set(fmt_time(self.lines[self.sel]["time"]))
        else:
            self.sel_time_var.set("--:--.---")

    # ---- 时间戳输入框（V1.8.8）----
    def _on_time_focus_in(self, _event=None) -> None:
        """输入框获得焦点：标记输入态（屏蔽编辑器快捷键）并全选。"""
        self._time_edit_active = True
        try:
            self.sel_time_entry.select_range(0, "end")
        except tk.TclError:
            pass

    def _on_time_focus_out(self, _event=None) -> None:
        """失焦：退出输入态并恢复显示（未回车确认的输入不生效）。"""
        self._time_edit_active = False
        self._update_sel_time()

    def _on_time_enter(self, _event=None) -> str:
        """回车：提交输入的时间戳。"""
        self._time_edit_active = False
        self._commit_time_input()
        return "break"

    def _on_time_escape(self, _event=None) -> str:
        """Esc：放弃输入（恢复原显示）。"""
        self._time_edit_active = False
        self._commit_time_input(cancel=True)
        return "break"

    def _commit_time_input(self, cancel: bool = False) -> None:
        """提交/取消时间戳输入。

    - 有效输入 → 设置选中行时间戳（记撤销；同值跳过）；
    - 无效输入 → 恢复显示 + 主状态栏提示；
    - cancel=True（Esc）→ 直接恢复显示。
        """
        if cancel:
            self._update_sel_time()
            self.listbox.focus_set()
            return
        value = parse_time_input(self.sel_time_var.get())
        if value is None:
            self._update_sel_time()
            try:
                self.app._hint_status(
                    "时间格式无效：支持 1:23.456 或 83.456（秒）")
            except Exception:
                pass
            self.listbox.focus_set()
            return
        if 0 <= self.sel < len(self.lines) \
                and value != self.lines[self.sel]["time"]:
            self._push_undo()
            self.lines[self.sel]["time"] = value
            self._update_row(self.sel)
        self._update_sel_time()
        self.listbox.focus_set()

    def _preview_text_at(self, t: float) -> str:
        """当前时间 t 在编辑数据中应显示的歌词文本。

        取“已打轴且时间 ≤ t”的行中时间最大的一条的文本（行可乱序，
        故遍历取最大者）；无匹配（未到任何已打轴行/全部未打轴）返回空串。
        """
        best_t: float | None = None
        best_text = ""
        for line in self.lines:
            lt = line["time"]
            if lt is not None and lt <= t and (best_t is None or lt > best_t):
                best_t = lt
                best_text = line["text"]
        return best_text

    # ------------------------------------------------------------------
    # A 区实时刷新
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if not self.visible:
            return
        try:
            t = self.app._current_time()
            self.cur_time_var.set(fmt_time(t))
            self.preview_var.set(self._preview_text_at(t))
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

    def jump_to_line(self) -> None:
        """跳转：把播放位置跳到选中行时间戳（保持当前播放状态）。

        播放中→继续播放；暂停中→保持暂停（位置已更新）；停止→仅更新
        进度/时间显示；选中行未打轴（无时间戳）时不响应。
        """
        if not (0 <= self.sel < len(self.lines)):
            return
        t = self.lines[self.sel]["time"]
        if t is None:
            return
        try:
            self.app._seek_to(float(t))
        except Exception:
            pass

    def nudge_time(self, delta: float) -> None:
        """微调选中行时间戳（±秒，按毫秒取整防累积误差；下限 0）。

        记入撤销栈（可 Z 回退）；选中行未打轴（无时间戳）时不响应。
        """
        if not (0 <= self.sel < len(self.lines)):
            return
        t = self.lines[self.sel]["time"]
        if t is None:
            return
        self._push_undo()
        ms = int(round(t * 1000)) + int(round(delta * 1000))
        self.lines[self.sel]["time"] = max(0, ms) / 1000.0
        self._update_row(self.sel)
        self._update_sel_time()

    def move_up(self) -> None:
        """上移选中行（与上一行交换；记入撤销，选中跟随）。"""
        if not self.lines or self.sel <= 0 or self.sel >= len(self.lines):
            return
        self._push_undo()
        i = self.sel
        self.lines[i - 1], self.lines[i] = self.lines[i], self.lines[i - 1]
        self._refresh_list()
        self._select(i - 1)

    def move_down(self) -> None:
        """下移选中行（与下一行交换；记入撤销，选中跟随）。"""
        if not self.lines or self.sel < 0 or self.sel >= len(self.lines) - 1:
            return
        self._push_undo()
        i = self.sel
        self.lines[i + 1], self.lines[i] = self.lines[i], self.lines[i + 1]
        self._refresh_list()
        self._select(i + 1)

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
            self.app.show_dialog("导入歌词", "无法读取文件。", kind="error")
            return
        # 保留导入文件中的翻译行（V1.11.0）
        self._kept_translation_lines = [
            ln.strip() for ln in content.splitlines()
            if _TRANSLATION_LINE_RE.match(ln)
        ]
        pairs = parse_lines(content)
        if not pairs:
            self.app.show_dialog("导入歌词", "文件中没有可用的歌词行。",
                                 kind="warning")
            return
        expand = False
        if not is_prefix_expanded([t for _, t in pairs]):
            expand = self.app.show_dialog(
                "导入歌词",
                "该文件不是逐字（前缀）歌词。\n"
                "是否转换为逐字歌词（按字展开）？",
                kind="question",
                buttons=(("是", "yes"), ("否", "no"))) == "yes"
        self._push_undo()
        if expand:
            self.lines = self._expand_pairs(pairs)
        else:
            self.lines = [{"text": t, "time": ts} for ts, t in pairs]
        self.sel = 0 if self.lines else -1
        self._refresh_list()

    def finish(self) -> None:
        """完成：未做更改时直接不保存退出；有更改时关闭前询问保存。"""
        if self._editing:
            return
        if not self.lines:
            self.hide()
            return
        # 内容与打开时一致（含“改过又撤销”）：直接退出，不询问不保存
        if (self._initial_lines is not None
                and self.lines == self._initial_lines):
            self.hide()
            return
        audio = self.app.audio_path
        if audio:
            name = os.path.splitext(os.path.basename(audio))[0] or "未命名"
            ans = self.app.show_dialog(
                "完成编辑",
                f"是否保存到歌曲同名歌词文件？\n{name}.lrc",
                kind="question",
                buttons=(("是", "yes"), ("否", "no"), ("取消", "cancel")))
            if ans == "cancel":
                return
            if ans == "yes" and not self._save_to_song(audio):
                return
        else:
            ans = self.app.show_dialog(
                "完成编辑",
                "当前没有播放歌曲，是否导出歌词到文件？",
                kind="question",
                buttons=(("是", "yes"), ("否", "no"), ("取消", "cancel")))
            if ans == "cancel":
                return
            if ans == "yes" and not self.export_file():
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
            self.app.show_dialog("导出歌词", f"写入失败：{e}", kind="error")
            return False
        return True

    # ------------------------------------------------------------------
    # 保存 / 导出辅助
    # ------------------------------------------------------------------
    def _to_lrc_text(self) -> str:
        """生成 LRC 文本（未打轴行占位 [00:00.000]）。

        V1.11.0：原文件的翻译行（[tr ...]）按时间戳插入保留，不丢失。
        """
        entries: list[tuple[float, int, str]] = []
        for line in self.lines:
            t = 0.0 if line["time"] is None else float(line["time"])
            entries.append((t, 0,
                            f"{fmt_lrc_time(line['time'])}{line['text']}"))
        for raw in self._kept_translation_lines:
            m = _TRANSLATION_LINE_RE.match(raw)
            t = (int(m.group(1)) * 60 + float(m.group(2))) if m else 0.0
            entries.append((t, 1, raw))
        entries.sort(key=lambda e: (e[0], e[1]))
        return "".join(e[2] + "\n" for e in entries)

    def _save_to_song(self, audio_path: str) -> bool:
        """保存到歌曲同名 .lrc 并刷新主界面歌词。返回是否成功。"""
        target = os.path.splitext(audio_path)[0] + ".lrc"
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(self._to_lrc_text())
        except OSError as e:
            self.app.show_dialog("保存歌词", f"写入失败：{e}", kind="error")
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
            font=self.text_font, relief="flat",
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
        text = strip_invisible(self.entry.get())
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
            self._refresh_convert_btn()
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
        if not self._editing and not self._time_edit_active:
            self.prev_line()
        return "break"

    def _key_next(self, _event=None) -> str:
        if not self._editing and not self._time_edit_active:
            self.next_line()
        return "break"

    def _key_stamp(self, _event=None) -> str:
        if not self._editing and not self._time_edit_active:
            self.stamp()
        return "break"

    def _key_undo(self, _event=None) -> str:
        if not self._editing and not self._time_edit_active:
            self.undo()
        return "break"

    def _key_redo(self, _event=None) -> str:
        if not self._editing and not self._time_edit_active:
            self.redo()
        return "break"

    def _key_finish(self, _event=None) -> str:
        if not self._editing and not self._time_edit_active:
            self.finish()
        return "break"
