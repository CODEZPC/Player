"""音乐播放器主应用 —— UI 布局、交互逻辑、歌词同步。"""

import os
import random
import threading
import time
from bisect import bisect_right
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

from lrc_parser import LrcParser
from audio_engine import AudioEngine
from cover_utils import extract_cover_art, cover_to_tk_image
from utils import (
    format_time,
    read_text_file,
    get_duration_mutagen,
    make_progress_bar,
    status_sep,
    bind_tooltip,
    resource_path,
)


# ===========================================================================
# 全局常量
# ===========================================================================

BG_COLOR = "#23272E"
FG_COLOR = "#C8C8C8"
ACCENT_COLOR = "#6FA3FF"
SUBTLE_COLOR = "#3A3F46"
BUTTON_WIDTH = 11
TICK_INTERVAL_MS = 10

PLAY_MODES = [
    ("列表循环", "loop_all"),
    ("单曲循环", "loop_one"),
    ("仅一首", "single"),
    ("随机播放", "shuffle"),
]

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac"}


# ===========================================================================
# LrcPlayerApp
# ===========================================================================

class LrcPlayerApp:
    """歌词播放器主应用。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        try:
            self.root.iconbitmap(resource_path("MP.ico"))
        except Exception:
            pass  # 图标文件缺失时不阻塞启动
        self.root.title("Player PRO V1.3.2")
        self.root.configure(bg=BG_COLOR)
        self.root.minsize(820, 250)
        self.root.resizable(True, True)

        # ---- 音频引擎 ----
        self.engine = AudioEngine()

        # ---- 音频状态 ----
        self.audio_path: str | None = None
        self.lrc_path: str | None = None
        self.duration: float | None = None

        # ---- 歌曲列表 ----
        self.audio_items: list[dict[str, str | int | None]] = []
        self.audio_root: str | None = None
        self.current_song_index: int | None = None
        self.viewed_song_index: int | None = None
        self._bg_cache_index = 0
        self._bg_cache_gen = 0
        self._first_scan_done = False
        self._info_req_id = 0
        self.interlude_items: list[dict] = []
        self._cover_photo = None
        self._tooltip = None  # 悬停提示窗口
        self._scan_total = 0
        self._scan_done = 0
        self._il_move_mode = False
        self._move_locked_index: int | None = None
        self._clear_confirm_active = False
        self._clear_confirm_timer: str | None = None

        # ---- 歌词浮层 ----
        self._lyric_overlay = None  # 全窗口歌词浮层 Frame
        self._overlay_cover_photo = None
        self._overlay_lyric_vars: list[tk.StringVar] = []

        # ---- 播放状态 ----
        self.is_playing = False
        self.is_paused = False
        self.play_started_at: float | None = None
        self.base_time = 0.0

        # ---- 歌词 ----
        self.lrc_lines: list[tuple[float, str]] = []
        self.lrc_times: list[float] = []
        self.current_lrc_index = -1

        # ---- UI 状态 ----
        self.user_seeking = False
        self.always_on_top = False
        self.play_mode_index = 0
        self.play_mode = PLAY_MODES[self.play_mode_index][1]

        # ---- 字体 ----
        self.title_font = self._pick_font("汉仪文黑-85W", 18)
        self.lyric_font = self._pick_font("汉仪文黑-85W", 13)
        self.info_font = self._pick_font("Jetbrains Mono", 11)
        self.button_font = self._pick_font("汉仪文黑-85W", 12)
        self.button_font_sm = self._pick_font("汉仪文黑-85W", 10)
        self.overlay_name_font = self._pick_font("汉仪文黑-85W", 20)
        self.overlay_artist_font = self._pick_font("汉仪文黑-85W", 14)
        self.overlay_lyric_big_font = self._pick_font("汉仪文黑-85W", 24)
        self.overlay_lyric_small_font = self._pick_font("汉仪文黑-85W", 13)

        # ---- 构建 ----
        self._configure_style()
        self._build_ui()

        if not self.engine.ready:
            self._disable_audio_controls()
            messagebox.showwarning("音频后端", "未安装 pygame，音频播放不可用。")

        self.root.after(TICK_INTERVAL_MS, self._tick)
        self._setup_keyboard_shortcuts()

    # ==================================================================
    # 字体 & 样式
    # ==================================================================

    def _pick_font(self, family: str, size: int, weight: str = "normal") -> tkfont.Font:
        """查找可用字体，降级到系统默认。"""
        available = list(tkfont.families(self.root))
        lookup = {name.lower(): name for name in available}
        family = lookup.get(family.lower(), family)
        if family not in available:
            family = "Segoe UI" if "Segoe UI" in lookup.values() else "TkDefaultFont"
        return tkfont.Font(family=family, size=size, weight=weight)

    def _configure_style(self) -> None:
        """配置 ttk 控件主题与样式。"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "LRC.Horizontal.TScale",
            troughcolor=SUBTLE_COLOR,
            background=BG_COLOR,
        )
        style.configure(
            "LRC.Vertical.TScrollbar",
            troughcolor=BG_COLOR,
            background=SUBTLE_COLOR,
            bordercolor=BG_COLOR,
            arrowcolor=FG_COLOR,
            relief="flat",
        )
        style.map(
            "LRC.Vertical.TScrollbar",
            background=[("active", ACCENT_COLOR), ("!active", SUBTLE_COLOR)],
            arrowcolor=[("active", BG_COLOR), ("!active", FG_COLOR)],
        )

    # ==================================================================
    # UI 构建
    # ==================================================================

    def _build_ui(self) -> None:
        """左右双栏布局：左侧主控区，右侧封面+插播列表+按钮区。"""
        outer = tk.Frame(self.root, bg=BG_COLOR)
        outer.pack(fill="both", expand=True, padx=18, pady=8)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=0)
        outer.rowconfigure(0, weight=1)

        # ---- 左栏 ----
        left_col = tk.Frame(outer, bg=BG_COLOR)
        left_col.grid(row=0, column=0, sticky="nsew")

        self._build_header(left_col)
        self._build_button_rows(left_col)

        # 拖拽取消条（初始隐藏，拖动进度条时显示）
        self._cancel_frame = tk.Frame(left_col, bg="#6B1010", height=40)
        self._cancel_label = tk.Label(
            self._cancel_frame, text="拖动到此处以取消",
            bg="#6B1010", fg="#FF5555", font=self.title_font)
        self._cancel_label.pack(expand=True)

        self._build_progress_bar(left_col)

        self.bottom_frame = tk.Frame(left_col, bg=BG_COLOR)
        self._build_bottom_panels(self.bottom_frame)

        # ---- 右栏（固定宽度）----
        right_col = tk.Frame(outer, bg=BG_COLOR, width=260)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right_col.grid_propagate(False)
        self._build_right_column(right_col)

        # ---- 状态栏 ----
        self._build_status_bar()

    def _build_status_bar(self) -> None:
        """窗口底部状态栏：程序状态、音频信息、音量、列表统计。"""
        bar = tk.Frame(self.root, bg=SUBTLE_COLOR, height=24)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        status_font = tkfont.Font(family="Segoe UI", size=10)
        lbl_cfg = dict(bg=SUBTLE_COLOR, fg=FG_COLOR, font=status_font,
                       anchor="w", padx=6)

        # 程序状态 + 文字进度条（w=210）
        prog_frame = tk.Frame(bar, bg=SUBTLE_COLOR, width=210, height=24)
        prog_frame.pack(side="left")
        prog_frame.pack_propagate(False)
        self._prog_var = tk.StringVar(value="就绪")
        prog_lbl = tk.Label(prog_frame, textvariable=self._prog_var, **lbl_cfg)
        prog_lbl.pack(fill="both", expand=True)
        bind_tooltip(prog_lbl, self._prog_var, self, status_font)

        status_sep(bar)

        # 音频设备（w=170）
        audio_frame = tk.Frame(bar, bg=SUBTLE_COLOR, width=170, height=24)
        audio_frame.pack(side="left")
        audio_frame.pack_propagate(False)
        self._audio_var = tk.StringVar(value=self._read_audio_info())
        audio_lbl = tk.Label(audio_frame, textvariable=self._audio_var, **lbl_cfg)
        audio_lbl.pack(fill="both", expand=True)
        bind_tooltip(audio_lbl, self._audio_var, self, status_font)

        status_sep(bar)

        # 歌曲列表统计（w=130）
        list_frame = tk.Frame(bar, bg=SUBTLE_COLOR, width=130, height=24)
        list_frame.pack(side="left")
        list_frame.pack_propagate(False)
        self._liststat_var = tk.StringVar(value="共 0 首")
        list_lbl = tk.Label(list_frame, textvariable=self._liststat_var, **lbl_cfg)
        list_lbl.pack(fill="both", expand=True)
        bind_tooltip(list_lbl, self._liststat_var, self, status_font)

        status_sep(bar)

        # 插播统计（w=85）
        il_frame = tk.Frame(bar, bg=SUBTLE_COLOR, width=85, height=24)
        il_frame.pack(side="left")
        il_frame.pack_propagate(False)
        self._ilst_var = tk.StringVar(value="插播: 0")
        il_lbl = tk.Label(il_frame, textvariable=self._ilst_var, **lbl_cfg)
        il_lbl.pack(fill="both", expand=True)
        bind_tooltip(il_lbl, self._ilst_var, self, status_font)

        # 初始刷新
        self._refresh_status_bar()

    # ==================================================================
    # 键盘快捷键（Alt + 字母 / Space / F11）
    # ==================================================================

    def _setup_keyboard_shortcuts(self) -> None:
        """Alt+字母永久绑定 + Space/F11 + 字母提示标签。"""
        self._key_tips: list = []
        self._tips_visible = False
        tip_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")

        shortcuts = [
            (self.open_file_btn,    "O", self._open_file),
            (self.open_folder_btn,  "F", self._scan_folder),
            (self.prev_btn,         "A", self._prev_track),
            (self.next_btn,         "D", self._next_track),
            (self.mode_btn,         "M", self._toggle_play_mode),
            (self.back_10s_btn,     "Q", self._seek_back_10s),
            (self.forward_10s_btn,  "E", self._seek_forward_10s),
            (self.stop_btn,         "S", self._stop),
            (self.topmost_btn,      "T", self._toggle_topmost),
        ]

        for btn, key, cmd in shortcuts:
            # 提示标签
            tip = tk.Label(self.root, text=key, bg="#333333", fg="#FFFFFF",
                           font=tip_font, padx=3, pady=1)
            self._key_tips.append((btn, key, cmd, tip))
            # 永久绑定 Alt+Key → 始终可用
            k = key.lower()
            self.root.bind(f"<Alt-Key-{k}>",
                           lambda e, c=cmd: self._on_alt_shortcut(c))

        # 也尝试绑定 Alt 单按 → 显示提示（可能部分平台不触发，不影响快捷键）
        self.root.bind("<Alt_L>", lambda e: self._show_key_tips())
        self.root.bind("<Alt_R>", lambda e: self._show_key_tips())

        # Space → 播放/暂停（始终可用）
        self.root.bind("<space>",
                       lambda e: (self._toggle_play_pause(), "break")[1])
        # F11 → 全屏
        self.root.bind("<F11>",
                       lambda e: (self._toggle_fullscreen(), "break")[1])
        # Escape → 隐藏提示
        self.root.bind("<Escape>", lambda e: self._hide_key_tips())

    def _on_alt_shortcut(self, cmd) -> str:
        """Alt+Key 触发：先显示提示（若未显示），执行命令，重置自动隐藏计时。"""
        if not self._tips_visible:
            self._show_key_tips()
        cmd()
        self._reset_tip_timer()
        return "break"

    def _show_key_tips(self) -> None:
        """在按钮下方显示快捷键字母提示。"""
        if self._tips_visible:
            return
        self._tips_visible = True
        for btn, key, cmd, tip in self._key_tips:
            x = btn.winfo_rootx() - self.root.winfo_rootx() + btn.winfo_width() // 2 - 8
            y = btn.winfo_rooty() - self.root.winfo_rooty() + btn.winfo_height()
            tip.place(x=x, y=y)
            tip.lift()
        self._reset_tip_timer()

    def _hide_key_tips(self) -> None:
        """隐藏快捷键提示。"""
        if not self._tips_visible:
            return
        self._tips_visible = False
        for _, key, cmd, tip in self._key_tips:
            tip.place_forget()
        self._cancel_tip_timer()

    def _reset_tip_timer(self) -> None:
        """重置 3 秒自动隐藏计时。"""
        self._cancel_tip_timer()
        self._tip_timer = self.root.after(3000, self._hide_key_tips)

    def _cancel_tip_timer(self) -> None:
        if hasattr(self, '_tip_timer') and self._tip_timer is not None:
            self.root.after_cancel(self._tip_timer)
            self._tip_timer = None

    def _toggle_fullscreen(self) -> None:
        """切换全屏状态。"""
        state = not self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", state)

    def _build_header(self, parent: tk.Frame) -> None:
        """顶部：当前歌词行 + 音视频信息行。"""
        header = tk.Frame(parent, bg=BG_COLOR)
        header.pack(fill="x")

        self.now_line_var = tk.StringVar(value="未加载歌词")
        now_label = tk.Label(
            header,
            textvariable=self.now_line_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.title_font,
            anchor="w",
        )
        now_label.pack(fill="x")

        self.info_var = tk.StringVar(value="音频: - | 歌词: -")
        info_label = tk.Label(
            header,
            textvariable=self.info_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.info_font,
            anchor="w",
        )
        info_label.pack(fill="x", pady=(4, 6))

    def _build_button_rows(self, parent: tk.Frame) -> None:
        """两行按钮，每行 5 个。包裹在容器中供取消条覆盖。"""
        self._btn_container = tk.Frame(parent, bg=BG_COLOR)
        self._btn_container.pack(fill="x")
        btn_cfg = dict(
            bg=SUBTLE_COLOR,
            fg=FG_COLOR,
            font=self.button_font,
            width=BUTTON_WIDTH,
            activebackground=ACCENT_COLOR,
            activeforeground=BG_COLOR,
            relief="flat",
            padx=10,
            pady=6,
        )

        # ---- 第一行 ----
        row1 = tk.Frame(self._btn_container, bg=BG_COLOR)
        row1.pack(fill="x")

        self.open_file_btn = tk.Button(
            row1, text="打开文件", command=self._open_file, **btn_cfg)
        self.open_file_btn.pack(side="left")

        self.prev_btn = tk.Button(
            row1, text="上一曲", command=self._prev_track, **btn_cfg)
        self.prev_btn.pack(side="left", padx=(8, 0))

        self.play_pause_btn = tk.Button(
            row1, text="播放", command=self._toggle_play_pause, **btn_cfg)
        self.play_pause_btn.pack(side="left", padx=(8, 0))

        self.next_btn = tk.Button(
            row1, text="下一曲", command=self._next_track, **btn_cfg)
        self.next_btn.pack(side="left", padx=(8, 0))

        mode_label = PLAY_MODES[self.play_mode_index][0]
        self.mode_btn = tk.Button(
            row1, text=f"模式: {mode_label}", command=self._toggle_play_mode, **btn_cfg)
        self.mode_btn.pack(side="left", padx=(8, 0))

        # ---- 第二行 ----
        row2 = tk.Frame(self._btn_container, bg=BG_COLOR)
        row2.pack(fill="x", pady=(6, 0))

        self.open_folder_btn = tk.Button(
            row2, text="打开文件夹", command=self._scan_folder, **btn_cfg)
        self.open_folder_btn.pack(side="left")

        self.back_10s_btn = tk.Button(
            row2, text="后退10s", command=self._seek_back_10s, **btn_cfg)
        self.back_10s_btn.pack(side="left", padx=(8, 0))

        self.stop_btn = tk.Button(
            row2, text="停止", command=self._stop, **btn_cfg)
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.forward_10s_btn = tk.Button(
            row2, text="前进10s", command=self._seek_forward_10s, **btn_cfg)
        self.forward_10s_btn.pack(side="left", padx=(8, 0))

        self.topmost_btn = tk.Button(
            row2, text="置顶: 关", command=self._toggle_topmost, **btn_cfg)
        self.topmost_btn.pack(side="left", padx=(8, 0))

    def _build_progress_bar(self, parent: tk.Frame) -> None:
        """进度条 + 右侧时间标签。"""
        progress_row = tk.Frame(parent, bg=BG_COLOR)
        progress_row.pack(fill="x", pady=(8, 4))

        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(
            progress_row,
            style="LRC.Horizontal.TScale",
            orient="horizontal",
            from_=0.0,
            to=100.0,
            variable=self.seek_var,
            command=self._on_seek_changed,
        )
        self.seek_scale.pack(side="left", fill="x", expand=True)
        self.seek_scale.bind("<ButtonPress-1>", self._on_seek_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        self.time_var = tk.StringVar(value="00:00 / --:--")
        time_label = tk.Label(
            progress_row,
            textvariable=self.time_var,
            bg=BG_COLOR,
            fg=FG_COLOR,
            font=self.info_font,
            padx=10,
        )
        time_label.pack(side="right")

    def _build_bottom_panels(self, parent: tk.Frame) -> None:
        """底部纵向布局：上方歌曲列表（全宽），下方歌曲信息条（全宽）+ 播放按钮。"""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)
        parent.rowconfigure(2, weight=0)

        # ---- 歌曲列表（全宽，占据大部分高度）----
        list_panel = tk.Frame(parent, bg=BG_COLOR)
        list_panel.grid(row=0, column=0, sticky="nsew")

        list_label = tk.Label(
            list_panel, text="歌曲列表", bg=BG_COLOR, fg=FG_COLOR,
            font=self.info_font, anchor="w")
        list_label.pack(anchor="w")

        list_inner = tk.Frame(list_panel, bg=BG_COLOR)
        list_inner.pack(fill="both", expand=True)

        self.song_list = tk.Listbox(
            list_inner,
            bg=BG_COLOR, fg=FG_COLOR, font=self.info_font,
            selectbackground=ACCENT_COLOR, selectforeground=BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False,
        )
        self.song_list.pack(side="left", fill="both", expand=True)
        self.song_list.bind("<<ListboxSelect>>", self._on_song_select)

        song_scroll = ttk.Scrollbar(
            list_inner, command=self.song_list.yview, style="LRC.Vertical.TScrollbar")
        song_scroll.pack(side="right", fill="y")
        self.song_list.config(yscrollcommand=song_scroll.set)

        # ---- 分隔线 ----
        sep = tk.Frame(parent, bg=SUBTLE_COLOR, height=1)
        sep.grid(row=1, column=0, sticky="ew", pady=(4, 2))

        # ---- 歌曲信息条（全宽，grid 流式布局）----
        info_outer = tk.Frame(parent, bg=BG_COLOR)
        info_outer.grid(row=2, column=0, sticky="ew")

        # 列宽策略：第 0 列（文件名/专辑）弹性伸缩，右边三栏定宽
        info_outer.columnconfigure(0, weight=1)
        # 右边三栏的固定列宽（像素）
        _FIXED_COL_WIDTHS = {1: 120, 2: 100, 3: 160}

        self.song_info_labels: dict[str, tk.StringVar] = {}
        self._song_info_val_widgets: dict[str, tk.Label] = {}
        info_fields = [
            ("文件名", 0, 0), ("时长", 0, 1), ("格式", 0, 2), ("文件大小", 0, 3),
            ("专辑", 1, 0), ("音轨号", 1, 1), ("歌词", 1, 2),
        ]
        for field, row, col in info_fields:
            seg = tk.Frame(info_outer, bg=BG_COLOR)
            seg.grid(row=row, column=col, sticky="w",
                     padx=(0 if col == 0 else 12, 0), pady=1)

            key_lbl = tk.Label(
                seg, text=f"{field}:", bg=BG_COLOR, fg=ACCENT_COLOR,
                font=self.info_font, anchor="w")
            key_lbl.pack(side="left")

            var = tk.StringVar(value="-")
            val_lbl = tk.Label(
                seg, textvariable=var, bg=BG_COLOR, fg=FG_COLOR,
                font=self.info_font, anchor="w")
            val_lbl.pack(side="left", padx=(2, 0))
            self.song_info_labels[field] = var
            self._song_info_val_widgets[field] = val_lbl

            # 右边三栏固定宽度，超出裁剪
            if col > 0:
                w = _FIXED_COL_WIDTHS.get(col, 75)
                seg.config(width=w, height=22)
                seg.pack_propagate(False)

            # 悬停显示完整文本
            bind_tooltip(val_lbl, var, self, self.info_font)

        # 初始化空列表
        self._refresh_song_list()

    # ==================================================================
    # 右侧栏：封面 + 插播列表 + 按钮区
    # ==================================================================

    def _build_right_column(self, parent: tk.Frame) -> None:
        """构建右侧栏：专辑封面、插播列表（可伸展）、按钮区（底部固定）。"""
        btn_cfg = dict(
            bg=SUBTLE_COLOR, fg=FG_COLOR, font=self.button_font,
            activebackground=ACCENT_COLOR, activeforeground=BG_COLOR,
            relief="flat", padx=6, pady=4,
        )

        # ---- 按钮区 A（状态感知：1行全宽 + 2列等宽固定）----
        btn_frame = tk.Frame(parent, bg=BG_COLOR)
        btn_frame.pack(side="bottom", fill="x")

        self._btn_a = tk.Button(
            btn_frame, text="播放此首", command=self._play_viewed_song, **btn_cfg)
        self._btn_a.pack(fill="x", pady=1)

        row = tk.Frame(btn_frame, bg=BG_COLOR)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)

        self._btn_b = tk.Button(
            row, text="添加到插播", command=self._interlude_add,
            width=7, **btn_cfg)
        self._btn_b.grid(row=0, column=0, sticky="ew", padx=(0, 2), pady=1)

        self._btn_c = tk.Button(
            row, text="清除插播", command=self._il_clear_confirm,
            width=7, **btn_cfg)
        self._btn_c.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=1)

        # ---- 插播列表（可伸展，填充剩余空间）----
        il_frame = tk.Frame(parent, bg=BG_COLOR)
        il_frame.pack(side="bottom", fill="both", expand=True, pady=(0, 6))

        il_label = tk.Label(
            il_frame, text="插播列表", bg=BG_COLOR, fg=FG_COLOR,
            font=self.info_font, anchor="w")
        il_label.pack(anchor="w")

        self.interlude_list = tk.Listbox(
            il_frame,
            bg=BG_COLOR, fg=FG_COLOR, font=self.info_font,
            selectbackground=ACCENT_COLOR, selectforeground=BG_COLOR,
            highlightthickness=0, relief="flat", activestyle="none",
            exportselection=False,
        )
        self.interlude_list.pack(side="left", fill="both", expand=True)
        self.interlude_list.bind("<FocusOut>", self._on_interlude_focus_out)

        # 插播列表选择变化时刷新按钮，移动模式下锁定选中项
        self.interlude_list.bind("<<ListboxSelect>>",
                                lambda e: self._on_interlude_select())

        # ---- 专辑封面（固定 220×220，与图片尺寸一致，避免灰边）----
        cover_frame = tk.Frame(parent, bg=BG_COLOR, height=220, width=220)
        cover_frame.pack(side="bottom", fill="x", pady=(0, 8))
        cover_frame.pack_propagate(False)
        self.cover_label = tk.Label(
            cover_frame, bg=BG_COLOR, fg=FG_COLOR,
            text="专辑封面", font=self.info_font,
        )
        self.cover_label.pack(fill="both", expand=True)
        # 点击封面打开全窗口歌词浮层
        self.cover_label.bind("<Button-1>", self._open_lyric_overlay)

    # ==================================================================
    # 专辑封面
    # ==================================================================

    def _update_cover(self, path: str | None) -> None:
        """根据当前播放的音频文件更新专辑封面。"""
        if not path:
            self._show_default_cover()
            self._refresh_overlay_header()
            return
        data = extract_cover_art(path)
        photo = cover_to_tk_image(data) if data else None
        if photo:
            self._cover_photo = photo
            self.cover_label.config(image=photo, text="")
        else:
            self._show_default_cover()
        self._refresh_overlay_header()

    def _show_default_cover(self) -> None:
        """显示默认封面占位。"""
        self._cover_photo = None
        self.cover_label.config(image="", text="无封面")

    # ==================================================================
    # 插播列表操作
    # ==================================================================

    def _refresh_il_buttons(self) -> None:
        """根据插播列表选中状态和移动模式刷新按钮文字与命令。"""
        # 先重置清除按钮状态（模式切换时恢复）
        self._reset_clear_button()

        if self._il_move_mode:
            self._btn_a.config(text="确认", command=self._il_move_confirm)
            self._btn_b.config(text="上移", command=self._il_move_up)
            self._btn_c.config(text="下移", command=self._il_move_down)
            return

        sel = self.interlude_list.curselection()
        if sel:
            self._btn_a.config(text="插入到插播", command=self._interlude_insert)
            self._btn_b.config(text="从插播移除", command=self._interlude_remove)
            self._btn_c.config(text="移动位置", command=self._il_enter_move_mode)
        else:
            self._btn_a.config(text="播放此首", command=self._play_viewed_song)
            self._btn_b.config(text="添加到插播", command=self._interlude_add)
            self._btn_c.config(text="清除插播", command=self._il_clear_confirm)

    def _reset_clear_button(self) -> None:
        """将清除按钮恢复为默认状态。"""
        if self._clear_confirm_timer:
            self.root.after_cancel(self._clear_confirm_timer)
            self._clear_confirm_timer = None
        self._clear_confirm_active = False
        self._btn_c.config(text="清除插播", fg=FG_COLOR,
                          font=self.button_font, width=7,
                          command=self._il_clear_confirm)

    # ---- 插播基本操作 ----

    def _refresh_interlude_list(self) -> None:
        """刷新插播列表显示。"""
        self.interlude_list.delete(0, "end")
        for item in self.interlude_items:
            name = os.path.basename(item.get("path") or "")
            self.interlude_list.insert("end", name)
        self._refresh_status_bar()

    def _on_interlude_select(self) -> None:
        """插播列表选择变化：移动模式下锁定选中项，否则刷新按钮。"""
        if self._il_move_mode and self._move_locked_index is not None:
            # 移动模式下强制选中锁定项，禁止点击其他歌曲
            self.interlude_list.selection_clear(0, "end")
            self.interlude_list.selection_set(self._move_locked_index)
        self._refresh_il_buttons()

    def _on_interlude_focus_out(self, _event: tk.Event) -> None:
        """插播列表失焦后清空选择（移动模式下不处理）。"""
        if self._il_move_mode:
            return
        self.interlude_list.selection_clear(0, "end")
        self._refresh_il_buttons()

    def _interlude_add(self) -> None:
        """将当前查看的歌曲添加到插播列表末尾。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        item = dict(self.audio_items[self.viewed_song_index])
        self.interlude_items.append(item)
        self._refresh_interlude_list()

    def _interlude_insert(self) -> None:
        """将当前查看的歌曲插入到插播列表选中位置之前。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        sel = self.interlude_list.curselection()
        pos = sel[0] if sel else len(self.interlude_items)
        item = dict(self.audio_items[self.viewed_song_index])
        self.interlude_items.insert(pos, item)
        self._refresh_interlude_list()

    def _interlude_remove(self) -> None:
        """移除插播列表中选中的条目（无确认弹窗）。"""
        sel = self.interlude_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self.interlude_items):
            del self.interlude_items[idx]
            self._refresh_interlude_list()
            self._refresh_il_buttons()

    # ---- 清除插播队列（二次确认）----

    def _il_clear_confirm(self) -> None:
        """清除插播队列：点击→亮红确认，再点击→清除，3秒自动恢复。"""
        if self._clear_confirm_active:
            self.interlude_items.clear()
            self._refresh_interlude_list()
            self._reset_clear_button()
            self._refresh_il_buttons()
            return
        self._clear_confirm_active = True
        self._btn_c.config(text="确定？再点", fg="#FF4444",
                          font=self.button_font, width=7,
                          command=self._il_clear_confirm)
        self._clear_confirm_timer = self.root.after(
            3000, self._reset_clear_button)

    # ---- 移动模式 ----

    def _il_enter_move_mode(self) -> None:
        """进入移动模式：锁定当前选中项，禁用失焦清除。"""
        sel = self.interlude_list.curselection()
        if not sel:
            return
        self._il_move_mode = True
        self._move_locked_index = sel[0]
        self._reset_clear_button()
        self._refresh_il_buttons()

    def _il_move_confirm(self) -> None:
        """确认移动，退出移动模式，恢复失焦清除。"""
        self._il_move_mode = False
        self._move_locked_index = None
        self.interlude_list.selection_clear(0, "end")
        self._refresh_il_buttons()

    def _il_move_up(self) -> None:
        """移动模式下将锁定项上移。"""
        if self._move_locked_index is None or self._move_locked_index <= 0:
            return
        items = self.interlude_items
        idx = self._move_locked_index
        items[idx], items[idx - 1] = items[idx - 1], items[idx]
        self._move_locked_index = idx - 1
        self._refresh_interlude_list()
        self.interlude_list.selection_set(self._move_locked_index)

    def _il_move_down(self) -> None:
        """移动模式下将锁定项下移。"""
        if (self._move_locked_index is None
                or self._move_locked_index >= len(self.interlude_items) - 1):
            return
        items = self.interlude_items
        idx = self._move_locked_index
        items[idx], items[idx + 1] = items[idx + 1], items[idx]
        self._move_locked_index = idx + 1
        self._refresh_interlude_list()
        self.interlude_list.selection_set(self._move_locked_index)

    # ==================================================================
    # 音频控件状态
    # ==================================================================

    def _disable_audio_controls(self) -> None:
        """音频后端不可用时禁用相关按钮。"""
        for btn in (self.play_pause_btn, self.stop_btn):
            btn.config(state="disabled")

    # ==================================================================
    # 状态栏更新
    # ==================================================================

    def _read_audio_info(self) -> str:
        """读取音频输出设备/参数信息。"""
        if not self.engine.ready:
            return "音频: 未就绪"
        try:
            import pygame
            info = pygame.mixer.get_init()
            if info:
                freq, fmt, ch = info
                ch_name = {1: "Mono", 2: "Stereo"}.get(ch, f"{ch}ch")
                bits = 16 if fmt < 0 else 8
                return f"音频: {freq}Hz {bits}bit {ch_name}"
        except Exception:
            pass
        return "音频: 就绪"

    def _refresh_status_bar(self) -> None:
        """刷新状态栏所有字段（主线程安全）。"""
        # 歌曲统计
        if self.audio_items:
            total = len(self.audio_items)
            cur = (self.current_song_index or 0) + 1 if self.current_song_index is not None else 0
            self._liststat_var.set(f"共{total}首" + (f" 第{cur}首" if cur else ""))
        else:
            self._liststat_var.set("共 0 首")

        # 插播统计
        n = len(self.interlude_items)
        self._ilst_var.set(f"插播: {n}首" if n else "插播: 0")

    def _set_scan_progress(self, done: int, total: int) -> None:
        """更新扫描进度（主线程安全）。"""
        pct = done / total * 100 if total > 0 else 0
        bar = make_progress_bar(pct)
        self._prog_var.set(f"解析中 {bar}")

    def _set_status_ready(self) -> None:
        """设置状态为就绪。"""
        self._prog_var.set("就绪")
        self._refresh_status_bar()

    # ==================================================================
    # 歌曲列表
    # ==================================================================

    def _scan_folder(self) -> None:
        """扫描文件夹：同步快扫显示列表，子线程后台缓存时长。"""
        folder = filedialog.askdirectory(title="选择歌曲文件夹")
        if not folder:
            return
        self.audio_root = folder

        # 同步快扫：walk + mutagen 元数据（不含 pygame.Sound，快速完成）
        self.audio_items = self._quick_scan_files(folder)
        self.current_song_index = None
        self.viewed_song_index = None
        self.song_list.selection_clear(0, "end")
        self._refresh_song_list()
        self._clear_song_info()

        # 显示底部面板 + 首次扫描调整窗口尺寸并锁定最小宽度
        self.bottom_frame.pack(fill="both", expand=True, pady=(6, 0))
        if not self._first_scan_done:
            self._first_scan_done = True
            self.root.geometry("1010x650")
            self.root.minsize(1010, 650)

        # 启动后台缓存并更新进度状态
        self._scan_total = len(self.audio_items)
        self._scan_done = 0
        self._set_scan_progress(0, self._scan_total)

        # 子线程后台缓存时长（不阻塞主线程）
        self._bg_cache_gen += 1
        gen = self._bg_cache_gen
        threading.Thread(
            target=self._bg_cache_thread, args=(gen,), daemon=True
        ).start()

    def _quick_scan_files(self, folder: str) -> list[dict[str, str | int | None]]:
        """快速扫描：只收集文件路径和元数据，不计算时长。"""
        items: list[dict[str, str | int | None]] = []
        for root, _, files in os.walk(folder):
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue
                path = os.path.join(root, name)
                lrc_path = self._find_lrc_for_audio(path)
                track_number, album_name, artist_name = self._get_metadata(path)
                rel = os.path.relpath(path, folder)
                items.append({
                    "path": path,
                    "lrc": lrc_path,
                    "display": rel,
                    "track": track_number,
                    "album": album_name,
                    "artist": artist_name,
                    "duration": None,
                })
        items.sort(key=self._song_sort_key)
        return items

    def _bg_cache_thread(self, generation: int) -> None:
        """后台子线程：用 mutagen 获取时长，不触碰 pygame 避免音频撕裂。"""
        items_snapshot = self.audio_items
        total = len(items_snapshot)
        for i, item in enumerate(items_snapshot):
            if generation != self._bg_cache_gen:
                return
            path = item.get("path")
            if path:
                dur = get_duration_mutagen(path)
                item["duration"] = dur
            self.root.after(0, lambda idx=i, d=item.get("duration"):
                           self._on_duration_cached(idx, d))
            # 更新进度
            self._scan_done = i + 1
            self.root.after(0, lambda d=i + 1, t=total:
                           self._set_scan_progress(d, t))
        # 全部完成
        self.root.after(0, self._set_status_ready)

    def _on_duration_cached(self, index: int, duration: float | None) -> None:
        """主线程回调：更新信息面板中的时长（若仍在查看该歌曲）。"""
        if self.viewed_song_index != index:
            return
        if "时长" in self.song_info_labels:
            self.song_info_labels["时长"].set(
                format_time(duration) if duration else "未知")

    @staticmethod
    def _song_sort_key(item: dict) -> tuple:
        """排序：目录层级 → 专辑名称 → 音轨号 → 文件名。"""
        display = item.get("display") or ""
        # 提取目录层级（根目录文件优先，子目录按名称排序）
        dir_path = os.path.dirname(display).replace("\\", "/")
        if dir_path in ("", "."):
            dir_parts = ()
        else:
            dir_parts = tuple(dir_path.split("/"))
        album = (item.get("album") or "").lower()
        track = item.get("track")
        track_key = track if isinstance(track, int) and track > 0 else 999999
        filename = os.path.basename(display).lower()
        return (dir_parts, album, track_key, filename)

    @staticmethod
    def _get_metadata(path: str) -> tuple[int | None, str, str]:
        """从音频元数据中读取音轨号（整数）、专辑名和歌手名。

        Returns:
            (track_number, album_name, artist_name) —— track_number 为 None 表示读取失败。
        """
        try:
            from mutagen import File as MutagenFile
        except Exception:
            return None, "", ""
        try:
            audio = MutagenFile(path, easy=True)
        except Exception:
            return None, "", ""
        if not audio or not audio.tags:
            return None, "", ""
        import re
        tags = {k.lower(): v for k, v in audio.tags.items()}
        # 读取专辑
        album = ""
        album_values = tags.get("album")
        if album_values:
            album = str(album_values[0]) if isinstance(album_values, (list, tuple)) else str(album_values)
        # 读取歌手
        artist = ""
        artist_values = tags.get("artist")
        if artist_values:
            artist = str(artist_values[0]) if isinstance(artist_values, (list, tuple)) else str(artist_values)
        # 读取音轨号
        for key in ("tracknumber", "track", "trck"):
            if key not in tags:
                continue
            values = tags.get(key)
            if not values:
                continue
            raw = str(values[0]) if isinstance(values, (list, tuple)) else str(values)
            match = re.search(r"\d+", raw)
            if match:
                try:
                    return int(match.group(0)), album, artist
                except ValueError:
                    return None, album, artist
        return None, album, artist

    def _refresh_song_list(self) -> None:
        """刷新 Listbox 中的歌曲显示。"""
        self.song_list.delete(0, "end")
        if not self.audio_items:
            self.song_list.insert("end", "尚未扫描歌曲")
            return
        for item in self.audio_items:
            self.song_list.insert("end", item.get("display") or "")
        self._refresh_status_bar()

    # ==================================================================
    # 歌曲列表交互
    # ==================================================================

    def _on_song_select(self, _event: tk.Event) -> None:
        """单击歌曲列表项 → 仅更新右侧歌曲信息面板，不切歌。"""
        if not self.audio_items:
            return
        selection = self.song_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.audio_items):
            return
        self.viewed_song_index = index
        self._show_song_info_for_index(index)

    # ==================================================================
    # 歌曲信息面板
    # ==================================================================

    def _show_song_info_for_index(self, index: int) -> None:
        """点击歌曲 → 立即显示"正在加载…"，子线程加载信息，支持取消。"""
        if index < 0 or index >= len(self.audio_items):
            self._clear_song_info()
            return

        # 立即标记"正在加载"
        for var in self.song_info_labels.values():
            var.set("正在加载...")

        # 取消上一个未完成的加载请求
        self._info_req_id += 1
        req_id = self._info_req_id

        # 捕获当前快照传给子线程
        item = self.audio_items[index]
        threading.Thread(
            target=self._load_info_thread,
            args=(index, req_id, item),
            daemon=True,
        ).start()

    def _load_info_thread(self, index: int, request_id: int,
                          item: dict) -> None:
        """子线程：收集歌曲信息，完成后投递到主线程。"""
        path = item.get("path") or ""

        info: dict[str, str] = {}
        info["文件名"] = os.path.basename(path)

        # 时长（已缓存直接用，否则显示"计算中"）
        dur = item.get("duration")
        info["时长"] = format_time(dur) if dur is not None else "计算中..."

        # 格式
        _, ext = os.path.splitext(path)
        info["格式"] = ext.upper() if ext else "未知"

        # 文件大小
        try:
            size = os.path.getsize(path)
            if size < 1024:
                info["文件大小"] = f"{size} B"
            elif size < 1048576:
                info["文件大小"] = f"{size / 1024:.1f} KB"
            else:
                info["文件大小"] = f"{size / 1048576:.1f} MB"
        except OSError:
            info["文件大小"] = "未知"

        # 专辑
        album = item.get("album")
        info["专辑"] = album if album else "无"

        # 音轨号
        track = item.get("track")
        info["音轨号"] = str(track) if track is not None else "无"

        # 歌词
        lrc = item.get("lrc")
        if lrc and os.path.exists(lrc):
            info["歌词"] = f"已匹配"
        else:
            info["歌词"] = "未匹配"

        # 回到主线程应用结果
        self.root.after(0, lambda: self._apply_info(index, info, request_id))

    def _apply_info(self, index: int, info: dict[str, str],
                    request_id: int) -> None:
        """主线程：若请求未过期且用户仍在查看该歌曲，应用信息。"""
        if request_id != self._info_req_id:
            return  # 已被更新的请求取消
        if self.viewed_song_index != index:
            return  # 用户已切换到其他歌曲
        for field, var in self.song_info_labels.items():
            var.set(info.get(field, "-"))

    def _clear_song_info(self) -> None:
        """清空歌曲信息面板。"""
        self.viewed_song_index = None
        for var in self.song_info_labels.values():
            var.set("-")

    def _play_viewed_song(self) -> None:
        """播放当前在信息面板中查看的歌曲。"""
        if self.viewed_song_index is None:
            return
        if self.viewed_song_index < 0 or self.viewed_song_index >= len(self.audio_items):
            return
        self._load_track_by_index(self.viewed_song_index, autoplay=True)

    # ==================================================================
    # 打开文件 / 加载
    # ==================================================================

    def _open_file(self) -> None:
        """打开单个音频文件并自动匹配 LRC。"""
        if not self.engine.ready:
            return
        path = filedialog.askopenfilename(
            title="选择音频文件",
            filetypes=[("音频文件", "*.mp3 *.wav *.ogg *.flac"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._load_audio_file(path)
        self._auto_load_lrc(path)

    def _load_audio_file(self, path: str) -> None:
        """加载音频文件到引擎。"""
        if not self.engine.ready:
            return
        success = self.engine.load(path)
        if not success:
            messagebox.showerror("加载音频", "无法加载音频文件。")
            return
        self.audio_path = path
        self._sync_song_list_selection(path)
        self.duration = self.engine.get_duration(path)
        self._configure_seek_range()
        self._reset_playback_state()
        self._update_info()
        self._update_cover(path)

    def _sync_song_list_selection(self, path: str) -> None:
        """若打开的文件在歌曲列表中，同步选中状态。"""
        self.current_song_index = None
        self.song_list.selection_clear(0, "end")
        for index, item in enumerate(self.audio_items):
            if item.get("path") == path:
                self.current_song_index = index
                self.song_list.selection_set(index)
                self.song_list.see(index)
                break

    def _load_track_by_index(self, index: int, autoplay: bool = True) -> None:
        """按索引加载并播放歌曲列表中的曲目。"""
        if index < 0 or index >= len(self.audio_items):
            return
        path = self.audio_items[index].get("path")
        if not path:
            return
        self.current_song_index = index
        self.viewed_song_index = index
        self.song_list.selection_clear(0, "end")
        self.song_list.selection_set(index)
        self.song_list.see(index)
        self._load_audio_file(path)
        lrc_path = self.audio_items[index].get("lrc")
        if lrc_path and os.path.exists(lrc_path):
            self._load_lrc_file(lrc_path)
        else:
            self._clear_lrc()
        self._refresh_status_bar()
        if autoplay:
            self._play()

    # ==================================================================
    # LRC 歌词
    # ==================================================================

    def _auto_load_lrc(self, audio_path: str) -> None:
        """自动查找并加载与音频同目录同名的 .lrc 文件。"""
        candidate = self._find_lrc_for_audio(audio_path)
        if candidate:
            self._load_lrc_file(candidate)
        else:
            self._clear_lrc()

    @staticmethod
    def _find_lrc_for_audio(audio_path: str) -> str | None:
        """查找与音频文件同名的 .lrc 文件。"""
        base, _ = os.path.splitext(audio_path)
        candidate = f"{base}.lrc"
        return candidate if os.path.exists(candidate) else None

    def _load_lrc_file(self, path: str) -> None:
        """加载并解析 LRC 歌词文件。"""
        content = read_text_file(path)
        if content is None:
            messagebox.showerror("加载歌词", "无法读取 LRC 文件。")
            self._clear_lrc()
            return
        self.lrc_lines = LrcParser.parse(content)
        self.lrc_times = [ts for ts, _ in self.lrc_lines]
        self.current_lrc_index = -1
        self.lrc_path = path
        self._refresh_lyric_display()
        self._update_info()

    def _clear_lrc(self) -> None:
        """清除当前歌词数据。"""
        self.lrc_lines.clear()
        self.lrc_times.clear()
        self.current_lrc_index = -1
        self.lrc_path = None
        self._refresh_lyric_display()
        self._update_info()

    def _refresh_lyric_display(self) -> None:
        """刷新歌词显示区。"""
        if not self.lrc_lines:
            self.now_line_var.set(
                "未加载歌词" if self.lrc_path is None else "未找到时间轴歌词")
            self._update_lyric_overlay()
            return
        self.now_line_var.set(self.lrc_lines[0][1])
        self._update_lyric_overlay()

    # ==================================================================
    # 信息行
    # ==================================================================

    def _update_info(self) -> None:
        """更新「音频: xxx | 歌词: xxx」信息行。"""
        audio_name = os.path.basename(self.audio_path) if self.audio_path else "未选择"
        lrc_name = os.path.basename(self.lrc_path) if self.lrc_path else "未匹配"
        self.info_var.set(f"音频: {audio_name} | 歌词: {lrc_name}")

    # ==================================================================
    # 进度条
    # ==================================================================

    def _configure_seek_range(self) -> None:
        """根据时长设置进度条的取值范围。"""
        if self.duration and self.duration > 0:
            self.seek_scale.config(from_=0.0, to=self.duration, state="normal")
        else:
            self.seek_scale.config(from_=0.0, to=100.0, state="disabled")
        self.seek_var.set(0.0)

    def _on_seek_press(self, _event: tk.Event) -> None:
        """进度条拖动开始：覆盖取消条到按钮区上方。"""
        if not self.engine.ready or not self.audio_path or self.duration is None:
            return
        self.user_seeking = True
        self._cancel_frame.place(in_=self._btn_container,
                                 relx=0, rely=0, relwidth=1, relheight=1)

    def _on_seek_release(self, _event: tk.Event) -> None:
        """进度条拖动结束：在取消条上则不跳转（继续当前播放），否则跳转。"""
        if not self.engine.ready or not self.audio_path or self.duration is None:
            return
        target = float(self.seek_var.get())
        self.user_seeking = False
        self._cancel_frame.place_forget()
        if not self._is_pointer_in_cancel():
            self._seek_to(target)

    def _is_pointer_in_cancel(self) -> bool:
        """判断当前鼠标指针是否在取消条范围内。"""
        try:
            y = self.root.winfo_pointery() - self._cancel_frame.winfo_rooty()
            return 0 <= y <= self._cancel_frame.winfo_height()
        except Exception:
            return False

    def _on_seek_changed(self, value: str) -> None:
        """进度条拖动中，实时更新时间与歌词预览。"""
        if not self.user_seeking:
            return
        try:
            target = float(value)
        except ValueError:
            return
        self.time_var.set(
            f"{format_time(target)} / {format_time(self.duration)}")
        self._sync_lyrics(target)

    # ==================================================================
    # 播放控制
    # ==================================================================

    def _play(self) -> None:
        """开始播放（内部方法）。"""
        if not self.engine.ready or not self.audio_path:
            return
        start_at = self.base_time if self.base_time > 0 else 0.0
        self.engine.play(start=start_at)
        self.is_playing = True
        self.is_paused = False
        self.play_started_at = time.monotonic() - start_at
        self.play_pause_btn.config(text="暂停")

    def _toggle_play_pause(self) -> None:
        """播放/暂停切换按钮回调。"""
        if not self.engine.ready or not self.audio_path:
            return
        if not self.is_playing:
            # 停止 → 播放
            self._play()
            self.play_pause_btn.config(text="暂停")
        elif self.is_paused:
            # 暂停 → 恢复
            self.engine.unpause()
            self.is_paused = False
            self.play_started_at = time.monotonic() - self.base_time
            self.play_pause_btn.config(text="暂停")
        else:
            # 播放中 → 暂停
            self.engine.pause()
            self.base_time = self._current_time()
            self.is_paused = True
            self.play_pause_btn.config(text="播放")

    def _stop(self) -> None:
        """停止播放。"""
        if not self.engine.ready:
            return
        self.engine.stop()
        self._reset_playback_state()

    def _seek_to(self, seconds: float) -> None:
        """跳转到指定秒数。"""
        if self.duration is None:
            return
        seconds = max(0.0, min(seconds, self.duration))
        self.base_time = seconds
        if self.is_playing or self.is_paused:
            self.engine.seek(seconds)
            if self.is_paused:
                self.engine.pause()
            self.play_started_at = time.monotonic() - seconds
            self.is_playing = True
        self.seek_var.set(seconds)
        self.time_var.set(
            f"{format_time(seconds)} / {format_time(self.duration)}")
        self._sync_lyrics(seconds)

    def _seek_back_10s(self) -> None:
        """后退 10 秒。"""
        if not self.audio_path or self.duration is None:
            return
        current = self._current_time()
        self._seek_to(max(0.0, current - 10.0))

    def _seek_forward_10s(self) -> None:
        """前进 10 秒。"""
        if not self.audio_path or self.duration is None:
            return
        current = self._current_time()
        self._seek_to(min(self.duration, current + 10.0))

    # ==================================================================
    # 上一曲 / 下一曲
    # ==================================================================

    def _prev_track(self) -> None:
        """切换到上一首歌曲。"""
        if not self.audio_items:
            return
        if self.current_song_index is None:
            idx = 0
        elif self.current_song_index > 0:
            idx = self.current_song_index - 1
        else:
            idx = len(self.audio_items) - 1
        self._load_track_by_index(idx, autoplay=True)

    def _next_track(self) -> None:
        """下一曲：插播优先，否则按播放模式切歌（随机模式下随机选曲）。"""
        if self.interlude_items:
            self._stop()
            self._play_next_interlude()
            return
        if not self.audio_items:
            return
        if self.play_mode == "shuffle":
            # 随机模式：与曲目自然结束的随机逻辑一致
            if self.current_song_index is None:
                idx = random.randrange(len(self.audio_items))
            elif len(self.audio_items) == 1:
                idx = 0
            else:
                choices = [
                    i for i in range(len(self.audio_items))
                    if i != self.current_song_index
                ]
                idx = random.choice(choices)
        elif self.current_song_index is None:
            idx = 0
        elif self.current_song_index < len(self.audio_items) - 1:
            idx = self.current_song_index + 1
        else:
            idx = 0
        self._load_track_by_index(idx, autoplay=True)

    # ==================================================================
    # 播放模式
    # ==================================================================

    def _toggle_play_mode(self) -> None:
        """切换播放模式。"""
        self.play_mode_index = (self.play_mode_index + 1) % len(PLAY_MODES)
        label, mode = PLAY_MODES[self.play_mode_index]
        self.play_mode = mode
        self.mode_btn.config(text=f"模式: {label}")

    # ==================================================================
    # 置顶
    # ==================================================================

    def _toggle_topmost(self) -> None:
        """切换窗口置顶状态。"""
        self.always_on_top = not self.always_on_top
        self.root.attributes("-topmost", self.always_on_top)
        self.topmost_btn.config(
            text="置顶: 开" if self.always_on_top else "置顶: 关")

    # ==================================================================
    # 播放状态
    # ==================================================================

    def _reset_playback_state(self) -> None:
        """重置播放状态到「停止」。"""
        self.is_playing = False
        self.is_paused = False
        self.base_time = 0.0
        self.play_started_at = None
        self.user_seeking = False
        self.seek_var.set(0.0)
        dur_str = format_time(self.duration)
        self.time_var.set(f"00:00 / {dur_str}")
        self._highlight_line(-1)
        self.play_pause_btn.config(text="播放")

    def _current_time(self) -> float:
        """根据播放起始时间计算当前播放位置（秒）。"""
        if not self.is_playing:
            return 0.0
        if self.is_paused:
            return self.base_time
        if self.play_started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.play_started_at)

    # ==================================================================
    # 歌词同步
    # ==================================================================

    def _sync_lyrics(self, current_time: float) -> None:
        """根据当前播放时间更新高亮歌词行。"""
        if not self.lrc_times:
            return
        index = bisect_right(self.lrc_times, current_time) - 1
        if index != self.current_lrc_index:
            self.current_lrc_index = index
            self._highlight_line(index)

    def _highlight_line(self, index: int) -> None:
        """高亮指定索引的歌词行，并同步歌词浮层。"""
        if not self.lrc_lines:
            self.now_line_var.set(
                "未加载歌词" if self.lrc_path is None else "未找到时间轴歌词")
            self._update_lyric_overlay()
            return
        if index < 0:
            self.now_line_var.set(self.lrc_lines[0][1])
            self._update_lyric_overlay()
            return
        if index >= len(self.lrc_lines):
            self._update_lyric_overlay()
            return
        self.now_line_var.set(self.lrc_lines[index][1])
        self._update_lyric_overlay()

    # ==================================================================
    # 全窗口歌词浮层（点击封面打开）
    # ==================================================================

    def _open_lyric_overlay(self, _event: tk.Event | None = None) -> None:
        """点击专辑封面：打开全窗口歌词浮层。"""
        if self._lyric_overlay is not None or not self.audio_path:
            return
        self._build_lyric_overlay()
        self._refresh_overlay_header()
        self._update_lyric_overlay()

    def _build_lyric_overlay(self) -> None:
        """构建全窗口浮层：左栏放大封面+歌曲信息，右栏七行歌词。"""
        overlay = tk.Frame(self.root, bg=BG_COLOR)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        self._lyric_overlay = overlay

        # 左上角返回按钮（小正方形、与背景同色、FLAT）
        close_btn = tk.Button(
            overlay, text="↓", command=self._close_lyric_overlay,
            bg=BG_COLOR, fg=FG_COLOR, font=self.button_font_sm,
            activebackground=BG_COLOR, activeforeground=ACCENT_COLOR,
            relief="flat", bd=0, highlightthickness=0, padx=6, pady=3)
        close_btn.place(x=10, y=10)

        # 左右两栏定宽占比 9:11（uniform 保证严格比例，内容不会顶开布局）
        overlay.columnconfigure(0, weight=9, uniform="ovl")
        overlay.columnconfigure(1, weight=11, uniform="ovl")
        overlay.rowconfigure(0, weight=1)

        # ---- 左栏：内容垂直居中 ----
        left = tk.Frame(overlay, bg=BG_COLOR)
        left.grid(row=0, column=0, sticky="nsew")

        tk.Frame(left, bg=BG_COLOR).pack(expand=True)  # 顶部弹性（垂直居中）

        self._overlay_cover_label = tk.Label(
            left, bg=BG_COLOR, fg=FG_COLOR, text="专辑封面",
            font=self.info_font)
        self._overlay_cover_label.pack(anchor="s", pady=(0, 16))

        self._overlay_name_var = tk.StringVar(value="")
        self._overlay_name_label = tk.Label(
            left, textvariable=self._overlay_name_var, bg=BG_COLOR,
            fg=FG_COLOR, font=self.overlay_name_font,
            anchor="center", justify="center")
        self._overlay_name_label.pack()

        self._overlay_artist_var = tk.StringVar(value="")
        self._overlay_artist_label = tk.Label(
            left, textvariable=self._overlay_artist_var, bg=BG_COLOR,
            fg=ACCENT_COLOR, font=self.overlay_artist_font,
            anchor="center", justify="center")
        self._overlay_artist_label.pack(pady=(6, 0))

        # 初始换行宽度（不依赖 Configure 事件，保证首次显示即生效）
        init_wrap = max(80, int(self.root.winfo_width() * 9 / 20) - 60)
        self._overlay_name_label.config(wraplength=init_wrap)
        self._overlay_artist_label.config(wraplength=init_wrap)

        tk.Frame(left, bg=BG_COLOR).pack(expand=True)  # 底部弹性（垂直居中）

        # ---- 右栏：七行歌词（上三/下三小字，中间大字）----
        right = tk.Frame(overlay, bg=BG_COLOR)
        right.grid(row=0, column=1, sticky="nsew")

        tk.Frame(right, bg=BG_COLOR).pack(expand=True)  # 顶部弹性空间
        self._overlay_lyric_vars = []
        self._overlay_lyric_labels = []
        lyric_wrap = max(80, int(self.root.winfo_width() * 11 / 20) - 48)
        for i in range(7):
            is_middle = (i == 3)
            var = tk.StringVar(value="")
            lbl = tk.Label(
                right, textvariable=var, bg=BG_COLOR,
                fg=ACCENT_COLOR if is_middle else FG_COLOR,
                font=(self.overlay_lyric_big_font if is_middle
                      else self.overlay_lyric_small_font),
                anchor="w",
                justify="left",
                wraplength=lyric_wrap)
            lbl.pack(fill="x", padx=24,
                     pady=(10, 10) if is_middle else 6)
            self._overlay_lyric_vars.append(var)
            self._overlay_lyric_labels.append(lbl)
        tk.Frame(right, bg=BG_COLOR).pack(expand=True)  # 底部弹性空间

        # 浮层尺寸变化时更新左栏文字换行宽度
        overlay.bind("<Configure>", self._on_overlay_configure)
        # 后创建的两栏 Frame 会盖住先创建的返回按钮，必须提升到最上层
        close_btn.lift()

    def _on_overlay_configure(self, event: tk.Event) -> None:
        """浮层尺寸变化时更新左右两栏文字的换行宽度。"""
        left_wrap = max(80, int(event.width * 9 / 20) - 60)
        lyric_wrap = max(80, int(event.width * 11 / 20) - 48)
        if hasattr(self, "_overlay_name_label"):
            self._overlay_name_label.config(wraplength=left_wrap)
            self._overlay_artist_label.config(wraplength=left_wrap)
        for lbl in getattr(self, "_overlay_lyric_labels", []):
            lbl.config(wraplength=lyric_wrap)

    def _close_lyric_overlay(self) -> None:
        """销毁歌词浮层并清理引用。"""
        if self._lyric_overlay is None:
            return
        self._lyric_overlay.destroy()
        self._lyric_overlay = None
        self._overlay_cover_photo = None
        self._overlay_lyric_vars = []
        self._overlay_lyric_labels = []

    def _refresh_overlay_header(self) -> None:
        """刷新浮层左栏：放大封面、歌曲名、歌手名。"""
        if self._lyric_overlay is None:
            return
        path = self.audio_path
        if not path:
            return
        # 封面（按窗口高度与左栏宽度动态放大，避免溢出）
        left_w = int(self.root.winfo_width() * 9 / 20)
        size = max(120, min(420, self.root.winfo_height() - 260, left_w - 40))
        data = extract_cover_art(path)
        photo = cover_to_tk_image(data, size) if data else None
        if photo:
            self._overlay_cover_photo = photo
            self._overlay_cover_label.config(image=photo, text="")
        else:
            self._overlay_cover_photo = None
            self._overlay_cover_label.config(image="", text="无封面")
        # 歌曲名（去扩展名）与歌手名
        name = os.path.splitext(os.path.basename(path))[0]
        self._overlay_name_var.set(name)
        artist = self._get_current_artist()
        self._overlay_artist_var.set(artist if artist else "")

    def _get_current_artist(self) -> str:
        """获取当前播放曲目的歌手名（优先扫描缓存，否则现场读标签）。"""
        if not self.audio_path:
            return ""
        if (self.current_song_index is not None
                and self.current_song_index < len(self.audio_items)):
            item = self.audio_items[self.current_song_index]
            if item.get("path") == self.audio_path:
                artist = item.get("artist")
                if artist:
                    return artist
        try:
            _, _, artist = self._get_metadata(self.audio_path)
            return artist
        except Exception:
            return ""

    @staticmethod
    def _build_lyric_stem(lines: list[tuple[float, str]]) -> list[int]:
        """提取主干歌词行（剔除逐字展开的中间态）。

        若某行去除空白后是下一行的前缀，则为逐字展开的中间态，跳过；
        返回保留行的原始索引列表。
        """
        def _norm(text: str) -> str:
            return text.replace("\u3000", "").replace(" ", "").strip()

        stem: list[int] = []
        for i, (_, text) in enumerate(lines):
            cur = _norm(text)
            if i + 1 < len(lines):
                nxt = _norm(lines[i + 1][1])
                if cur and nxt.startswith(cur) and nxt != cur:
                    continue
            stem.append(i)
        return stem

    def _update_lyric_overlay(self) -> None:
        """刷新浮层右栏七行歌词：中间为当前逐字行，上下为主干歌词。"""
        if self._lyric_overlay is None or not self._overlay_lyric_vars:
            return
        vars_ = self._overlay_lyric_vars
        for var in vars_:
            var.set("")
        if not self.lrc_lines:
            vars_[3].set("未加载歌词" if self.lrc_path is None
                         else "未找到时间轴歌词")
            return

        # 当前行（-1 时按首行处理，与主窗口一致）
        cur = self.current_lrc_index if self.current_lrc_index >= 0 else 0
        stem_indices = self._build_lyric_stem(self.lrc_lines)

        # 定位中间行所属的主干分组
        g = bisect_right(stem_indices, cur) - 1
        if cur not in stem_indices:
            g += 1  # 逐字中间态属于下一个主干分组

        # 上三行 / 下三行：主干完整歌词（去尾部占位空白）
        for offset in (-3, -2, -1, 1, 2, 3):
            idx = g + offset
            if 0 <= idx < len(stem_indices):
                text = self.lrc_lines[stem_indices[idx]][1].rstrip(" \u3000")
                vars_[offset + 3].set(text)

        # 中间行与现有解析一致（逐字原样）
        vars_[3].set(self.lrc_lines[cur][1])

    # ==================================================================
    # 主循环
    # ==================================================================

    def _tick(self) -> None:
        """定时器回调：更新进度条、时间、歌词同步，检测曲目结束。"""
        if self.engine.ready and self.is_playing and not self.is_paused:
            if not self.engine.is_busy():
                self._handle_track_end()
            elif not self.user_seeking:
                current = self._current_time()
                if self.duration:
                    self.seek_var.set(current)
                self.time_var.set(
                    f"{format_time(current)} / {format_time(self.duration)}")
                self._sync_lyrics(current)
        elif self.is_paused and not self.user_seeking:
            current = self._current_time()
            self.time_var.set(
                f"{format_time(current)} / {format_time(self.duration)}")
        self.root.after(TICK_INTERVAL_MS, self._tick)

    def _handle_track_end(self) -> None:
        """曲目播放结束后：优先播放插播列表队首，否则按模式导航。"""
        # ---- 插播优先（除「仅一首」模式外）----
        if self.play_mode != "single" and self.interlude_items:
            self._play_next_interlude()
            return

        if self.play_mode == "loop_one":
            if self.audio_path:
                self._seek_to(0.0)
                return
            self._reset_playback_state()
            return

        if self.play_mode == "single":
            self._reset_playback_state()
            return

        if not self.audio_items or self.current_song_index is None:
            self._reset_playback_state()
            return

        if self.play_mode == "shuffle":
            if len(self.audio_items) == 1:
                next_idx = self.current_song_index
            else:
                choices = [
                    i for i in range(len(self.audio_items))
                    if i != self.current_song_index
                ]
                next_idx = random.choice(choices)
            self._load_track_by_index(next_idx, autoplay=True)
            return

        # loop_all
        next_idx = (self.current_song_index + 1) % len(self.audio_items)
        self._load_track_by_index(next_idx, autoplay=True)

    def _play_next_interlude(self) -> None:
        """播放插播列表队首歌曲，并将其从列表中移除。"""
        if not self.interlude_items:
            return

        # 若处于移动模式，队首被消费需调整锁定索引
        if self._il_move_mode and self._move_locked_index is not None:
            if self._move_locked_index == 0:
                # 锁定项即队首，将被消费，退出移动模式
                self._il_move_confirm()
            else:
                # 队首移除后，所有后续索引前移一位
                self._move_locked_index -= 1

        item = self.interlude_items.pop(0)
        self._refresh_interlude_list()

        # 移动模式下恢复选中状态
        if self._il_move_mode and self._move_locked_index is not None:
            self.interlude_list.selection_set(self._move_locked_index)

        path = item.get("path")
        if not path or not os.path.exists(path):
            return
        # 同步 current_song_index 到此曲在主列表中的位置
        for i, main_item in enumerate(self.audio_items):
            if main_item.get("path") == path:
                self.current_song_index = i
                break
        # 加载并播放
        self.engine.load(path)
        self.audio_path = path
        self.duration = item.get("duration") or self.engine.get_duration(path)
        self._configure_seek_range()
        self._reset_playback_state()
        self._update_info()
        self._update_cover(path)
        lrc = item.get("lrc")
        if lrc and os.path.exists(lrc):
            self._load_lrc_file(lrc)
        else:
            self._clear_lrc()
        self._play()

    # ==================================================================
    # 关闭
    # ==================================================================

    def on_close(self) -> None:
        """窗口关闭时的清理。"""
        self.engine.quit()
        self.root.destroy()
