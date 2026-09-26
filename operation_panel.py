"""右侧操作区面板（V1.7.7 从 app.py 提取）—— 倍速 / 音高 / 音量 / 高级功能 / 选项 / 歌词选项。

- 深色平滑滚动容器 `SmoothScrollFrame`（改造自 note/scrolling-example.py）：
  鼠标滚轮（滑块上也生效）平滑滚动；**内容不超一屏时滚动条自动隐藏**，
  后续添加更多设置项不会改变窗口宽度/高度。
- 拖动交互内聚于此：倍速「左取消(红)/右重置(蓝)」、音高与高级功能的蓝色「重置」条。
- 控件以公开属性暴露（`speed_var` / `vol_var` / `mode_btns` ...），
  app 与控制台经 `app.op` 访问（如 `app.op.vol_var`、`app.op.update_vol_preview()`）。
"""

import tkinter as tk
from tkinter import ttk


class SmoothScrollFrame(tk.Frame):
    """深色平滑滚动容器：把内容放进 `self.inner` 即可。

    与示例的差异：滚动条超出才显示（自动隐藏）、滑块上也响应滚轮、
    动画带销毁防护、`<Configure>` 时自动为新增子控件补绑滚轮。
    """

    PIXELS_PER_NOTCH = 70    # 鼠标滚轮一格滚动像素
    EASING = 0.22            # 缓动系数 0~1，越大越快越"硬"
    FRAME_MS = 16            # 帧间隔 ≈ 60fps
    SNAP = 0.5               # 差值小于此值直接吸附，避免无限逼近
    WHEEL_SKIP = {"Text", "Listbox"}   # 这些控件上滚轮交给控件自身

    def __init__(self, master, bg: str = "#23272E",
                 scrollbar_style: str | None = None, **kwargs) -> None:
        super().__init__(master, bg=bg, **kwargs)
        self._bg = bg

        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0,
                                takefocus=0, bg=bg)
        sb_cfg: dict = {"orient": "vertical", "command": self._on_scrollbar}
        if scrollbar_style:
            sb_cfg["style"] = scrollbar_style
        self.vbar = ttk.Scrollbar(self, **sb_cfg)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self._target = 0.0       # 目标偏移（像素）
        self._anim_id = None
        self._wheel_bound: set = set()
        self._vbar_shown = False

    # ---------------- 布局 ----------------
    def _on_inner_configure(self, event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_vbar_visibility()
        self.bind_wheel()   # 动态新增的子控件自动补绑滚轮

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)
        self._update_vbar_visibility()

    def _update_vbar_visibility(self) -> None:
        """内容超出视口才显示滚动条（不占宽）。"""
        try:
            need = self._content_height() > self.canvas.winfo_height() + 1
        except tk.TclError:
            return
        if need and not self._vbar_shown:
            self.vbar.pack(side="right", fill="y")
            self._vbar_shown = True
        elif not need and self._vbar_shown:
            self.vbar.pack_forget()
            self._vbar_shown = False

    # ---------------- 几何辅助 ----------------
    def _content_box(self):
        return self.canvas.bbox("all")

    def _content_height(self) -> int:
        box = self._content_box()
        return (box[3] - box[1]) if box else 0

    def _max_offset(self) -> float:
        return max(0.0, self._content_height() - self.canvas.winfo_height())

    def _current_offset(self) -> float:
        box = self._content_box()
        y0 = box[1] if box else 0
        return self.canvas.canvasy(0) - y0

    def _apply(self, offset: float) -> None:
        h = self._content_height()
        if h > 0:
            self.canvas.yview_moveto(offset / h)

    # ---------------- 滚动条：即时响应，不做动画 ----------------
    def _on_scrollbar(self, *args) -> None:
        self._cancel_anim()
        self.canvas.yview(*args)
        self._target = self._current_offset()

    # ---------------- 对外接口 ----------------
    def scroll_by(self, px: float) -> None:
        """px > 0：内容上移（看更下方的内容）。"""
        self._target = max(0.0, min(self._max_offset(), self._target + px))
        self._start_anim()

    def scroll_to(self, px: float, animate: bool = True) -> None:
        self._target = max(0.0, min(self._max_offset(), float(px)))
        if animate:
            self._start_anim()
        else:
            self._cancel_anim()
            self._apply(self._target)

    def scroll_to_widget(self, widget, margin: int = 24,
                         animate: bool = True) -> None:
        """把某个控件滚进可见区域（配合聚焦/高亮使用）。"""
        self.update_idletasks()
        top = widget.winfo_rooty() - self.inner.winfo_rooty()
        bottom = top + widget.winfo_height()
        view_h = self.canvas.winfo_height()
        cur = self._current_offset()
        if top - margin < cur:
            self.scroll_to(top - margin, animate)
        elif bottom + margin > cur + view_h:
            self.scroll_to(bottom + margin - view_h, animate)

    # ---------------- 缓动动画 ----------------
    def _start_anim(self) -> None:
        if self._anim_id is None:
            self._anim_id = self.after(self.FRAME_MS, self._animate)

    def _cancel_anim(self) -> None:
        if self._anim_id is not None:
            try:
                self.after_cancel(self._anim_id)
            except tk.TclError:
                pass
            self._anim_id = None

    def _animate(self) -> None:
        self._anim_id = None
        try:
            if not self.winfo_exists():
                return
            # 窗口尺寸可能已变化：目标值随之钳制
            self._target = max(0.0, min(self._max_offset(), self._target))
            current = self._current_offset()
            diff = self._target - current
            if abs(diff) < self.SNAP:
                self._apply(self._target)      # 吸附到精确位置
                return
            self._apply(current + diff * self.EASING)
            self._anim_id = self.after(self.FRAME_MS, self._animate)
        except tk.TclError:
            self._anim_id = None

    # ---------------- 滚轮 ----------------
    def bind_wheel(self, *widgets) -> None:
        """为控件（默认 canvas/滚动条/inner 递归）绑定滚轮（幂等）。"""
        for w in (widgets or (self.inner, self.canvas, self.vbar)):
            self._bind_one(w)

    def _bind_one(self, widget) -> None:
        try:
            cls = widget.winfo_class()
        except tk.TclError:
            return
        if cls not in self.WHEEL_SKIP and widget not in self._wheel_bound:
            self._wheel_bound.add(widget)
            widget.bind("<MouseWheel>", self._on_wheel, add="+")   # Win/macOS
            widget.bind("<Button-4>", self._on_wheel, add="+")     # Linux 上滚
            widget.bind("<Button-5>", self._on_wheel, add="+")     # Linux 下滚
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for c in children:
            self._bind_one(c)

    def _on_wheel(self, event) -> None:
        if event.num == 4:                       # Linux 上滚
            px = -self.PIXELS_PER_NOTCH
        elif event.num == 5:                     # Linux 下滚
            px = self.PIXELS_PER_NOTCH
        elif abs(event.delta) >= 120:            # Windows：±120 的倍数
            px = -event.delta / 120.0 * self.PIXELS_PER_NOTCH
        else:                                    # macOS：像素级 delta
            px = -event.delta * 3.0
        self.scroll_by(px)

    def forget(self, widget) -> None:
        """销毁控件前调用，清理滚轮引用避免内存泄漏。"""
        self._wheel_bound.discard(widget)
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for c in children:
            self.forget(c)


class OperationPanel:
    """右侧操作区：倍速 / 音高 / 音量 / 高级功能 / 选项区（自包含）。

    `app.op` 即本对象；控件均为公开属性，供 app / 控制台读写。
    """

    def __init__(self, app, parent: tk.Frame) -> None:
        # 延迟导入：app 在本模块导入时尚未定义这些常量
        from app import BG_COLOR, FG_COLOR, ACCENT_COLOR, SUBTLE_COLOR
        self.app = app
        self.BG_COLOR = BG_COLOR
        self.FG_COLOR = FG_COLOR
        self.ACCENT_COLOR = ACCENT_COLOR
        self.SUBTLE_COLOR = SUBTLE_COLOR

        # 拖动状态
        self._speed_dragging = False
        self._pitch_dragging = False
        self._adv_dragging = False
        self._adv_active: str | None = None
        self._lyric_opt_active: str | None = None

        # 选项区按钮组
        self.lyric_mode_btns: dict[str, tk.Button] = {}
        self.mode_btns: dict[str, tk.Button] = {}
        self.topmost_btns: dict[bool, tk.Button] = {}

        # 滚动容器（内容不超一屏时滚动条自动隐藏）
        self.scroller = SmoothScrollFrame(
            parent, bg=BG_COLOR,
            scrollbar_style="LRC.Vertical.TScrollbar")
        self.scroller.pack(fill="both", expand=True)
        inner = self.scroller.inner

        self._build_speed(inner)
        self._build_pitch(inner)
        self._build_volume(inner)
        self._build_advanced(inner)
        self._build_options(inner)
        self._build_lyric_options(inner)
        self.scroller.bind_wheel()

    # ---------------- 便捷引用 ----------------
    def _px(self, v: int, min_px: int = 0) -> int:
        return self.app._px(v, min_px)

    # ==================================================================
    # 构建：倍速 / 音高 / 音量 / 高级功能 / 选项区
    # ==================================================================

    def _build_speed(self, parent: tk.Frame) -> None:
        """倍速区：标题行兼作拖拽区（左取消红 / 右重置蓝）+ 滑块与数值。"""
        speed_frame = tk.Frame(parent, bg=self.BG_COLOR)
        speed_frame.pack(side="top", fill="x", pady=(4, 18))

        self.speed_cancel_row = tk.Frame(speed_frame, bg=self.BG_COLOR)
        self.speed_cancel_row.pack(fill="x")
        tk.Label(self.speed_cancel_row, text="倍速", bg=self.BG_COLOR,
                 fg=self.ACCENT_COLOR,
                 font=self.app.button_font).pack(side="left")
        self.pitch_btn = tk.Button(
            self.speed_cancel_row, text="保音高: 关",
            command=self._toggle_pitch_fix,
            bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
            font=self.app.button_font_sm,
            activebackground=self.ACCENT_COLOR,
            activeforeground=self.BG_COLOR,
            relief="flat", padx=6, pady=2)
        self.pitch_btn.pack(side="right")

        slider_row = tk.Frame(speed_frame, bg=self.BG_COLOR)
        slider_row.pack(fill="x", pady=(2, 0))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.speed_scale = ttk.Scale(
            slider_row, style="LRC.Horizontal.TScale", orient="horizontal",
            from_=0.1, to=3.0, variable=self.speed_var,
            command=self._on_speed_changed)
        self.speed_scale.pack(side="left", fill="x", expand=True)
        self.speed_scale.bind("<ButtonPress-1>", self._on_speed_press)
        self.speed_scale.bind("<ButtonRelease-1>", self._on_speed_release)
        self.speed_preview = tk.Label(
            slider_row, text="1.00x", width=6, bg=self.BG_COLOR,
            fg=self.FG_COLOR, font=self.app.info_font, anchor="e")
        self.speed_preview.pack(side="right", padx=(10, 0))

        # 拖拽条（拖动时覆盖标题行）：左半取消（红）/ 右半重置（蓝）
        self.speed_drag_bar = tk.Frame(speed_frame, height=self._px(36))
        self.speed_cancel_part = tk.Frame(self.speed_drag_bar, bg="#6B1010")
        self.speed_cancel_part.pack(side="left", fill="both", expand=True)
        tk.Label(self.speed_cancel_part, text="取消",
                 bg="#6B1010", fg="#FF5555",
                 font=self.app.button_font_sm).pack(expand=True)
        self.speed_reset_part = tk.Frame(self.speed_drag_bar, bg="#1F3A8A")
        self.speed_reset_part.pack(side="right", fill="both", expand=True)
        tk.Label(self.speed_reset_part, text="重置",
                 bg="#1F3A8A", fg="#6FA3FF",
                 font=self.app.button_font_sm).pack(expand=True)

    def _build_pitch(self, parent: tk.Frame) -> None:
        """音高区（移调 ±12 半音，变调不变速）：标题行为重置条覆盖区。"""
        pitch_frame = tk.Frame(parent, bg=self.BG_COLOR)
        pitch_frame.pack(side="top", fill="x", pady=(0, 18))

        self.pitch_cancel_row = tk.Frame(pitch_frame, bg=self.BG_COLOR)
        self.pitch_cancel_row.pack(fill="x")
        tk.Label(self.pitch_cancel_row, text="音高（LAB）",
                 bg=self.BG_COLOR, fg=self.ACCENT_COLOR,
                 font=self.app.button_font).pack(side="left")

        self.pitch_reset = tk.Frame(pitch_frame, bg="#1F3A8A",
                                    height=self._px(36))
        tk.Label(self.pitch_reset, text="重置", bg="#1F3A8A", fg="#6FA3FF",
                 font=self.app.button_font_sm).pack(expand=True)

        pitch_row = tk.Frame(pitch_frame, bg=self.BG_COLOR)
        pitch_row.pack(fill="x", pady=(2, 0))
        self.pitch_var = tk.DoubleVar(value=0.0)
        self.pitch_scale = ttk.Scale(
            pitch_row, style="LRC.Horizontal.TScale", orient="horizontal",
            from_=-12, to=12, variable=self.pitch_var,
            command=self._on_pitch_changed)
        self.pitch_scale.pack(side="left", fill="x", expand=True)
        self.pitch_scale.bind("<ButtonPress-1>", self._on_pitch_press)
        self.pitch_scale.bind("<ButtonRelease-1>", self._on_pitch_release)
        self.pitch_preview = tk.Label(
            pitch_row, text="0", width=6, bg=self.BG_COLOR, fg=self.FG_COLOR,
            font=self.app.info_font, anchor="e")
        self.pitch_preview.pack(side="right", padx=(10, 0))

    def _build_volume(self, parent: tk.Frame) -> None:
        """音量区：无取消，拖动实时生效。"""
        vol_frame = tk.Frame(parent, bg=self.BG_COLOR)
        vol_frame.pack(side="top", fill="x")

        vol_title_row = tk.Frame(vol_frame, bg=self.BG_COLOR)
        vol_title_row.pack(fill="x")
        tk.Label(vol_title_row, text="音量", bg=self.BG_COLOR,
                 fg=self.ACCENT_COLOR,
                 font=self.app.button_font).pack(side="left")

        vol_slider_row = tk.Frame(vol_frame, bg=self.BG_COLOR)
        vol_slider_row.pack(fill="x", pady=(2, 0))
        self.vol_var = tk.DoubleVar(value=100.0)
        self.vol_scale = ttk.Scale(
            vol_slider_row, style="LRC.Horizontal.TScale", orient="horizontal",
            from_=0, to=100, variable=self.vol_var,
            command=self._on_vol_changed)
        self.vol_scale.pack(side="left", fill="x", expand=True)
        self.vol_preview = tk.Label(
            vol_slider_row, text="100%", width=6, bg=self.BG_COLOR,
            fg=self.FG_COLOR, font=self.app.info_font, anchor="e")
        self.vol_preview.pack(side="right", padx=(10, 0))

    def _build_advanced(self, parent: tk.Frame) -> None:
        """高级功能区：声道平衡 / 响度增益（含控制台/重置按钮）。"""
        adv_frame = tk.Frame(parent, bg=self.BG_COLOR)
        adv_frame.pack(side="top", fill="x", pady=(18, 4))

        self.adv_cancel_row = tk.Frame(adv_frame, bg=self.BG_COLOR)
        self.adv_cancel_row.pack(fill="x")
        tk.Label(self.adv_cancel_row, text="高级功能", bg=self.BG_COLOR,
                 fg=self.ACCENT_COLOR,
                 font=self.app.button_font).pack(side="left")
        self.console_btn = tk.Button(
            self.adv_cancel_row, text="控制台",
            command=self.app._toggle_console,
            bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
            font=self.app.button_font_sm,
            activebackground=self.ACCENT_COLOR,
            activeforeground=self.BG_COLOR,
            relief="flat", padx=6, pady=2)
        self.console_btn.pack(side="right")
        # 「重置」按钮：位于「控制台」左侧（side=right 后 pack → 更靠左）
        self.reset_btn = tk.Button(
            self.adv_cancel_row, text="重置",
            command=self.app._toggle_reset,
            bg=self.SUBTLE_COLOR, fg="#FF9A9A",
            font=self.app.button_font_sm,
            activebackground="#6B1010", activeforeground="#FF5555",
            relief="flat", padx=6, pady=2)
        self.reset_btn.pack(side="right")

        self.adv_reset = tk.Frame(adv_frame, bg="#1F3A8A",
                                  height=self._px(36))
        tk.Label(self.adv_reset, text="重置", bg="#1F3A8A", fg="#6FA3FF",
                 font=self.app.button_font_sm).pack(expand=True)

        (self.balance_var, self.balance_scale,
         self.balance_val) = self._build_adv_slider(
            adv_frame, "声道平衡", -1.0, 1.0, 0.0,
            lambda v: f"{v:+.2f}", self._on_balance_changed, "balance")
        (self.gain_var, self.gain_scale,
         self.gain_val) = self._build_adv_slider(
            adv_frame, "响度增益", 0.0, 2.0, 1.0,
            lambda v: f"{v:.2f}x", self._on_gain_changed, "gain")

    def _build_adv_slider(self, parent, title, from_, to, default, fmt,
                          on_change, key, group: str = "adv"):
        """构建一行参数滑块（返回 var/scale/val）。

        group="adv"：拖动时在「高级功能」标题行覆盖蓝色重置条；
        group="lyric"：在「歌词选项」标题行覆盖；松手在条内则仅重置该项。
        """
        row = tk.Frame(parent, bg=self.BG_COLOR)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text=title, bg=self.BG_COLOR, fg=self.FG_COLOR,
                 font=self.app.list_font, width=8,
                 anchor="w").pack(side="left")
        var = tk.DoubleVar(value=default)
        scale = ttk.Scale(row, style="LRC.Horizontal.TScale",
                          orient="horizontal", from_=from_, to=to,
                          variable=var, command=on_change)
        scale.pack(side="left", fill="x", expand=True)
        scale.bind("<ButtonPress-1>",
                   lambda e, k=key, g=group: self._on_slider_press(e, k, g))
        scale.bind("<ButtonRelease-1>",
                   lambda e, g=group: self._on_slider_release(e, g))
        val = tk.Label(row, text=fmt(default), width=6, bg=self.BG_COLOR,
                       fg=self.FG_COLOR, font=self.app.info_font, anchor="e")
        val.pack(side="right", padx=(10, 0))
        return var, scale, val

    def _build_options(self, parent: tk.Frame) -> None:
        """选项区：播放模式 / 置顶（无缝按钮组）。"""
        opt_frame = tk.Frame(parent, bg=self.BG_COLOR)
        opt_frame.pack(side="top", fill="x", pady=(18, 4))

        tk.Label(opt_frame, text="选项", bg=self.BG_COLOR,
                 fg=self.ACCENT_COLOR,
                 font=self.app.button_font, anchor="w").pack(anchor="w")

        # 播放模式：关闭（仅一首）/ 列表（列表循环）/ 单曲（单曲循环）/ 随机
        box = self._build_option_row(opt_frame, "模式")
        for text, mode in (("关闭", "single"), ("列表", "loop_all"),
                           ("单曲", "loop_one"), ("随机", "shuffle")):
            self.mode_btns[mode] = self._make_seam_button(
                box, text, lambda m=mode: self.app._set_play_mode(m))
        self.refresh_mode_buttons()

        # 置顶：开 / 关
        box = self._build_option_row(opt_frame, "置顶")
        for text, val in (("开", True), ("关", False)):
            self.topmost_btns[val] = self._make_seam_button(
                box, text, lambda v=val: self.app._set_topmost(v))
        self.refresh_topmost_buttons()

    def _build_lyric_options(self, parent: tk.Frame) -> None:
        """歌词选项区：歌词偏移 / 桌面歌词 / 透明度 / 默认宽度 / 字体大小。"""
        lyr_frame = tk.Frame(parent, bg=self.BG_COLOR)
        lyr_frame.pack(side="top", fill="x", pady=(18, 4))

        self.lyric_opt_cancel_row = tk.Frame(lyr_frame, bg=self.BG_COLOR)
        self.lyric_opt_cancel_row.pack(fill="x")
        tk.Label(self.lyric_opt_cancel_row, text="歌词选项",
                 bg=self.BG_COLOR, fg=self.ACCENT_COLOR,
                 font=self.app.button_font).pack(side="left")

        self.lyric_opt_reset = tk.Frame(lyr_frame, bg="#1F3A8A",
                                        height=self._px(36))
        tk.Label(self.lyric_opt_reset, text="重置", bg="#1F3A8A", fg="#6FA3FF",
                 font=self.app.button_font_sm).pack(expand=True)

        # 歌词偏移（±600ms，10ms 步进）
        (self.lrc_offset_var, self.lrc_offset_scale,
         self.lrc_offset_val) = self._build_adv_slider(
            lyr_frame, "歌词偏移", -60, 60, 0,
            lambda v: f"{v*10}ms", self._on_lrc_offset_changed, "lrc",
            group="lyric")

        # 桌面歌词：关闭 / 顶部 / 底部
        box = self._build_option_row(lyr_frame, "桌面歌词")
        for text, mode in (("关闭", "off"), ("顶部", "top"),
                           ("底部", "bottom")):
            self.lyric_mode_btns[mode] = self._make_seam_button(
                box, text, lambda m=mode: self.app._set_lyric_bar_mode(m))
        self.refresh_lyric_mode_buttons()

        # 透明度 / 默认宽度 / 字体大小（作用于桌面歌词条）
        (self.lyric_alpha_var, self.lyric_alpha_scale,
         self.lyric_alpha_val) = self._build_adv_slider(
            lyr_frame, "透明度", 30, 100, self.app._lyric_bar_alpha,
            lambda v: f"{v:.0f}%", self._on_lyric_alpha_changed, "alpha",
            group="lyric")
        (self.lyric_width_var, self.lyric_width_scale,
         self.lyric_width_val) = self._build_adv_slider(
            lyr_frame, "默认宽度", 10, 100, self.app._lyric_bar_width_pct,
            lambda v: f"{v:.0f}%", self._on_lyric_width_changed, "width",
            group="lyric")
        (self.lyric_font_var, self.lyric_font_scale,
         self.lyric_font_val) = self._build_adv_slider(
            lyr_frame, "字体大小", 12, 30, self.app._lyric_bar_font_size,
            lambda v: f"{v:.0f}px", self._on_lyric_font_changed, "font",
            group="lyric")

    def _build_option_row(self, parent: tk.Frame, title: str) -> tk.Frame:
        """「选项」区一行：左侧定宽标签 + 右侧无缝按钮容器（返回容器）。"""
        row = tk.Frame(parent, bg=self.BG_COLOR)
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text=title, bg=self.BG_COLOR, fg=self.FG_COLOR,
                 font=self.app.list_font, anchor="w",
                 width=8).pack(side="left")
        box = tk.Frame(row, bg=self.BG_COLOR)
        box.pack(side="right")
        return box

    def _make_seam_button(self, box: tk.Frame, text: str,
                          command) -> tk.Button:
        """创建无缝衔接小按钮（无间距无边框）并 pack 到按钮容器。"""
        b = tk.Button(
            box, text=text, command=command,
            bg=self.SUBTLE_COLOR, fg=self.FG_COLOR,
            font=self.app.button_font_sm,
            activebackground=self.ACCENT_COLOR,
            activeforeground=self.BG_COLOR,
            relief="flat", bd=0, highlightthickness=0,
            padx=self._px(8), pady=self._px(2))
        b.pack(side="left")   # 无缝衔接：按钮之间不留间距
        return b

    # ==================================================================
    # 倍速交互
    # ==================================================================

    def _on_speed_press(self, _event: tk.Event) -> None:
        """倍速拖动开始：拖拽条（左取消/右重置）覆盖标题行。"""
        if not self.app.engine.ready or not self.app.audio_path:
            return
        self._speed_dragging = True
        self.speed_drag_bar.place(in_=self.speed_cancel_row,
                                  relx=0, rely=0, relwidth=1, relheight=1)

    def _on_speed_changed(self, value: str) -> None:
        """倍速拖动中：仅更新预览（吸附到 0.05 步进），不生效。"""
        if not self._speed_dragging:
            return
        v = round(float(value) / 0.05) * 0.05
        self.speed_var.set(v)
        self.speed_preview.config(text=f"{v:.2f}x")

    def _on_speed_release(self, _event: tk.Event) -> None:
        """倍速拖动结束：左半取消回退 / 右半重置为 1.0x / 其余应用。"""
        if not self._speed_dragging:
            return
        self._speed_dragging = False
        self.speed_drag_bar.place_forget()
        side = self._pointer_side_in(self.speed_drag_bar)
        if side == "left":
            # 取消：回退到当前生效倍速
            v = self.app.engine.get_speed()
            self.speed_var.set(v)
            self.speed_preview.config(text=f"{v:.2f}x")
        elif side == "right":
            # 重置：回到默认 1.0x
            v = 1.0
            self.speed_var.set(v)
            self.app.engine.set_speed(v)
            self.speed_preview.config(text=f"{v:.2f}x")
        else:
            v = round(float(self.speed_var.get()) / 0.05) * 0.05
            self.speed_var.set(v)
            self.app.engine.set_speed(v)
        self.app._schedule_config_save()

    def _pointer_side_in(self, widget) -> str | None:
        """判断指针是否在控件内，并返回所在半区（'left'/'right'/None）。"""
        try:
            x = self.app.root.winfo_pointerx() - widget.winfo_rootx()
            y = self.app.root.winfo_pointery() - widget.winfo_rooty()
            if not (0 <= x <= widget.winfo_width()
                    and 0 <= y <= widget.winfo_height()):
                return None
            return "left" if x < widget.winfo_width() / 2 else "right"
        except Exception:
            return None

    # ==================================================================
    # 音量交互与预览
    # ==================================================================

    def _on_vol_changed(self, value: str) -> None:
        """音量变化：吸附到整数并实时生效（无取消功能）。"""
        v = int(round(float(value)))
        v = max(0, min(100, v))
        self.vol_var.set(v)
        self.app.engine.set_volume(v / 100.0)
        self.update_vol_preview()
        self.app._schedule_config_save()

    def update_vol_preview(self) -> None:
        """按音量×增益更新音量数值显示；增益≠1 时显示有效音量并置黄色。"""
        vol = int(round(float(self.vol_var.get())))
        gain = (self.app.engine.get_gain()
                if self.app.engine.backend == "sounddevice" else 1.0)
        if abs(gain - 1.0) < 1e-9:
            self.vol_preview.config(text=f"{vol}%", fg=self.FG_COLOR)
        else:
            eff = int(round(vol * gain))
            self.vol_preview.config(text=f"{eff}%", fg="#FFD54F")

    # ==================================================================
    # 音高（移调）交互
    # ==================================================================

    def _toggle_pitch_fix(self) -> None:
        """切换保音高模式（变速不变调）。"""
        on = not self.app.engine.get_pitch_fix()
        self.app.engine.set_pitch_fix(on)
        self.pitch_btn.config(text="保音高: 开" if on else "保音高: 关")
        self.app._schedule_config_save()

    def _on_pitch_press(self, _event: tk.Event) -> None:
        """音高滑块拖动开始：显示蓝色重置条覆盖标题行。"""
        if not self.app.engine.ready or not self.app.audio_path:
            return
        if self.app.engine.backend != "sounddevice":
            return
        self._pitch_dragging = True
        self.pitch_reset.place(in_=self.pitch_cancel_row,
                               relx=0, rely=0, relwidth=1, relheight=1)

    def _on_pitch_changed(self, value: str) -> None:
        """音高移调拖动中：吸附到整数半音并实时生效。"""
        if not self._pitch_dragging:
            return
        v = int(round(float(value)))
        v = max(-12, min(12, v))
        self.pitch_var.set(v)
        self.app.engine.set_pitch_shift(v)
        self.pitch_preview.config(text=f"{v:+d}", fg=self.pitch_color(v))

    def _on_pitch_release(self, _event: tk.Event) -> None:
        """音高滑块拖动结束：松手在重置条内则重置为 0，否则保持当前值。"""
        if not self._pitch_dragging:
            return
        self._pitch_dragging = False
        self.pitch_reset.place_forget()
        if self.app._is_pointer_in_cancel(self.pitch_reset):
            self.pitch_var.set(0)
            self.app.engine.set_pitch_shift(0)
            self.pitch_preview.config(text="+0", fg=self.FG_COLOR)
        self.app._schedule_config_save()

    def pitch_color(self, v: int) -> str:
        """音高移调数值颜色：偏离 0 越大越偏黄（警示）。"""
        p = min(1.0, abs(v) / 12.0)
        r = int(0xC8 + (0xFF - 0xC8) * p)
        g = int(0xC8 + (0xD5 - 0xC8) * p)
        b = int(0xC8 + (0x4F - 0xC8) * p)
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    # ==================================================================
    # 滑块交互（高级功能 / 歌词选项：蓝色「重置」条覆盖各自标题行）
    # ==================================================================

    def _on_slider_press(self, _event: tk.Event, key: str,
                         group: str) -> None:
        """滑块拖动开始：记录所在组与项，显示小组标题行的蓝色重置条。"""
        if group == "adv":
            self._adv_dragging = True
            self._adv_active = key
            self.adv_reset.place(in_=self.adv_cancel_row,
                                 relx=0, rely=0, relwidth=1, relheight=1)
        else:
            self._lyric_opt_active = key
            self.lyric_opt_reset.place(in_=self.lyric_opt_cancel_row,
                                       relx=0, rely=0, relwidth=1,
                                       relheight=1)

    def _on_slider_release(self, _event: tk.Event, group: str) -> None:
        """滑块拖动结束：松手在重置条内则仅重置该项，否则保持当前值。"""
        if group == "adv":
            if not self._adv_dragging:
                return
            self._adv_dragging = False
            self.adv_reset.place_forget()
            if self.app._is_pointer_in_cancel(self.adv_reset):
                self._reset_adv(self._adv_active)
        else:
            self.lyric_opt_reset.place_forget()
            if self.app._is_pointer_in_cancel(self.lyric_opt_reset):
                self._reset_lyric_opt(self._lyric_opt_active)

    def _reset_adv(self, key: str | None) -> None:
        """重置指定高级参数为默认值（仅当前正在调整的那一项）。"""
        if key == "balance":
            self.balance_var.set(0.0)
            self.app.engine.set_balance(0.0)
            self.balance_val.config(text="+0.00")
        elif key == "gain":
            self.gain_var.set(1.0)
            self.app.engine.set_gain(1.0)
            self.gain_val.config(text="1.00x", fg=self.FG_COLOR)
            self.update_vol_preview()
        self.app._schedule_config_save()

    def _reset_lyric_opt(self, key: str | None) -> None:
        """重置「歌词选项」指定参数为默认值（仅当前正在调整的那一项）。"""
        if key == "lrc":
            self.app.lrc_offset = 0
            self.lrc_offset_var.set(0)
            self.lrc_offset_val.config(text="0ms", fg=self.FG_COLOR)
            self.app._sync_lyrics(self.app._current_time())
            self.app._schedule_config_save()
        elif key == "alpha":
            self.app._set_lyric_bar_alpha(85)
        elif key == "width":
            self.app._set_lyric_bar_width(25)
        elif key == "font":
            self.app._set_lyric_bar_font_size(18)

    def _on_lrc_offset_changed(self, value: str) -> None:
        """歌词偏移：正值=歌词延后，负值=歌词提前（步进 10ms）。"""
        v = round(float(value) / 1) * 1
        v = max(-60, min(60, v))
        self.lrc_offset_var.set(v)
        self.app.lrc_offset = v
        self.lrc_offset_val.config(text=f"{v * 10}ms",
                                   fg=self.lrc_offset_color(v))
        self.app._sync_lyrics(self.app._current_time())
        self.app._schedule_config_save()

    def lrc_offset_color(self, v: float) -> str:
        """歌词偏移数值颜色：随偏移量增大由灰转红（警示）。"""
        p = min(1.0, abs(v) / 60.0)
        r = int(0xC8 + (0xFF - 0xC8) * p)
        g = int(0xC8 + (0x55 - 0xC8) * p)
        b = int(0xC8 + (0x55 - 0xC8) * p)
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    # ---- 歌词条参数（透明度 / 默认宽度 / 字体大小）----

    def _on_lyric_alpha_changed(self, value: str) -> None:
        """歌词条透明度（30~100%，实时生效）。"""
        v = int(round(float(value)))
        v = max(30, min(100, v))
        self.set_lyric_alpha_display(v)
        self.app._set_lyric_bar_alpha(v)

    def _on_lyric_width_changed(self, value: str) -> None:
        """歌词条默认宽度（占屏 10~100%，实时生效）。"""
        v = int(round(float(value)))
        v = max(10, min(100, v))
        self.set_lyric_width_display(v)
        self.app._set_lyric_bar_width(v)

    def _on_lyric_font_changed(self, value: str) -> None:
        """歌词条字体大小（12~30px，实时生效）。"""
        v = int(round(float(value)))
        v = max(12, min(30, v))
        self.set_lyric_font_display(v)
        self.app._set_lyric_bar_font_size(v)

    def set_lyric_alpha_display(self, v: int) -> None:
        """同步透明度滑块显示（同值时跳过，避免递归触发）。"""
        if int(round(float(self.lyric_alpha_var.get()))) != v:
            self.lyric_alpha_var.set(v)
        self.lyric_alpha_val.config(text=f"{v}%")

    def set_lyric_width_display(self, v: int) -> None:
        """同步默认宽度滑块显示（同值时跳过）。"""
        if int(round(float(self.lyric_width_var.get()))) != v:
            self.lyric_width_var.set(v)
        self.lyric_width_val.config(text=f"{v}%")

    def set_lyric_font_display(self, v: int) -> None:
        """同步字体大小滑块显示（同值时跳过）。"""
        if int(round(float(self.lyric_font_var.get()))) != v:
            self.lyric_font_var.set(v)
        self.lyric_font_val.config(text=f"{v}px")
    def _on_balance_changed(self, value: str) -> None:
        """声道平衡：-1 全左，0 居中，+1 全右（仅 sounddevice）。"""
        if self.app.engine.backend != "sounddevice":
            return
        v = round(float(value) / 0.05) * 0.05
        v = max(-1.0, min(1.0, v))
        self.balance_var.set(v)
        self.app.engine.set_balance(v)
        self.balance_val.config(text=f"{v:+.2f}")
        self.app._schedule_config_save()

    def _on_gain_changed(self, value: str) -> None:
        """响度增益（前置放大）：1.0 原始，最高 2.0（+6dB，仅 sounddevice）。"""
        if self.app.engine.backend != "sounddevice":
            return
        v = round(float(value) / 0.05) * 0.05
        v = max(0.0, min(2.0, v))
        self.gain_var.set(v)
        self.app.engine.set_gain(v)
        self.gain_val.config(text=f"{v:.2f}x", fg=self.gain_color(v))
        self.update_vol_preview()  # 增益变化同步音量显示
        self.app._schedule_config_save()

    def gain_color(self, v: float) -> str:
        """增益数值颜色：低于 1 偏灰，高于 1 偏黄（警示提升）。"""
        p = max(-1.0, min(1.0, v - 1))
        if p <= 0:
            r = int(0xC8 + (0x6D - 0xC8) * abs(p))
            g = int(0xC8 + (0x6D - 0xC8) * abs(p))
            b = int(0xC8 + (0x6D - 0xC8) * abs(p))
        else:
            r = int(0xC8 + (0xFF - 0xC8) * abs(p))
            g = int(0xC8 + (0xD5 - 0xC8) * abs(p))
            b = int(0xC8 + (0x4F - 0xC8) * abs(p))
        return "#{:02x}{:02x}{:02x}".format(r, g, b)

    # ==================================================================
    # 按钮组刷新（app 状态变化时调用）
    # ==================================================================

    def refresh_lyric_mode_buttons(self) -> None:
        """刷新桌面歌词三按钮选中态：当前模式高亮（ACCENT 底/深色字）。"""
        for mode, btn in self.lyric_mode_btns.items():
            active = (mode == self.app._lyric_bar_mode)
            try:
                btn.config(bg=self.ACCENT_COLOR if active else self.SUBTLE_COLOR,
                           fg=self.BG_COLOR if active else self.FG_COLOR)
            except tk.TclError:
                pass

    def refresh_mode_buttons(self) -> None:
        """刷新模式按钮组选中态：当前模式高亮（ACCENT 底/深色字）。"""
        for mode, btn in self.mode_btns.items():
            active = (mode == self.app.play_mode)
            try:
                btn.config(bg=self.ACCENT_COLOR if active else self.SUBTLE_COLOR,
                           fg=self.BG_COLOR if active else self.FG_COLOR)
            except tk.TclError:
                pass

    def refresh_topmost_buttons(self) -> None:
        """刷新置顶按钮组选中态：当前状态高亮（ACCENT 底/深色字）。"""
        for val, btn in self.topmost_btns.items():
            active = (bool(val) == bool(self.app.always_on_top))
            try:
                btn.config(bg=self.ACCENT_COLOR if active else self.SUBTLE_COLOR,
                           fg=self.BG_COLOR if active else self.FG_COLOR)
            except tk.TclError:
                pass

    # ==================================================================
    # 配置同步（启动恢复 / 重置默认共用）
    # ==================================================================

    def apply_values(self, *, speed: float, pitch_fix: bool, pitch_shift: int,
                     volume: int, lrc_offset: float, balance: float,
                     gain: float, lyric_alpha: int, lyric_width: int,
                     lyric_font: int) -> None:
        """把配置数值同步到控件显示（不触发回调、不保存）。"""
        self.speed_var.set(speed)
        self.speed_preview.config(text=f"{speed:.2f}x")

        self.pitch_btn.config(
            text="保音高: 开" if pitch_fix else "保音高: 关")
        self.pitch_var.set(pitch_shift)
        self.pitch_preview.config(text=f"{pitch_shift:+d}",
                                  fg=self.pitch_color(pitch_shift))

        self.vol_var.set(volume)
        self.update_vol_preview()

        self.lrc_offset_var.set(lrc_offset)
        self.lrc_offset_val.config(
            text=f"{int(round(lrc_offset * 10))}ms",
            fg=self.lrc_offset_color(lrc_offset))

        self.balance_var.set(balance)
        self.balance_val.config(text=f"{balance:+.2f}")

        self.gain_var.set(gain)
        self.gain_val.config(text=f"{gain:.2f}x", fg=self.gain_color(gain))

        self.set_lyric_alpha_display(lyric_alpha)
        self.set_lyric_width_display(lyric_width)
        self.set_lyric_font_display(lyric_font)
