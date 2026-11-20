"""Main Desktop Studio Window built on Fluent Blue (PyQt6) & Impeccable Operate principles."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import wave

from fluent_blue import (
    Badge,
    Button,
    Checkbox,
    Column,
    Command,
    CommandPalette,
    ConfirmDialog,
    DataTable,
    Divider,
    FilePicker,
    FluentWindow,
    FormField,
    IconButton,
    Input,
    LogViewer,
    MultiSelect,
    NumberInput,
    Panel,
    PasswordInput,
    ProgressBar,
    ScrollArea,
    SegmentedControl,
    Select,
    Sidebar,
    Slider,
    Splitter,
    Stack,
    Switch,
    Tabs,
    Text,
    TextArea,
    ThemeManager,
    Toast,
)
from fluent_blue.compatibility.qt import (
    QApplication,
    QAbstractItemView,
    QAudioOutput,
    QDesktopServices,
    QHBoxLayout,
    QMediaPlayer,
    QSizePolicy,
    QStackedWidget,
    Qt,
    QtCore,
    QUrl,
    QVBoxLayout,
    QWidget,
)
from fluent_blue.components.feedback.feedback import ToastCard, _OverlayHost

from ..core.config import AppSettings, project_root
from ..core.models import (
    CommentMode,
    JobStage,
    LayoutConfig,
    PresetProfile,
    STAGE_LABELS_VI,
    SegmentItem,
    VideoJob,
    VoiceAssignMode,
)
from ..core.pipeline import BatchPipelineManager
from ..render.ffmpeg_compositor import nvenc_available
from ..scrapers.threads_scraper import open_threads_login_session, sample_video1_script
from ..tts.router import TTSRouter, VoiceOption
from .widgets.stage_canvas import InteractiveStageCanvas
from .widgets.video_player import StudioVideoPlayer

Signal = getattr(QtCore, "pyqtSignal", None) or getattr(QtCore, "Signal")
logger = logging.getLogger("threadstudio")
logger.setLevel(logging.INFO)


class _TopCenterToastHost(_OverlayHost):
    """Fluent toast stack anchored at the horizontal top center."""

    def _relayout(self) -> None:
        super()._relayout()
        self.move((self._window.width() - self.width()) // 2, 0)


class _TopCenterToast:
    @staticmethod
    def show(message: str, parent=None, *, variant: str = "info", duration: int = 3500) -> ToastCard:
        window = parent.window() if parent is not None else QApplication.activeWindow()
        if window is None:
            raise ValueError("Cần cửa sổ ứng dụng để hiển thị thông báo.")
        name = "_threadstudio_top_center_toasts"
        host = window.findChild(
            _TopCenterToastHost,
            name,
            Qt.FindChildOption.FindDirectChildrenOnly,
        )
        if host is None:
            host = _TopCenterToastHost(window, "top-center")
            host.setObjectName(name)
        card = ToastCard(message=message, variant=variant, duration=duration)
        host.add(card)
        return card


# All existing Toast.show(...) calls now share the same top-center placement.
Toast = _TopCenterToast


class _AudioSeekSlider(Slider):
    """Seek slider that jumps directly to a clicked point and remains draggable."""

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            ratio = max(0.0, min(1.0, event.position().x() / self.width()))
            self.setValue(self.minimum() + round(ratio * (self.maximum() - self.minimum())))
        super().mousePressEvent(event)


class _AsyncUiBridge(QObject := QtCore.QObject):
    voice_preview_ready = Signal(str)
    voice_preview_failed = Signal(str)
    login_finished = Signal(str)
    audio_export_ready = Signal(str)
    audio_export_failed = Signal(str)
    audio_save_ready = Signal(str)
    audio_save_failed = Signal(str)


class ThreadsStudioWindow(FluentWindow):
    """Three-zone production shell: Sidebar Rail | Task Workspace | 9:16 Interactive Stage."""

    def __init__(self, settings: AppSettings) -> None:
        super().__init__(title="Threads Studio Pro — Tạo Video Đọc Bình Luận Đa Luồng", icon_name="layers")
        self.settings = settings
        self.pipeline = BatchPipelineManager(settings, self)
        self._ui_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ts-ui-helper")
        self._bridge = _AsyncUiBridge(self)

        self._voices: list[VoiceOption] = TTSRouter.available_voices()
        self._voice_label_by_id: dict[str, str] = {v.full_id: v.display_name for v in self._voices}
        self._current_inspect_job_id: str | None = None
        self._syncing_layout_controls = False
        self._syncing_segment_editor = False
        self._responsive_mode = ""
        self._log_is_compact = False
        self._sidebar_auto_collapsed = False

        # Audio preview player
        self._audio_player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._audio_player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(0.95)

        self.resize(1460, 900)
        self._build_shell()
        self._wire_signals()
        self._install_command_palette()
        self._refresh_queue_table()
        self._sync_layout_spinners_from_cfg()

        gpu_str = "GPU NVIDIA NVENC Sẵn sàng" if nvenc_available() else "CPU libx264 (Không phát hiện NVENC)"
        logger.info("Threads Studio Pro khởi động thành công · %d giọng AI · %s", len(self._voices), gpu_str)

    # ------------------------------------------------------------------
    # Shell Construction
    # ------------------------------------------------------------------

    def _build_shell(self) -> None:
        root = QWidget()
        root.setMinimumSize(0, 0)
        root.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._content_host.setMinimumSize(0, 0)
        self._content_host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        root_col = QVBoxLayout(root)
        pad = ThemeManager.spacing(2)
        root_col.setContentsMargins(pad, pad, pad, pad)
        root_col.setSpacing(ThemeManager.spacing(2))

        # 1. Top Operational Action Bar
        root_col.addWidget(self._build_top_bar())

        # 2. Horizontal 3-Zone Workspace: Sidebar | Center Pages | Right 9:16 Stage
        self._workspace_holder = QWidget()
        self._workspace_holder.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        ws_row = QHBoxLayout(self._workspace_holder)
        ws_row.setContentsMargins(0, 0, 0, 0)
        ws_row.setSpacing(ThemeManager.spacing(2))

        self._sidebar = Sidebar(width=216, collapsed_width=56)
        self._sidebar.add_section("Sản Xuất")
        self._nav_queue = self._sidebar.add_item("Tạo Lô · Hàng Đợi", key="queue", icon="layers")
        self._nav_inspector = self._sidebar.add_item("Duyệt Kịch Bản", key="inspector", icon="edit")
        self._nav_audio_export = self._sidebar.add_item("Tạo File Âm Thanh", key="audio_export", icon="volume")
        self._sidebar.add_section("Cấu Hình Studio")
        self._nav_voices = self._sidebar.add_item("Giọng Đọc AI", key="voices", icon="volume")
        self._nav_media = self._sidebar.add_item("Nguồn Video · Nhạc", key="media", icon="folder")
        self._nav_settings = self._sidebar.add_item("Cài Đặt · Đa Luồng", key="settings", icon="settings")
        ws_row.addWidget(self._sidebar)

        self._pages = QStackedWidget()
        self._pages.setMinimumSize(0, 0)
        self._pages.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._page_keys = ["queue", "inspector", "audio_export", "voices", "media", "settings"]
        page_widgets = [
            self._build_queue_page(),
            self._build_inspector_page(),
            self._build_audio_export_page(),
            self._build_voices_page(),
            self._build_media_page(),
            self._build_settings_page(),
        ]
        for page in page_widgets:
            if isinstance(page, ScrollArea):
                page.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._pages.addWidget(page)

        self._stage_panel = self._build_right_stage_panel()
        self._stage_panel.setMinimumSize(0, 0)
        self._stage_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._stage_scroll = ScrollArea(self._stage_panel)
        self._stage_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._stage_scroll.setMinimumSize(0, 0)
        self._stage_scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._center_stage_splitter = Splitter(
            widgets=[self._pages, self._stage_scroll],
            orientation="horizontal",
            sizes=[850, 390],
            collapsible=False,
        )
        ws_row.addWidget(self._center_stage_splitter, 1)

        # Keep an invisible logger sink for diagnostics; the technical log panel
        # is intentionally removed from the user-facing interface.
        self._log_panel = self._build_log_panel()
        self._log_panel.hide()

        root_col.addWidget(self._workspace_holder, 1)
        self.set_central_widget(root)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "_center_stage_splitter"):
            self._apply_responsive_layout(event.size().width(), event.size().height())

    def _apply_responsive_layout(self, width: int, height: int) -> None:
        mode = "compact" if width < 1100 else "medium" if width < 1500 else "wide"
        if mode != self._responsive_mode:
            self._responsive_mode = mode
            if mode in ("compact", "medium"):
                if not self._sidebar.is_collapsed():
                    self._sidebar.set_collapsed(True)
                    self._sidebar_auto_collapsed = True
                self._title_lbl.hide()
                self._encoder_badge.hide()
                self._btn_cmd_palette.hide()
                self._switch_dark.hide()
            if mode == "compact":
                self._canvas.setMinimumSize(180, 220)
                self._center_stage_splitter.setOrientation(Qt.Orientation.Vertical)
                available = max(500, self._center_stage_splitter.height())
                # Prioritize the working form on narrow screens so the entire
                # queue page fits without any scrolling. The stage stays below.
                self._pages.setMaximumHeight(max(520, int(available * 0.82)))
                self._center_stage_splitter.setSizes([int(available * 0.82), int(available * 0.18)])
            elif mode == "medium":
                self._pages.setMaximumHeight(16777215)
                self._canvas.setMinimumSize(220, 360)
                self._center_stage_splitter.setOrientation(Qt.Orientation.Horizontal)
                available = max(800, self._center_stage_splitter.width())
                self._center_stage_splitter.setSizes([int(available * 0.55), int(available * 0.45)])
            else:
                self._pages.setMaximumHeight(16777215)
                if self._sidebar_auto_collapsed:
                    self._sidebar.set_collapsed(False)
                    self._sidebar_auto_collapsed = False
                self._title_lbl.show()
                self._encoder_badge.show()
                self._btn_cmd_palette.show()
                self._switch_dark.show()
                self._canvas.setMinimumSize(250, 410)
                self._center_stage_splitter.setOrientation(Qt.Orientation.Horizontal)
                available = max(900, self._center_stage_splitter.width())
                self._center_stage_splitter.setSizes([int(available * 0.60), int(available * 0.40)])

        # Resize events can arrive before Qt has assigned the splitter its final
        # height. Recalculate on every event while compact, not only on mode change.
        if mode == "compact":
            available = max(500, self._center_stage_splitter.height())
            self._pages.setMaximumHeight(max(520, int(available * 0.82)))
            self._center_stage_splitter.setSizes([int(available * 0.82), int(available * 0.18)])

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setMinimumSize(0, 0)
        bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(bar)
        row.setContentsMargins(ThemeManager.spacing(1), 0, ThemeManager.spacing(1), 0)
        row.setSpacing(ThemeManager.spacing(2))

        self._btn_collapse_nav = IconButton(icon="sidebar", tooltip="Thu gọn / mở rộng thanh điều hướng")
        row.addWidget(self._btn_collapse_nav)

        self._title_lbl = Text("Threads Studio Pro", style="heading-4")
        row.addWidget(self._title_lbl)

        has_gpu = nvenc_available() and self.settings.prefer_nvenc
        self._encoder_badge = Badge(
            "NVENC 1080p" if has_gpu else "CPU x264 1080p",
            variant="success" if has_gpu else "info",
        )
        row.addWidget(self._encoder_badge)

        row.addStretch(1)

        preset_opts = [(p.name, i) for i, p in enumerate(self.settings.presets)]
        self._preset_select = Select(
            options=preset_opts,
            value=0 if preset_opts else None,
            placeholder="Chọn Preset chủ đề…",
            size="small",
        )
        self._preset_select.setMinimumWidth(160)
        row.addWidget(self._preset_select)

        self._btn_apply_preset = Button("Nạp Preset", size="small", variant="secondary", icon="wand")
        row.addWidget(self._btn_apply_preset)

        self._btn_cmd_palette = Button("Lệnh nhanh (Ctrl+K)", size="small", variant="ghost", icon="search")
        row.addWidget(self._btn_cmd_palette)

        self._switch_dark = Switch("Giao diện tối", checked=(ThemeManager.current() == "dark"))
        row.addWidget(self._switch_dark)

        self._btn_open_outputs = Button("Kết quả", size="small", variant="outline", icon="folder-open")
        row.addWidget(self._btn_open_outputs)
        return bar

    # ------------------------------------------------------------------
    # Page 1: Batch Ingestion & Multi-Threaded Queue
    # ------------------------------------------------------------------

    def _build_queue_page(self) -> QWidget:
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, ThemeManager.spacing(1), 0)
        col.setSpacing(ThemeManager.spacing(2))

        # Section A: Batch Input Panel
        btn_demo = Button("Bài Mẫu Video 1", size="small", variant="secondary", icon="zap")
        btn_paste = Button("Dán Link", size="small", variant="ghost", icon="copy")
        btn_login_quick = Button("Đăng nhập Threads", size="small", variant="outline", icon="user")
        ingest_panel = Panel(
            title="Nhập Bài Viết Threads (Đơn Lẻ Hoặc Chạy Lô)",
            actions=[btn_login_quick, btn_demo, btn_paste],
            padding=2,
        )

        self._input_mode_seg = SegmentedControl(
            options=[
                ("Dán Lô Link Threads", "urls"),
                ("Tự Nhập Kịch Bản", "manual"),
            ],
            value="urls",
        )
        ingest_panel.add(self._input_mode_seg)

        self._batch_input_area = TextArea(
            placeholder=(
                "Dán 1 hoặc 50 đường link bài viết Threads (mỗi dòng 1 link)...\n"
                "Ví dụ:\nhttps://www.threads.com/@user/post/ABC123\nhttps://www.threads.com/share/XYZ789"
            ),
            rows=4,
        )
        self._batch_input_area.setFixedHeight(138)
        ingest_panel.add(self._batch_input_area)

        # Quick row: Comment mode, comment count, speed
        opts_row = Stack(direction="horizontal", spacing=3, align="center")

        self._cmt_mode_select = Select(
            options=[
                ("Nhiều tim · Gồm cả tác giả", CommentMode.TOP_LIKES_WITH_AUTHOR.value),
                ("Nhiều tim · Không lấy tác giả", CommentMode.TOP_LIKES_NO_AUTHOR.value),
                ("Chỉ bình luận của tác giả (Kể chuyện)", CommentMode.AUTHOR_ONLY.value),
            ],
            value=self.settings.comment_mode,
        )
        opts_row.add(FormField("Chế độ lấy bình luận", self._cmt_mode_select), 3)

        self._cmt_count_spin = NumberInput(
            value=self.settings.max_comments,
            minimum=1,
            maximum=25,
            step=1,
            decimals=0,
        )
        opts_row.add(FormField("Số bình luận", self._cmt_count_spin), 1)

        self._speed_quick_spin = NumberInput(
            value=self.settings.voice_speed,
            minimum=0.75,
            maximum=1.80,
            step=0.05,
            decimals=2,
        )
        opts_row.add(FormField("Tốc độ đọc", self._speed_quick_spin), 1)
        ingest_panel.add(opts_row)

        flags_cta_row = Stack(direction="horizontal", spacing=3, align="center")
        self._chk_hide_name = Checkbox("Che tên tài khoản", checked=self.settings.layout.hide_username)
        self._chk_require_review = Checkbox(
            "Duyệt và sửa từ viết tắt trước khi xuất",
            checked=self.settings.require_review_default,
        )
        flags_cta_row.add(self._chk_hide_name)
        flags_cta_row.add(self._chk_require_review)
        flags_cta_row.add_stretch(1)

        self._btn_enqueue = Button(
            "Tạo Video Đa Luồng",
            variant="primary",
            size="medium",
            icon="play",
        )
        flags_cta_row.add(self._btn_enqueue)
        ingest_panel.add(flags_cta_row)
        col.addWidget(ingest_panel)

        # Section B: Batch Queue DataTable
        self._btn_approve_all = Button("Duyệt Tất Cả Chờ", size="small", variant="primary", icon="check-circle")
        self._btn_inspect_selected = Button("Sửa Kịch Bản", size="small", variant="secondary", icon="edit")
        self._btn_watch_selected = Button("Xem MP4", size="small", variant="outline", icon="play")
        self._btn_stop_all = Button("Dừng Lô", size="small", variant="danger", icon="stop")

        queue_panel = Panel(
            title="Hàng Đợi Xử Lý Đa Luồng",
            actions=[
                self._btn_inspect_selected,
                self._btn_approve_all,
                self._btn_watch_selected,
                self._btn_stop_all,
            ],
            padding=2,
        )

        self._batch_progress = ProgressBar(value=0, show_label=True, thickness=6)
        queue_panel.add(self._batch_progress)

        self._queue_table = DataTable(
            columns=[
                Column("job_id", "Mã", width=82),
                Column("slug", "Bài viết / Chủ đề", width=185),
                Column("segments", "Số đoạn", width=80, align="center"),
                Column("stage_label", "Khâu hiện tại", width=155),
                Column("progress", "Tiến độ", width=85, align="right", formatter=lambda v: f"{int(v)}%"),
                Column("detail", "Chi tiết trạng thái", width=260),
            ],
            rows=[],
            page_size=None,
            selection="single",
            searchable=True,
            exportable=False,
            empty_title="Chưa có video nào trong hàng đợi",
            empty_description="Dán danh sách link Threads hoặc bấm 'Nạp Bài Mẫu Video 1' để chạy thử ngay.",
        )
        self._queue_table.setMinimumHeight(185)
        queue_panel.add(self._queue_table)
        col.addWidget(queue_panel, 1)

        # Wire local buttons
        btn_demo.clicked.connect(self._load_demo_script)
        btn_paste.clicked.connect(self._paste_links_from_clipboard)
        btn_login_quick.clicked.connect(self._start_threads_login)

        return container

    # ------------------------------------------------------------------
    # Page 2: Inline Script & Teencode Inspector (No Blocking Modal!)
    # ------------------------------------------------------------------

    def _build_inspector_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, ThemeManager.spacing(2), 0)
        col.setSpacing(ThemeManager.spacing(2))

        self._btn_re_normalize = Button("Bung Lại Từ Viết Tắt", size="small", variant="secondary", icon="wand")
        self._btn_approve_current = Button(
            "Duyệt Xong · Xuất Video Ngay",
            size="medium",
            variant="primary",
            icon="check",
        )
        header_panel = Panel(
            title="Duyệt · Chỉnh Sửa Kịch Bản Trước Khi Xuất",
            actions=[self._btn_re_normalize, self._btn_approve_current],
            padding=3,
        )

        job_pick_row = Stack(direction="horizontal", spacing=2, align="center")
        job_pick_row.add(Text("Đang kiểm tra Job:", style="label", color="text-secondary"))
        self._inspect_job_select = Select(options=[], placeholder="Chọn một Job trong hàng đợi…")
        job_pick_row.add(self._inspect_job_select, 1)
        header_panel.add(job_pick_row)
        col.addWidget(header_panel)

        # Split left segment table vs right text & voice editor
        split = Splitter(orientation="horizontal", collapsible=False)

        left_box = Panel(title="Thứ Tự Xuất Hiện", padding=3)
        self._seg_table = DataTable(
            columns=[
                Column("idx_label", "#", width=54, sortable=False),
                Column("meta", "Tác giả / Tim", width=110, sortable=False),
                Column("snippet", "Nội dung", width=170, sortable=False),
            ],
            rows=[],
            page_size=None,
            selection="single",
            searchable=False,
            column_menu=False,
            exportable=False,
            empty_title="Chưa chọn Job",
            empty_description="Chọn 1 bài trong Hàng Đợi để sửa lời đọc.",
            row_height=34,
        )
        left_box.add(self._seg_table, 1)

        order_btns = Stack(direction="horizontal", spacing=2)
        self._btn_seg_up = Button("Lên", size="small", icon="arrow-up", full_width=True)
        self._btn_seg_down = Button("Xuống", size="small", icon="arrow-down", full_width=True)
        order_btns.add(self._btn_seg_up, 1)
        order_btns.add(self._btn_seg_down, 1)
        left_box.add(order_btns)

        right_box = Panel(title="Chi Tiết Đoạn · Lời Đọc TTS", padding=3)
        top_seg_flags = Stack(direction="horizontal", spacing=2, align="center")
        self._chk_seg_enabled = Checkbox("Đọc và hiển thị đoạn này trong video", checked=True)
        top_seg_flags.add(self._chk_seg_enabled)
        top_seg_flags.add_stretch(1)
        self._btn_preview_seg_voice = Button("Nghe thử đoạn này", size="small", variant="outline", icon="volume")
        top_seg_flags.add(self._btn_preview_seg_voice)
        right_box.add(top_seg_flags)

        voice_opts = [(lbl, vid) for vid, lbl in self._voice_label_by_id.items()]
        self._seg_voice_select = Select(options=voice_opts, placeholder="Chọn giọng đọc riêng cho đoạn này…")
        right_box.add(FormField("Giọng đọc của đoạn này", self._seg_voice_select))

        self._seg_spoken_area = TextArea(
            placeholder="Lời đọc TTS (đã tự sửa từ viết tắt ko -> không, đc -> được)...",
            rows=4,
        )
        right_box.add(
            FormField(
                "Lời đọc AI (Có thể chỉnh sửa trực tiếp để máy đọc chuẩn 100%)",
                self._seg_spoken_area,
            )
        )

        self._seg_raw_area = TextArea(
            placeholder="Chữ hiển thị trên thẻ bình luận trắng/tối...",
            rows=4,
        )
        right_box.add(FormField("Chữ hiển thị trên Thẻ Threads (Xem trực tiếp bên phải)", self._seg_raw_area))

        nav_seg_btns = Stack(direction="horizontal", spacing=2)
        self._btn_prev_seg = Button("Đoạn trước", size="small", icon="chevron-left")
        self._btn_next_seg = Button("Đoạn tiếp", size="small", icon="chevron-right", icon_position="right")
        nav_seg_btns.add(self._btn_prev_seg)
        nav_seg_btns.add(self._btn_next_seg)
        nav_seg_btns.add_stretch(1)
        right_box.add(nav_seg_btns)

        split.addWidget(left_box)
        split.addWidget(right_box)
        split.setSizes([335, 495])
        col.addWidget(split, 1)
        return page

    # ------------------------------------------------------------------
    # Page 3: Text-to-Audio Only Export
    # ------------------------------------------------------------------

    def _build_audio_export_page(self) -> QWidget:
        stale_preview = self.settings.user_data_dir() / "work" / "audio-only-preview.wav"
        try:
            stale_preview.unlink(missing_ok=True)
        except PermissionError:
            pass
        for old_preview in stale_preview.parent.glob("audio-preview-*.wav"):
            try:
                old_preview.unlink(missing_ok=True)
            except PermissionError:
                pass
        self._audio_preview_path = ""
        self._audio_preview_request = ""
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, ThemeManager.spacing(2), 0)
        col.setSpacing(ThemeManager.spacing(2))

        self._btn_normalize_audio_text = Button(
            "Bung Lại Từ Viết Tắt", size="small", variant="secondary", icon="wand"
        )
        panel = Panel(
            title="Lồng Tiếng Từ Văn Bản · Chỉ Xuất File Âm Thanh",
            actions=[self._btn_normalize_audio_text],
            padding=4,
        )
        voice_options = [(voice.display_name, voice.full_id) for voice in self._voices]
        default_voice = self.settings.preview_voice_id
        if default_voice not in {value for _, value in voice_options} and voice_options:
            default_voice = voice_options[0][1]

        audio_split = Splitter(orientation="horizontal", collapsible=False)
        segment_list_panel = Panel(title="Danh Sách Câu · Đọc Theo Thứ Tự", padding=2)
        self._audio_segments_table = DataTable(
            columns=[
                Column("number", "#", width=48, align="center", sortable=False),
                Column("snippet", "Nội dung", width=300, sortable=False),
                Column("voice_label", "Giọng", width=150, sortable=False),
            ],
            rows=[], page_size=None, selection="single", searchable=False,
            column_menu=False, exportable=False, empty_title="Chưa có câu nào",
            empty_description="Bấm Thêm Câu để bắt đầu.", row_height=34,
        )
        self._audio_segments_table.setMinimumHeight(230)
        segment_list_panel.add(self._audio_segments_table)
        segment_buttons = Stack(direction="horizontal", spacing=2)
        self._btn_audio_add_segment = Button("Thêm Câu", size="small", variant="primary", icon="plus")
        self._btn_audio_delete_segment = Button("Xóa Câu", size="small", variant="danger", icon="trash")
        self._btn_audio_segment_up = Button("Lên", size="small", icon="arrow-up")
        self._btn_audio_segment_down = Button("Xuống", size="small", icon="arrow-down")
        for button in (
            self._btn_audio_add_segment, self._btn_audio_delete_segment,
            self._btn_audio_segment_up, self._btn_audio_segment_down,
        ):
            segment_buttons.add(button)
        segment_list_panel.add(segment_buttons)

        segment_editor_panel = Panel(title="Nội Dung Và Giọng Của Câu Đang Chọn", padding=3)
        self._audio_export_text = TextArea(
            placeholder="Nhập nội dung của câu hoặc đoạn này…",
            rows=6,
        )
        self._audio_export_text.setFixedHeight(180)
        segment_editor_panel.add(FormField("Nội dung cần đọc", self._audio_export_text))
        self._audio_export_voice = Select(options=voice_options, value=default_voice)
        segment_editor_panel.add(FormField("Giọng đọc riêng cho câu này", self._audio_export_voice))

        audio_split.addWidget(segment_list_panel)
        audio_split.addWidget(segment_editor_panel)
        audio_split.setSizes([470, 430])
        panel.add(audio_split)

        self._audio_export_speed = NumberInput(
            value=self.settings.voice_speed,
            minimum=0.75,
            maximum=1.80,
            step=0.05,
            decimals=2,
        )
        voice_row = Stack(direction="horizontal", spacing=3)
        voice_row.add(FormField("Tốc độ chung cho toàn bộ file", self._audio_export_speed), 1)
        voice_row.add_stretch(3)
        panel.add(voice_row)

        self._audio_export_name = Input(
            placeholder="Tên file, ví dụ: loi-thoai-quang-cao",
            clearable=True,
        )
        self._audio_export_format = Select(
            options=[("MP3 · Dung lượng nhẹ", "mp3"), ("WAV · Chất lượng gốc", "wav")],
            value="mp3",
        )
        output_row = Stack(direction="horizontal", spacing=3)
        output_row.add(FormField("Tên file", self._audio_export_name), 3)
        output_row.add(FormField("Định dạng", self._audio_export_format), 1)
        panel.add(output_row)

        self._btn_export_audio_only = Button(
            "Nghe Thử",
            variant="primary",
            size="medium",
            icon="play",
        )
        self._btn_export_audio_only.setFixedWidth(160)
        self._btn_save_audio_only = Button(
            "Lưu File Âm Thanh",
            variant="secondary",
            size="medium",
            icon="save",
        )
        self._btn_save_audio_only.setFixedWidth(190)
        self._btn_save_audio_only.setEnabled(False)
        audio_actions = Stack(direction="horizontal", spacing=2)
        audio_actions.add(self._btn_export_audio_only)
        audio_actions.add(self._btn_save_audio_only)
        audio_actions.add_stretch(1)
        panel.add(audio_actions)

        audio_seek_row = Stack(direction="horizontal", spacing=2, align="center")
        self._audio_seek_slider = _AudioSeekSlider(value=0, minimum=0, maximum=1, disabled=True)
        self._audio_time_label = Text("00:00 / 00:00", color="text-secondary")
        self._audio_time_label.setMinimumWidth(105)
        audio_seek_row.add(self._audio_seek_slider, 1)
        audio_seek_row.add(self._audio_time_label)
        panel.add(audio_seek_row)
        panel.add(
            Text(
                "File hoàn thành được lưu trực tiếp trong thư mục Kết quả; không tạo video hay giữ dữ liệu tạm.",
                color="text-secondary",
                wrap=True,
            )
        )
        self._audio_segments = [
            {"text": "", "voice_id": default_voice, "voice_label": self._voice_label_by_id.get(default_voice, "Giọng đọc")}
        ]
        self._current_audio_segment = 0
        self._syncing_audio_segment = False
        self._refresh_audio_segments_table(select_index=0)
        col.addWidget(panel)
        col.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # Page 4: Multi-Engine AI Voice Studio (CapCut + Edge + VieNeu)
    # ------------------------------------------------------------------

    def _build_voices_page(self) -> QWidget:
        scroll = ScrollArea()
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, ThemeManager.spacing(2), 0)
        col.setSpacing(ThemeManager.spacing(3))

        # Voice Audition Panel
        audition_panel = Panel(title="Nghe Thử Giọng Đọc AI (CapCut · Edge · VieNeu-TTS)", padding=4)
        voice_opts = [(lbl, vid) for vid, lbl in self._voice_label_by_id.items()]
        valid_preview = (
            self.settings.preview_voice_id
            if self.settings.preview_voice_id in self._voice_label_by_id
            else (voice_opts[0][1] if voice_opts else None)
        )
        self._audition_voice_select = Select(options=voice_opts, value=valid_preview)
        audition_panel.add(FormField("Chọn giọng nghe thử", self._audition_voice_select))

        sample_row = Stack(direction="horizontal", spacing=2, align="center")
        self._audition_input = Input(
            text="Có ai thích từ nức nở trong tiếng Việt giống mình không, nghe vừa sâu lắng vừa da diết.",
            clearable=True,
        )
        sample_row.add(self._audition_input, 1)
        self._btn_audition_play = Button("Nghe thử", variant="primary", icon="volume")
        sample_row.add(self._btn_audition_play)
        audition_panel.add(sample_row)
        col.addWidget(audition_panel)

        # Multi-Voice Pool Selection for Video Conversations
        pool_panel = Panel(title="Bộ Giọng Phối Hợp Trong Video (Đọc Đa Giọng Tự Nhiên)", padding=4)
        valid_selected = [v for v in self.settings.selected_voices if v in self._voice_label_by_id]
        if not valid_selected and voice_opts:
            valid_selected = [voice_opts[0][1]]

        self._voice_pool_multiselect = MultiSelect(
            options=voice_opts,
            values=valid_selected,
            placeholder="Tích chọn các giọng muốn phối hợp trong video…",
            max_labels=3,
        )
        pool_panel.add(
            FormField(
                "Chọn giọng đọc cho video",
                self._voice_pool_multiselect,
            )
        )

        mix_row = Stack(direction="horizontal", spacing=3)
        self._voice_assign_select = Select(
            options=[
                ("Ngẫu nhiên đổi giọng mỗi bình luận", VoiceAssignMode.RANDOM.value),
                ("Luân phiên theo vòng lặp", VoiceAssignMode.ROUND_ROBIN.value),
                ("Giữ 1 giọng duy nhất (Hợp kể chuyện)", VoiceAssignMode.SINGLE.value),
            ],
            value=self.settings.voice_assign_mode,
        )
        mix_row.add(FormField("Cách phối giọng giữa các thẻ", self._voice_assign_select), 2)
        pool_panel.add(mix_row)
        col.addWidget(pool_panel)

        # VieNeu-TTS v3 Turbo Instant Voice Cloning Panel
        vieneu_status = "Đã cài SDK vieneu" if TTSRouter.vieneu_installed() else "Chạy qua Local Endpoint hoặc cài `pip install vieneu`"
        clone_panel = Panel(
            title="Nhân Bản Giọng Đọc Riêng (VieNeu-TTS Instant Clone 3–8s)",
            actions=[Badge(vieneu_status, variant="info")],
            padding=4,
        )
        self._clone_audio_picker = FilePicker(
            mode="open",
            filter="Audio (*.wav *.mp3 *.flac *.m4a)",
            placeholder="Chọn mẫu ghi âm 3–8 giây giọng gốc sạch nhiễu…",
            button_text="Chọn file mẫu",
        )
        if self.settings.vieneu_clone_ref_audio:
            self._clone_audio_picker.set_paths([self.settings.vieneu_clone_ref_audio])
        clone_panel.add(FormField("File âm thanh mẫu (Reference Audio)", self._clone_audio_picker))

        self._clone_ref_text = Input(
            text=self.settings.vieneu_clone_ref_text,
            placeholder="Nhập đúng câu thoại nói trong file mẫu 3–8s ở trên…",
            clearable=True,
        )
        clone_panel.add(FormField("Nội dung chữ của file mẫu (Reference Transcript)", self._clone_ref_text))

        self._vieneu_endpoint_input = Input(
            text=self.settings.vieneu_endpoint,
            placeholder="Để trống nếu dùng SDK Python `vieneu` trực tiếp, hoặc nhập http://127.0.0.1:8000",
            clearable=True,
        )
        clone_panel.add(FormField("VieNeu HTTP Server (Tuỳ chọn)", self._vieneu_endpoint_input))
        col.addWidget(clone_panel)
        col.addStretch(1)

        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Page 4: Media Folders & Audio Mixing Presets
    # ------------------------------------------------------------------

    def _build_media_page(self) -> QWidget:
        scroll = ScrollArea()
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, ThemeManager.spacing(2), 0)
        col.setSpacing(ThemeManager.spacing(3))

        # These are application-managed libraries; users add files through the
        # UI and never need to browse or configure their folder paths.
        self.settings.media.bg_video_dir = str(self._managed_media_library("video nền"))
        self.settings.media.sub_video_dir = str(self._managed_media_library("video phụ"))
        self.settings.media.bgm_dir = str(self._managed_media_library("nhạc nền"))

        btn_save_preset = Button("Lưu Preset Mới", size="small", variant="primary", icon="save")
        self._btn_delete_preset = Button("Xóa Preset", size="small", variant="danger", icon="trash")
        folders_panel = Panel(
            title="Nguồn Video & Âm Thanh",
            actions=[self._btn_delete_preset, btn_save_preset],
            padding=4,
        )

        self._picker_bg_file = FilePicker(
            mode="open-multiple",
            filter="Video (*.mp4 *.mov *.mkv *.webm *.avi)",
            placeholder="Các video đã thêm sẽ được ứng dụng tự quản lý…",
            button_text="Thêm Video Nền",
        )
        self._bg_library_status = Text("", color="text-secondary")

        self._btn_add_bg_library = Button(
            "Thêm Video Nền", size="small", variant="primary", icon="plus"
        )
        self._btn_preview_bg_library = Button(
            "Xem Video Đã Chọn", size="small", variant="outline", icon="play"
        )
        self._btn_delete_bg_library = Button("Xóa Đã Chọn", size="small", variant="danger", icon="trash")
        video_library_panel = Panel(
            title="Tất Cả Video Nền Đã Thêm",
            actions=[self._btn_add_bg_library, self._btn_preview_bg_library, self._btn_delete_bg_library],
            padding=2,
        )
        video_library_panel.add(self._bg_library_status)
        self._bg_library_table = DataTable(
            columns=[
                Column("checked", "", width=44, sortable=False, align="center"),
                Column("name", "Tên video", width=360),
                Column("size", "Dung lượng", width=110, align="right"),
            ],
            rows=[],
            page_size=None,
            selection="single",
            searchable=True,
            exportable=False,
            empty_title="Chưa thêm video nền",
            empty_description="Bấm 'Thêm Video Nền' để đưa video vào ứng dụng.",
            row_height=34,
        )
        self._bg_library_table.setMinimumHeight(170)
        self._enable_checkbox_selection(self._bg_library_table)
        video_library_panel.add(self._bg_library_table)
        folders_panel.add(video_library_panel)

        self._chk_sub_video = Checkbox(
            "Bật Video Phụ (Meme / Anime / Gameplay tắt tiếng gốc)",
            checked=self.settings.layout.sub_enabled,
        )
        folders_panel.add(self._chk_sub_video)

        self._picker_sub_file = FilePicker(
            mode="open-multiple",
            filter="Video (*.mp4 *.mov *.mkv *.webm *.avi)",
            button_text="Thêm Video Phụ",
        )
        self._sub_library_status = Text("", color="text-secondary")
        self._btn_add_sub_library = Button("Thêm Video Phụ", size="small", variant="primary", icon="plus")
        self._btn_preview_sub_library = Button("Xem Video Đã Chọn", size="small", variant="outline", icon="play")
        self._btn_delete_sub_library = Button("Xóa Đã Chọn", size="small", variant="danger", icon="trash")
        sub_library_panel = Panel(
            title="Tất Cả Video Phụ · Meme · Anime · Gameplay",
            actions=[self._btn_add_sub_library, self._btn_preview_sub_library, self._btn_delete_sub_library],
            padding=2,
        )
        sub_library_panel.add(self._sub_library_status)
        self._sub_library_table = DataTable(
            columns=[
                Column("checked", "", width=44, sortable=False, align="center"),
                Column("name", "Tên video", width=360),
                Column("size", "Dung lượng", width=110, align="right"),
            ],
            rows=[], page_size=None, selection="single", searchable=True,
            exportable=False, empty_title="Chưa thêm video phụ",
            empty_description="Bấm 'Thêm Video Phụ' để đưa video vào ứng dụng.", row_height=34,
        )
        self._sub_library_table.setMinimumHeight(150)
        self._enable_checkbox_selection(self._sub_library_table)
        sub_library_panel.add(self._sub_library_table)
        folders_panel.add(sub_library_panel)

        folders_panel.add(Divider())

        self._picker_bgm_file = FilePicker(
            mode="open-multiple",
            filter="Âm thanh (*.mp3 *.wav *.m4a *.aac *.ogg *.flac)",
            placeholder="Các bài nhạc đã thêm sẽ được ứng dụng tự quản lý…",
            button_text="Thêm Nhạc Nền",
        )
        self._bgm_library_status = Text("", color="text-secondary")
        self._btn_add_bgm_library = Button("Thêm Nhạc Nền", size="small", variant="primary", icon="plus")
        self._btn_preview_bgm_library = Button("Nghe Nhạc Đã Chọn", size="small", variant="outline", icon="play")
        self._btn_delete_bgm_library = Button("Xóa Đã Chọn", size="small", variant="danger", icon="trash")
        bgm_library_panel = Panel(
            title="Tất Cả Nhạc Nền Đã Thêm",
            actions=[self._btn_add_bgm_library, self._btn_preview_bgm_library, self._btn_delete_bgm_library],
            padding=2,
        )
        bgm_library_panel.add(self._bgm_library_status)
        self._bgm_library_table = DataTable(
            columns=[
                Column("checked", "", width=44, sortable=False, align="center"),
                Column("name", "Tên bài nhạc", width=360),
                Column("size", "Dung lượng", width=110, align="right"),
            ],
            rows=[], page_size=None, selection="single", searchable=True,
            exportable=False, empty_title="Chưa thêm nhạc nền",
            empty_description="Bấm 'Thêm Nhạc Nền' để đưa nhạc vào ứng dụng.", row_height=34,
        )
        self._bgm_library_table.setMinimumHeight(150)
        self._enable_checkbox_selection(self._bgm_library_table)
        bgm_library_panel.add(self._bgm_library_table)
        folders_panel.add(bgm_library_panel)

        bgm_mix_row = Stack(direction="horizontal", spacing=3, align="center")
        self._chk_bgm = Checkbox("Bật Nhạc Nền", checked=self.settings.media.bgm_enabled)
        self._slider_bgm = Slider(value=self.settings.media.bgm_volume_pct, minimum=0, maximum=100)
        self._spin_bgm = NumberInput(value=self.settings.media.bgm_volume_pct, minimum=0, maximum=100, step=5)
        self._spin_bgm.setMaximumWidth(95)
        bgm_mix_row.add(self._chk_bgm)
        bgm_mix_row.add(self._slider_bgm, 1)
        bgm_mix_row.add(self._spin_bgm)
        bgm_mix_row.add(Text("%", color="text-secondary"))
        folders_panel.add(bgm_mix_row)

        self._picker_sfx = FilePicker(mode="directory", button_text="Chọn thư mục")
        self._picker_sfx.set_paths([self.settings.media.sfx_dir])
        folders_panel.add(
            FormField(
                "Thư mục Hiệu Ứng Chuyển Cảnh SFX (Tắt mục này nếu làm video kể chuyện trầm lắng)",
                self._picker_sfx,
            )
        )

        sfx_mix_row = Stack(direction="horizontal", spacing=3, align="center")
        self._chk_sfx = Checkbox("Bật Hiệu Ứng Chuyển Cảnh", checked=self.settings.media.sfx_enabled)
        self._slider_sfx = Slider(value=self.settings.media.sfx_volume_pct, minimum=0, maximum=100)
        self._spin_sfx = NumberInput(value=self.settings.media.sfx_volume_pct, minimum=0, maximum=100, step=5)
        self._spin_sfx.setMaximumWidth(95)
        sfx_mix_row.add(self._chk_sfx)
        sfx_mix_row.add(self._slider_sfx, 1)
        sfx_mix_row.add(self._spin_sfx)
        sfx_mix_row.add(Text("%", color="text-secondary"))
        folders_panel.add(sfx_mix_row)

        preset_save_row = Stack(direction="horizontal", spacing=2, align="center")
        self._new_preset_name = Input(placeholder="Đặt tên Preset mới (VD: Kênh Tâm Sự Đêm Khuya)…", clearable=True)
        preset_save_row.add(self._new_preset_name, 1)
        folders_panel.add(preset_save_row)

        col.addWidget(folders_panel)
        col.addStretch(1)

        btn_save_preset.clicked.connect(self._save_current_as_preset)
        self._btn_delete_preset.clicked.connect(self._delete_selected_preset)
        self._picker_bg_file.files_selected.connect(
            lambda paths: self._import_media_into_library(paths, "video nền")
        )
        self._picker_bgm_file.files_selected.connect(
            lambda paths: self._import_media_into_library(paths, "nhạc nền")
        )
        self._picker_sub_file.files_selected.connect(
            lambda paths: self._import_media_into_library(paths, "video phụ")
        )
        self._btn_preview_bg_library.clicked.connect(self._preview_selected_background_video)
        self._btn_add_bg_library.clicked.connect(self._picker_bg_file.browse)
        self._bg_library_table.row_activated.connect(self._preview_background_video_row)
        self._btn_delete_bg_library.clicked.connect(
            lambda: self._delete_selected_media(self._bg_library_table, "video nền")
        )
        self._btn_add_sub_library.clicked.connect(self._picker_sub_file.browse)
        self._btn_preview_sub_library.clicked.connect(
            lambda: self._preview_selected_library_video(self._sub_library_table, "video phụ")
        )
        self._sub_library_table.row_activated.connect(self._preview_background_video_row)
        self._btn_delete_sub_library.clicked.connect(
            lambda: self._delete_selected_media(self._sub_library_table, "video phụ")
        )
        self._btn_add_bgm_library.clicked.connect(self._picker_bgm_file.browse)
        self._btn_preview_bgm_library.clicked.connect(self._preview_selected_background_music)
        self._bgm_library_table.row_activated.connect(self._preview_background_music_row)
        self._btn_delete_bgm_library.clicked.connect(
            lambda: self._delete_selected_media(self._bgm_library_table, "nhạc nền")
        )
        self._refresh_media_library_status()
        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Page 5: Credentials, Threads Session & Multi-Thread Worker Pool
    # ------------------------------------------------------------------

    def _build_settings_page(self) -> QWidget:
        scroll = ScrollArea()
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, ThemeManager.spacing(2), 0)
        col.setSpacing(ThemeManager.spacing(3))

        auth_panel = Panel(title="Tài Khoản Threads & AI Chuẩn Hóa Teencode", padding=4)
        login_row = Stack(direction="horizontal", spacing=3, align="center")
        self._btn_login_threads = Button("Mở Trình Duyệt Đăng Nhập Threads", variant="primary", icon="globe")
        login_row.add(self._btn_login_threads)
        login_row.add(
            Text(
                "Đăng nhập 1 lần để lưu cookie vào user_data/threads_profile — cào được toàn bộ bình luận ẩn.",
                color="text-secondary",
                wrap=True,
            ),
            1,
        )
        auth_panel.add(login_row)

        self._gemini_key_input = PasswordInput(
            text=self.settings.gemini_api_key,
            placeholder="AIzaSy... (Để trống vẫn chạy tốt bằng bộ từ điển Teencode nội bộ)",
            clearable=True,
        )
        auth_panel.add(
            FormField(
                "Gemini API Key (Nâng độ chính xác sửa viết tắt lên 98% + Đọc chữ trong ảnh meme chat)",
                self._gemini_key_input,
            )
        )

        self._capcut_dev_picker = FilePicker(
            mode="open",
            filter="JSON (*.json)",
            placeholder="Mặc định tự cấp thiết bị ảo CapCut (chỉ chọn nếu muốn dùng device.json riêng)",
            button_text="Chọn JSON",
        )
        if self.settings.capcut_device_json:
            self._capcut_dev_picker.set_paths([self.settings.capcut_device_json])
        auth_panel.add(FormField("CapCut Device Config (Tuỳ chọn)", self._capcut_dev_picker))
        col.addWidget(auth_panel)

        # Concurrency & Hardware Acceleration Panel
        perf_panel = Panel(title="Cấu Hình Đa Luồng & Tăng Tốc Phần Cứng GPU", padding=4)
        workers_row = Stack(direction="horizontal", spacing=3)

        self._spin_w_scrape = NumberInput(value=self.settings.max_scrape_workers, minimum=1, maximum=6, step=1)
        self._spin_w_ai = NumberInput(value=self.settings.max_ai_workers, minimum=1, maximum=12, step=1)
        self._spin_w_tts = NumberInput(value=self.settings.max_tts_workers, minimum=1, maximum=12, step=1)
        self._spin_w_render = NumberInput(value=self.settings.max_render_workers, minimum=1, maximum=6, step=1)

        workers_row.add(FormField("Luồng Cào Threads", self._spin_w_scrape), 1)
        workers_row.add(FormField("Luồng Gemini AI", self._spin_w_ai), 1)
        workers_row.add(FormField("Luồng Tạo Voice TTS", self._spin_w_tts), 1)
        workers_row.add(FormField("Luồng Ghép FFmpeg", self._spin_w_render), 1)
        perf_panel.add(workers_row)

        self._switch_nvenc = Switch(
            "Ưu tiên mã hóa phần cứng NVIDIA NVENC (h264_nvenc) khi xuất hàng loạt",
            checked=self.settings.prefer_nvenc,
        )
        perf_panel.add(self._switch_nvenc)

        self._picker_out_root = FilePicker(mode="directory", button_text="Đổi thư mục")
        self._picker_out_root.set_paths([str(self.settings.resolved_output_root())])
        perf_panel.add(FormField("Thư mục lưu Video xuất ra", self._picker_out_root))

        self._btn_save_settings = Button("Lưu Toàn Bộ Cài Đặt", variant="primary", icon="save")
        perf_panel.add(self._btn_save_settings)
        col.addWidget(perf_panel)
        col.addStretch(1)

        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Persistent Right 9:16 Stage & Layout Inspector
    # ------------------------------------------------------------------

    def _build_right_stage_panel(self) -> QWidget:
        btn_snap = Button("Chụp thử ảnh", size="small", variant="ghost", icon="image")
        btn_reset_layout = Button("Đặt lại", size="small", variant="ghost", icon="refresh")
        stage_panel = Panel(
            title="Sân Khấu 9:16 · 1080p",
            actions=[btn_snap, btn_reset_layout],
            padding=3,
        )

        self._stage_tabs = Tabs()
        # Tab 1: Interactive 9:16 Canvas
        self._canvas = InteractiveStageCanvas(self.settings.layout)
        self._stage_tabs.add_tab(self._canvas, "Bố Cục Kéo-Thả", icon="layout")

        # Tab 2: Video preview with controls available in full screen too
        self._video_player = StudioVideoPlayer()
        self._stage_tabs.add_tab(self._video_player, "Xem Thành Phẩm", icon="play")
        stage_panel.add(self._stage_tabs, 1)

        # Compact coordinate & style tabs below canvas (matching & upgrading Video 2)
        self._geom_tabs = Tabs()

        # Geometry Tab A: Comment Card
        card_tab = QWidget()
        c_col = QVBoxLayout(card_tab)
        c_col.setContentsMargins(4, 6, 4, 4)
        c_col.setSpacing(ThemeManager.spacing(2))

        c_row1 = Stack(direction="horizontal", spacing=2)
        self._spin_card_x = NumberInput(value=self.settings.layout.card_x_pct, minimum=5, maximum=95, step=1, decimals=1)
        self._spin_card_y = NumberInput(value=self.settings.layout.card_y_pct, minimum=5, maximum=95, step=1, decimals=1)
        self._spin_card_scale = NumberInput(
            value=self.settings.layout.card_scale_pct, minimum=50, maximum=122, step=2, decimals=1
        )
        c_row1.add(FormField("X %", self._spin_card_x), 1)
        c_row1.add(FormField("Y %", self._spin_card_y), 1)
        c_row1.add(FormField("Cỡ %", self._spin_card_scale), 1)
        c_col.addWidget(c_row1)

        c_row2 = Stack(direction="horizontal", spacing=2)
        self._spin_card_rad = NumberInput(value=self.settings.layout.card_radius_px, minimum=0, maximum=48, step=2)
        self._sel_card_theme = Select(
            options=[("Trắng (Chuẩn Threads)", "light"), ("Tối (Dark Card)", "dark")],
            value=self.settings.layout.card_theme,
        )
        c_row2.add(FormField("Bo góc (px)", self._spin_card_rad), 1)
        c_row2.add(FormField("Nền thẻ", self._sel_card_theme), 2)
        c_col.addWidget(c_row2)
        self._geom_tabs.add_tab(card_tab, "Thẻ bình luận")

        # Geometry Tab B: Sub-video
        sub_tab = QWidget()
        s_col = QVBoxLayout(sub_tab)
        s_col.setContentsMargins(4, 6, 4, 4)
        s_col.setSpacing(ThemeManager.spacing(2))

        s_row1 = Stack(direction="horizontal", spacing=2)
        self._spin_sub_x = NumberInput(value=self.settings.layout.sub_x_pct, minimum=5, maximum=95, step=1, decimals=1)
        self._spin_sub_y = NumberInput(value=self.settings.layout.sub_y_pct, minimum=5, maximum=95, step=1, decimals=1)
        self._spin_sub_w = NumberInput(value=self.settings.layout.sub_w_pct, minimum=25, maximum=100, step=2, decimals=1)
        self._spin_sub_h = NumberInput(value=self.settings.layout.sub_h_pct, minimum=12, maximum=65, step=2, decimals=1)
        s_row1.add(FormField("X %", self._spin_sub_x), 1)
        s_row1.add(FormField("Y %", self._spin_sub_y), 1)
        s_row1.add(FormField("Rộng %", self._spin_sub_w), 1)
        s_row1.add(FormField("Cao %", self._spin_sub_h), 1)
        s_col.addWidget(s_row1)

        s_row2 = Stack(direction="horizontal", spacing=2)
        self._spin_sub_rad = NumberInput(value=self.settings.layout.sub_radius_px, minimum=0, maximum=48, step=2)
        self._sel_sub_fit = Select(
            options=[("Lấp đầy khung (Cover)", "cover"), ("Vừa khít khung (Contain)", "contain")],
            value=self.settings.layout.sub_fit,
        )
        s_row2.add(FormField("Bo góc (px)", self._spin_sub_rad), 1)
        s_row2.add(FormField("Tỉ lệ khung", self._sel_sub_fit), 2)
        s_col.addWidget(s_row2)
        self._geom_tabs.add_tab(sub_tab, "Video phụ")

        stage_panel.add(self._geom_tabs)

        btn_snap.clicked.connect(self._export_stage_snapshot)
        btn_reset_layout.clicked.connect(self._reset_layout_defaults)
        return stage_panel

    def _build_log_panel(self) -> QWidget:
        btn_clear_log = Button("Xoá nhật ký", size="small", variant="ghost", icon="eraser")
        panel = Panel(title="Nhật Ký Xử Lý Đa Luồng", actions=[btn_clear_log], padding=2)
        self._log_viewer = LogViewer(max_entries=10000, timestamps=True)
        self._log_viewer.setMinimumHeight(110)
        self._log_handler = self._log_viewer.attach_logger(logger, level=logging.INFO)
        panel.add(self._log_viewer, 1)
        btn_clear_log.clicked.connect(self._log_viewer.clear)
        return panel

    # ------------------------------------------------------------------
    # Signal Wiring & Live Synchronization
    # ------------------------------------------------------------------

    def _wire_signals(self) -> None:
        self._btn_collapse_nav.clicked.connect(
            lambda: self._sidebar.set_collapsed(not self._sidebar.is_collapsed())
        )
        self._sidebar.current_changed.connect(self._on_nav_changed)
        self._switch_dark.toggled.connect(self._on_theme_toggled)
        self._btn_apply_preset.clicked.connect(self._apply_selected_preset)
        self._btn_open_outputs.clicked.connect(self._open_output_root)

        # Queue & Batch actions
        self._btn_enqueue.clicked.connect(self._on_enqueue_clicked)
        self._btn_approve_all.clicked.connect(self._on_approve_all_clicked)
        self._btn_inspect_selected.clicked.connect(self._open_selected_job_in_inspector)
        self._btn_watch_selected.clicked.connect(self._watch_selected_job_video)
        self._btn_stop_all.clicked.connect(self._on_stop_all_clicked)
        self._queue_table.row_activated.connect(self._on_queue_row_activated)

        # Pipeline signals
        self.pipeline.job_changed.connect(self._on_pipeline_job_changed)
        self.pipeline.review_ready.connect(self._on_pipeline_review_ready)
        self.pipeline.job_finished.connect(self._on_pipeline_job_finished)
        self.pipeline.counts_changed.connect(self._update_badges_and_progress)

        # Inspector signals
        self._current_seg_idx = 0
        self._inspect_job_select.value_changed.connect(self._load_job_into_inspector)
        self._seg_table.selection_changed.connect(self._on_inspector_table_selected)
        self._chk_seg_enabled.toggled.connect(self._on_segment_fields_edited)
        self._seg_voice_select.value_changed.connect(self._on_segment_fields_edited)
        self._seg_spoken_area.text_changed.connect(self._on_segment_fields_edited)
        self._seg_raw_area.text_changed.connect(self._on_segment_fields_edited)
        self._btn_seg_up.clicked.connect(lambda: self._move_current_segment(-1))
        self._btn_seg_down.clicked.connect(lambda: self._move_current_segment(1))
        self._btn_prev_seg.clicked.connect(lambda: self._step_segment_selection(-1))
        self._btn_next_seg.clicked.connect(lambda: self._step_segment_selection(1))
        self._btn_re_normalize.clicked.connect(self._renormalize_current_job)
        self._btn_approve_current.clicked.connect(self._approve_current_inspected_job)
        self._btn_preview_seg_voice.clicked.connect(self._preview_current_segment_audio)

        # Voice studio signals
        self._btn_audition_play.clicked.connect(self._play_voice_audition)
        self._bridge.voice_preview_ready.connect(self._on_voice_preview_ready)
        self._bridge.voice_preview_failed.connect(self._on_voice_preview_failed)
        self._bridge.login_finished.connect(lambda msg: Toast.show(msg, parent=self, variant="success"))
        self._btn_export_audio_only.clicked.connect(self._audio_primary_action)
        self._btn_save_audio_only.clicked.connect(self._save_audio_only)
        self._btn_normalize_audio_text.clicked.connect(self._normalize_audio_export_text)
        self._audio_player.playbackStateChanged.connect(self._on_audio_preview_playback_state_changed)
        self._audio_player.positionChanged.connect(self._on_audio_preview_position_changed)
        self._audio_player.durationChanged.connect(self._on_audio_preview_duration_changed)
        self._audio_seek_slider.sliderReleased.connect(self._seek_audio_preview)
        self._audio_seek_slider.sliderPressed.connect(self._seek_audio_preview)
        self._bridge.audio_export_ready.connect(self._on_audio_export_ready)
        self._bridge.audio_export_failed.connect(self._on_audio_export_failed)
        self._bridge.audio_save_ready.connect(self._on_audio_save_ready)
        self._bridge.audio_save_failed.connect(self._on_audio_save_failed)
        self._audio_export_text.text_changed.connect(self._invalidate_audio_preview)
        self._audio_export_voice.value_changed.connect(self._invalidate_audio_preview)
        self._audio_export_speed.value_changed.connect(self._invalidate_audio_preview)
        self._audio_export_text.text_changed.connect(self._on_audio_segment_edited)
        self._audio_export_voice.value_changed.connect(self._on_audio_segment_edited)
        self._audio_segments_table.selection_changed.connect(self._on_audio_segment_selected)
        self._btn_audio_add_segment.clicked.connect(self._add_audio_segment)
        self._btn_audio_delete_segment.clicked.connect(self._delete_audio_segment)
        self._btn_audio_segment_up.clicked.connect(lambda: self._move_audio_segment(-1))
        self._btn_audio_segment_down.clicked.connect(lambda: self._move_audio_segment(1))

        # Layout & Canvas live sync
        self._canvas.layout_edited.connect(self._sync_layout_spinners_from_cfg)
        self._canvas.layer_selected.connect(
            lambda layer: self._geom_tabs.setCurrentIndex(0 if layer == "card" else 1)
        )
        self._chk_hide_name.toggled.connect(self._on_layout_controls_changed)
        self._chk_sub_video.toggled.connect(self._on_layout_controls_changed)
        for sp in (
            self._spin_card_x,
            self._spin_card_y,
            self._spin_card_scale,
            self._spin_card_rad,
            self._spin_sub_x,
            self._spin_sub_y,
            self._spin_sub_w,
            self._spin_sub_h,
            self._spin_sub_rad,
        ):
            sp.value_changed.connect(self._on_layout_controls_changed)
        self._sel_card_theme.value_changed.connect(self._on_layout_controls_changed)
        self._sel_sub_fit.value_changed.connect(self._on_layout_controls_changed)

        # Audio sliders <-> spinners sync
        self._slider_bgm.valueChanged.connect(lambda v: self._spin_bgm.set_value(float(v)))
        self._spin_bgm.value_changed.connect(lambda v: self._slider_bgm.setValue(int(v)))
        self._slider_sfx.valueChanged.connect(lambda v: self._spin_sfx.set_value(float(v)))
        self._spin_sfx.value_changed.connect(lambda v: self._slider_sfx.setValue(int(v)))

        # Settings actions
        self._btn_login_threads.clicked.connect(self._start_threads_login)
        self._btn_save_settings.clicked.connect(self._persist_all_settings)

    def _install_command_palette(self) -> None:
        commands = [
            Command("demo", "Nạp bài mẫu Video 1 (“nức nở”, “thương”…)", icon="zap", action=self._load_demo_script),
            Command("enqueue", "Chạy tạo video đa luồng ngay", icon="play", action=self._on_enqueue_clicked),
            Command("approve_all", "Duyệt tất cả bài đang chờ", icon="check-circle", action=self._on_approve_all_clicked),
            Command("open_out", "Mở thư mục Kết quả xuất MP4", icon="folder-open", action=self._open_output_root),
            Command("theme", "Chuyển đổi giao diện Sáng / Tối", icon="moon", action=ThemeManager.toggle),
            Command("save", "Lưu toàn bộ cài đặt hiện tại", icon="save", action=self._persist_all_settings),
        ]
        self._palette = CommandPalette(
            parent=self,
            commands=commands,
            shortcut="Ctrl+K",
            placeholder="Gõ lệnh nhanh (VD: nạp mẫu, duyệt tất cả, mở thư mục…)",
        )
        self._btn_cmd_palette.clicked.connect(self._palette.open_palette)

    # ------------------------------------------------------------------
    # Actions & Event Handlers
    # ------------------------------------------------------------------

    def _on_nav_changed(self, key: str) -> None:
        if key in self._page_keys:
            self._pages.setCurrentIndex(self._page_keys.index(key))
        if hasattr(self, "_stage_scroll"):
            audio_only = key == "audio_export"
            self._stage_scroll.setVisible(not audio_only)
            if audio_only:
                self._pages.setMaximumHeight(16777215)
                self._center_stage_splitter.setSizes([max(700, self._center_stage_splitter.width()), 0])
            else:
                self._responsive_mode = ""
                self._apply_responsive_layout(self.width(), self.height())

    def _on_theme_toggled(self, is_dark: bool) -> None:
        target = "dark" if is_dark else "light"
        ThemeManager.set_theme(target)
        self.settings.theme = target

    def _collect_live_settings(self) -> None:
        self.settings.comment_mode = str(self._cmt_mode_select.value() or CommentMode.TOP_LIKES_WITH_AUTHOR.value)
        self.settings.max_comments = int(self._cmt_count_spin.value())
        self.settings.voice_speed = float(self._speed_quick_spin.value())
        self.settings.require_review_default = self._chk_require_review.isChecked()
        self.settings.voice_assign_mode = str(self._voice_assign_select.value() or VoiceAssignMode.RANDOM.value)
        chosen_voices = [str(v) for v in self._voice_pool_multiselect.values()]
        if chosen_voices:
            self.settings.selected_voices = chosen_voices

        self.settings.media.bg_video_file = ""
        self.settings.media.sub_video_dir = str(self._managed_media_library("video phụ"))
        self.settings.media.bgm_file = ""
        self.settings.media.sfx_dir = self._picker_sfx.path()
        self.settings.media.bgm_enabled = self._chk_bgm.isChecked()
        self.settings.media.bgm_volume_pct = int(self._spin_bgm.value())
        self.settings.media.sfx_enabled = self._chk_sfx.isChecked()
        self.settings.media.sfx_volume_pct = int(self._spin_sfx.value())

        self.settings.gemini_api_key = self._gemini_key_input.text().strip()
        self.settings.capcut_device_json = self._capcut_dev_picker.path()
        self.settings.vieneu_clone_ref_audio = self._clone_audio_picker.path()
        self.settings.vieneu_clone_ref_text = self._clone_ref_text.text().strip()
        self.settings.vieneu_endpoint = self._vieneu_endpoint_input.text().strip()
        self.settings.max_scrape_workers = int(self._spin_w_scrape.value())
        self.settings.max_ai_workers = int(self._spin_w_ai.value())
        self.settings.max_tts_workers = int(self._spin_w_tts.value())
        self.settings.max_render_workers = int(self._spin_w_render.value())
        self.settings.prefer_nvenc = self._switch_nvenc.isChecked()
        if self._picker_out_root.path():
            self.settings.output_root = self._picker_out_root.path()

    def _persist_all_settings(self) -> None:
        self._collect_live_settings()
        self.pipeline._rebuild_semaphores()
        self.settings.save()
        has_gpu = nvenc_available() and self.settings.prefer_nvenc
        self._encoder_badge.setText("NVENC 1080p" if has_gpu else "CPU x264 1080p")
        self._encoder_badge.set_variant("success" if has_gpu else "info")
        Toast.show("Đã lưu toàn bộ cấu hình Threads Studio Pro.", parent=self, variant="success")

    def _managed_media_library(self, label: str) -> Path:
        sample_media = project_root() / "assets" / "sample_media"
        folder = {
            "video nền": "bg_grid",
            "video phụ": "sub_videos",
            "nhạc nền": "bgm",
        }.get(label, "bgm")
        return sample_media / folder

    def _refresh_media_library_status(self) -> None:
        configured_video_dir = Path(self.settings.media.bg_video_dir) if self.settings.media.bg_video_dir else None
        configured_sub_dir = Path(self.settings.media.sub_video_dir) if self.settings.media.sub_video_dir else None
        configured_music_dir = Path(self.settings.media.bgm_dir) if self.settings.media.bgm_dir else None
        video_dir = (
            configured_video_dir
            if configured_video_dir and configured_video_dir.is_dir()
            else self._managed_media_library("video nền")
        )
        music_dir = (
            configured_music_dir
            if configured_music_dir and configured_music_dir.is_dir()
            else self._managed_media_library("nhạc nền")
        )
        sub_dir = (
            configured_sub_dir
            if configured_sub_dir and configured_sub_dir.is_dir()
            else self._managed_media_library("video phụ")
        )
        video_files = sorted(
            [p for p in video_dir.glob("*") if p.is_file()],
            key=lambda path: path.name.lower(),
        ) if video_dir.exists() else []
        sub_files = sorted(
            [p for p in sub_dir.glob("*") if p.is_file()], key=lambda path: path.name.lower()
        ) if sub_dir.exists() else []
        music_files = sorted(
            [p for p in music_dir.glob("*") if p.is_file()], key=lambda path: path.name.lower()
        ) if music_dir.exists() else []
        video_count = len(video_files)
        music_count = len(music_files)
        self._bg_library_status.setText(f"Đã thêm {video_count} video · Ứng dụng tự chọn ngẫu nhiên khi xuất")
        self._sub_library_status.setText(f"Đã thêm {len(sub_files)} video phụ · Tự cắt ghép ngẫu nhiên khi bật")
        self._bgm_library_status.setText(f"Đã thêm {music_count} bài nhạc · Ứng dụng tự chọn ngẫu nhiên khi xuất")
        self._bg_library_table.set_rows(self._media_library_rows(video_files))
        self._sub_library_table.set_rows(self._media_library_rows(sub_files))
        self._bgm_library_table.set_rows(self._media_library_rows(music_files))

    def _media_library_rows(self, paths: list[Path]) -> list[dict]:
        return [
            {
                "checked": "☐",
                "name": path.name,
                "size": self._format_file_size(path.stat().st_size),
                "path": str(path),
            }
            for path in paths
        ]

    def _enable_checkbox_selection(self, table: DataTable) -> None:
        table.view.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        table.selection_changed.connect(lambda rows, current=table: self._sync_checkmarks(current, rows))

    @staticmethod
    def _sync_checkmarks(table: DataTable, selected_rows: list[dict]) -> None:
        selected_paths = {str(row.get("path", "")) for row in selected_rows}
        rows = table.rows()
        for row in rows:
            row["checked"] = "☑" if str(row.get("path", "")) in selected_paths else "☐"
        if rows:
            table.model.dataChanged.emit(
                table.model.index(0, 0),
                table.model.index(table.model.rowCount() - 1, 0),
            )

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} GB"

    def _preview_selected_background_video(self) -> None:
        self._preview_selected_library_video(self._bg_library_table, "video nền")

    def _preview_selected_library_video(self, table: DataTable, label: str) -> None:
        selected = table.selected_rows()
        if not selected:
            Toast.show(f"Hãy chọn một {label} trong danh sách.", parent=self, variant="info")
            return
        self._preview_background_video_row(selected[0])

    def _preview_background_video_row(self, row: dict) -> None:
        video_path = Path(str(row.get("path", "")))
        if not video_path.is_file():
            Toast.show("Video này không còn tồn tại trong kho.", parent=self, variant="warning")
            self._refresh_media_library_status()
            return
        self._video_player.set_source(str(video_path))
        self._stage_tabs.setCurrentIndex(1)
        self._video_player.play()

    def _preview_selected_background_music(self) -> None:
        selected = self._bgm_library_table.selected_rows()
        if not selected:
            Toast.show("Hãy chọn một bài nhạc trong danh sách.", parent=self, variant="info")
            return
        self._preview_background_music_row(selected[0])

    def _preview_background_music_row(self, row: dict) -> None:
        audio_path = Path(str(row.get("path", "")))
        if not audio_path.is_file():
            Toast.show("Bài nhạc này không còn tồn tại trong kho.", parent=self, variant="warning")
            self._refresh_media_library_status()
            return
        self._audio_player.setSource(QUrl.fromLocalFile(str(audio_path)))
        self._audio_player.play()
        Toast.show(f"Đang phát thử: {audio_path.name}", parent=self, variant="info")

    def _delete_selected_media(self, table: DataTable, label: str) -> None:
        selected = table.selected_rows()
        if not selected:
            Toast.show(f"Hãy tích chọn {label} cần xóa.", parent=self, variant="info")
            return
        library = self._managed_media_library(label)
        media_paths = [Path(str(row.get("path", ""))) for row in selected]
        try:
            if any(not path.is_file() or path.resolve().parent != library.resolve() for path in media_paths):
                Toast.show("Không thể xóa file nằm ngoài kho của ứng dụng.", parent=self, variant="warning")
                return
        except OSError:
            Toast.show("Không tìm thấy file cần xóa.", parent=self, variant="warning")
            self._refresh_media_library_status()
            return

        item_word = "file" if len(media_paths) > 1 else media_paths[0].suffix.lstrip(".").upper()
        dialog = ConfirmDialog(
            self,
            title=f"Xóa {label} đã chọn?",
            message=(
                f"Bạn sắp xóa vĩnh viễn {len(media_paths)} {item_word} khỏi kho ứng dụng.\n"
                "Thao tác này không thể hoàn tác."
            ),
            confirm_text="Xóa vĩnh viễn",
            cancel_text="Hủy",
            danger=True,
            width=480,
        )
        dialog.exec()
        if not dialog.was_confirmed():
            return
        try:
            self._audio_player.stop()
            self._video_player.pause()
            for media_path in media_paths:
                media_path.unlink()
            self._refresh_media_library_status()
            Toast.show(f"Đã xóa {len(media_paths)} file.", parent=self, variant="success")
        except OSError as exc:
            Toast.show(f"Không thể xóa file: {exc}", parent=self, variant="danger")

    def _import_media_into_library(self, paths: list[str], label: str) -> None:
        if not paths:
            return
        destination_dir = self._managed_media_library(label)
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            imported = 0
            skipped = 0
            for raw_path in paths:
                source = Path(raw_path)
                if not source.is_file():
                    skipped += 1
                    continue
                destination = destination_dir / source.name
                if source.resolve() == destination.resolve():
                    skipped += 1
                    continue
                if destination.exists():
                    if destination.stat().st_size == source.stat().st_size:
                        skipped += 1
                        continue
                    counter = 2
                    while destination.exists():
                        destination = destination_dir / f"{source.stem}-{counter}{source.suffix}"
                        counter += 1
                shutil.copy2(source, destination)
                imported += 1
            self.settings.media.bg_video_file = ""
            self.settings.media.bgm_file = ""
            if label == "video nền":
                self.settings.media.bg_video_dir = str(destination_dir)
            elif label == "video phụ":
                self.settings.media.sub_video_dir = str(destination_dir)
            else:
                self.settings.media.bgm_dir = str(destination_dir)
            self._collect_live_settings()
            self.settings.save()
            self._refresh_media_library_status()
            message = f"Đã thêm {imported} file {label} vào ứng dụng."
            if skipped:
                message += f" Bỏ qua {skipped} file đã có."
            Toast.show(message, parent=self, variant="success" if imported else "info")
        except OSError as exc:
            Toast.show(f"Không thể Import {label}: {exc}", parent=self, variant="danger")
        finally:
            if label == "video nền":
                self._picker_bg_file.set_paths([])
            elif label == "video phụ":
                self._picker_sub_file.set_paths([])
            else:
                self._picker_bgm_file.set_paths([])

    def _load_demo_script(self) -> None:
        self._input_mode_seg.set_value("manual")
        self._batch_input_area.set_text(sample_video1_script())
        self._sidebar.set_current("queue")
        Toast.show("Đã nạp kịch bản mẫu từ Video 1 — bấm Tạo Video Đa Luồng để chạy!", parent=self, variant="info")

    def _paste_links_from_clipboard(self) -> None:
        cb = QApplication.clipboard().text().strip()
        if not cb:
            Toast.show("Clipboard hiện đang trống.", parent=self, variant="warning")
            return
        cur = self._batch_input_area.text().strip()
        merged = f"{cur}\n{cb}".strip() if cur else cb
        self._batch_input_area.set_text(merged)

    def _on_enqueue_clicked(self) -> None:
        self._collect_live_settings()
        self.settings.save()
        raw = self._batch_input_area.text().strip()
        if not raw:
            Toast.show("Hãy dán ít nhất 1 link Threads hoặc bấm 'Nạp Bài Mẫu Video 1'.", parent=self, variant="warning")
            return

        mode = self._input_mode_seg.value()
        new_jobs: list[VideoJob] = []

        if mode == "manual":
            # Support batching multiple manual scripts separated by `===`
            scripts = [s.strip() for s in raw.split("===") if s.strip()]
            for sc in scripts:
                new_jobs.append(self._snapshot_new_job(manual_script=sc))
        else:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            urls = [ln for ln in lines if ln.startswith("http")]
            if not urls:
                # User pasted plain story text while in URL mode — handle gracefully!
                new_jobs.append(self._snapshot_new_job(manual_script=raw))
            else:
                for u in urls:
                    new_jobs.append(self._snapshot_new_job(source_url=u))

        self.pipeline.enqueue_jobs(new_jobs)
        self._refresh_queue_table()
        Toast.show(f"Đã đẩy {len(new_jobs)} tác vụ vào bể xử lý đa luồng!", parent=self, variant="info")

    def _snapshot_new_job(self, *, source_url: str = "", manual_script: str = "") -> VideoJob:
        return VideoJob(
            source_url=source_url,
            manual_script=manual_script,
            require_review=self.settings.require_review_default,
            comment_mode=self.settings.comment_mode,
            max_comments=self.settings.max_comments,
            voice_speed=self.settings.voice_speed,
            voice_assign_mode=self.settings.voice_assign_mode,
            selected_voices=list(self.settings.selected_voices),
            layout=copy.deepcopy(self.settings.layout),
            media=copy.deepcopy(self.settings.media),
        )

    # ------------------------------------------------------------------
    # Pipeline Callbacks & Queue Table Updates
    # ------------------------------------------------------------------

    def _on_pipeline_job_changed(self, job_id: str) -> None:
        self._refresh_queue_table()
        self._refresh_inspector_job_dropdown()
        if self._current_inspect_job_id == job_id:
            job = self.pipeline.jobs.get(job_id)
            if job and job.stage == JobStage.AWAITING_REVIEW.value and not self._seg_table.rows():
                self._populate_inspector_segments(job)

    def _on_pipeline_review_ready(self, job_id: str) -> None:
        self._refresh_inspector_job_dropdown()
        # Automatically load the first ready job into the inspector if none open yet
        if self._current_inspect_job_id is None:
            self._inspect_job_select.set_value(job_id)
            self._load_job_into_inspector(job_id)
            if len(self.pipeline.job_order) == 1:
                self._sidebar.set_current("inspector")
        Toast.show(f"Job #{job_id} đã chuẩn hóa xong — sẵn sàng duyệt kịch bản!", parent=self, variant="info")

    def _on_pipeline_job_finished(self, job_id: str, mp4_path: str) -> None:
        Toast.show(f"Xuất xong MP4 cho Job #{job_id}!", parent=self, variant="success")
        if Path(mp4_path).exists():
            self._video_player.set_source(mp4_path)
            self._stage_tabs.setCurrentIndex(1)
        self.pipeline.forget_completed_job(job_id)
        if self.pipeline.active_count() == 0 and self.pipeline.review_count() == 0:
            self._log_viewer.clear()

    def _update_badges_and_progress(self) -> None:
        active = self.pipeline.active_count()
        review = self.pipeline.review_count()
        self._nav_queue.set_badge(active if active > 0 else None)
        self._nav_inspector.set_badge(review if review > 0 else None)

        if not self.pipeline.jobs:
            self._batch_progress.set_value(0)
            return
        avg = sum(j.progress_pct for j in self.pipeline.jobs.values()) / len(self.pipeline.jobs)
        self._batch_progress.set_value(avg)

    def _refresh_queue_table(self) -> None:
        rows = []
        for jid in reversed(self.pipeline.job_order):
            j = self.pipeline.jobs[jid]
            rows.append(
                {
                    "job_id": j.job_id,
                    "slug": j.title_slug,
                    "segments": len(j.enabled_segments) if j.segments else j.max_comments + 1,
                    "stage_label": STAGE_LABELS_VI.get(j.stage, j.stage),
                    "progress": j.progress_pct,
                    "detail": j.status_detail,
                }
            )
        self._queue_table.set_rows(rows)

    def _selected_queue_job_id(self) -> str | None:
        sel = self._queue_table.selected_rows()
        if sel:
            return str(sel[0]["job_id"])
        if self.pipeline.job_order:
            return self.pipeline.job_order[-1]
        return None

    def _on_queue_row_activated(self, row: dict) -> None:
        jid = str(row.get("job_id", ""))
        job = self.pipeline.jobs.get(jid)
        if not job:
            return
        if job.stage == JobStage.COMPLETED.value and job.output_mp4 and Path(job.output_mp4).exists():
            self._video_player.set_source(job.output_mp4)
            self._stage_tabs.setCurrentIndex(1)
            self._video_player.play()
        elif job.segments:
            self._open_job_in_inspector(jid)

    def _open_selected_job_in_inspector(self) -> None:
        jid = self._selected_queue_job_id()
        if jid:
            self._open_job_in_inspector(jid)

    def _open_job_in_inspector(self, job_id: str) -> None:
        self._refresh_inspector_job_dropdown()
        job = self.pipeline.jobs.get(job_id)
        if not job or not job.segments:
            Toast.show("Job này đang cào dữ liệu, chờ vài giây để tải kịch bản.", parent=self, variant="info")
            return
        self._inspect_job_select.set_value(job_id)
        self._load_job_into_inspector(job_id)
        self._sidebar.set_current("inspector")

    def _watch_selected_job_video(self) -> None:
        jid = self._selected_queue_job_id()
        job = self.pipeline.jobs.get(jid or "")
        if job and job.output_mp4 and Path(job.output_mp4).exists():
            self._video_player.set_source(job.output_mp4)
            self._stage_tabs.setCurrentIndex(1)
            self._video_player.play()
        else:
            Toast.show("Job này chưa xuất file MP4.", parent=self, variant="warning")

    def _on_approve_all_clicked(self) -> None:
        count = self.pipeline.approve_all_waiting()
        if count:
            Toast.show(f"Đã duyệt & đẩy {count} video vào lò xuất MP4!", parent=self, variant="success")
        else:
            Toast.show("Không có bài nào đang ở trạng thái Chờ duyệt.", parent=self, variant="info")

    def _on_stop_all_clicked(self) -> None:
        stoppable = [
            job for job in self.pipeline.jobs.values()
            if job.stage not in (
                JobStage.COMPLETED.value,
                JobStage.FAILED.value,
                JobStage.CANCELLED.value,
            )
        ]
        if not stoppable:
            Toast.show("Tác vụ đã hoàn tất, hiện không còn tiến trình nào để dừng.", parent=self, variant="info")
            return
        self.pipeline.cancel_all_active()
        Toast.show(f"Đã dừng {len(stoppable)} tác vụ đang chạy.", parent=self, variant="warning")

    # ------------------------------------------------------------------
    # Inspector Editing Logic
    # ------------------------------------------------------------------

    def _refresh_inspector_job_dropdown(self) -> None:
        opts = [
            (f"#{jid} · {self.pipeline.jobs[jid].title_slug} ({STAGE_LABELS_VI.get(self.pipeline.jobs[jid].stage, '')})", jid)
            for jid in reversed(self.pipeline.job_order)
            if self.pipeline.jobs[jid].segments
        ]
        self._inspect_job_select.set_options(opts)
        if self._current_inspect_job_id and any(v == self._current_inspect_job_id for _, v in opts):
            self._inspect_job_select.set_value(self._current_inspect_job_id)

    def _load_job_into_inspector(self, job_id: object) -> None:
        jid = str(job_id or "")
        job = self.pipeline.jobs.get(jid)
        if not job:
            return
        self._current_inspect_job_id = jid
        self._populate_inspector_segments(job)

    def _populate_inspector_segments(self, job: VideoJob, select_idx: int = 0) -> None:
        rows = []
        for idx, seg in enumerate(job.segments):
            mark = "✓" if seg.enabled else "✕"
            idx_lbl = f"{mark} Gốc" if seg.is_root_post else f"{mark} #{idx}"
            meta = f"@{seg.username}" if seg.is_root_post else f"♥ {seg.likes}"
            rows.append(
                {
                    "row_idx": idx,
                    "idx_label": idx_lbl,
                    "meta": meta,
                    "snippet": seg.raw_text.replace("\n", " ")[:48],
                }
            )
        self._seg_table.set_rows(rows)
        if rows:
            clamped = max(0, min(len(rows) - 1, select_idx))
            self._seg_table.select_rows([clamped])
            self._on_inspector_row_selected(clamped)

    def _on_inspector_table_selected(self, selected_rows: list[dict]) -> None:
        if not selected_rows:
            return
        self._on_inspector_row_selected(int(selected_rows[0]["row_idx"]))

    def _on_inspector_row_selected(self, row_idx: int) -> None:
        job = self.pipeline.jobs.get(self._current_inspect_job_id or "")
        if not job or not (0 <= row_idx < len(job.segments)):
            return
        self._current_seg_idx = row_idx
        seg = job.segments[row_idx]
        self._syncing_segment_editor = True
        try:
            self._chk_seg_enabled.setChecked(seg.enabled)
            if seg.voice_id in self._voice_label_by_id:
                self._seg_voice_select.set_value(seg.voice_id)
            self._seg_spoken_area.set_text(seg.spoken_text)
            self._seg_raw_area.set_text(seg.raw_text)
        finally:
            self._syncing_segment_editor = False

        self._stage_tabs.setCurrentIndex(0)
        self._canvas.set_preview_segment(seg)

    def _on_segment_fields_edited(self, *_args) -> None:
        if self._syncing_segment_editor:
            return
        job = self.pipeline.jobs.get(self._current_inspect_job_id or "")
        row = self._current_seg_idx
        if not job or not (0 <= row < len(job.segments)):
            return
        seg = job.segments[row]
        seg.enabled = self._chk_seg_enabled.isChecked()
        if self._seg_voice_select.value():
            seg.voice_id = str(self._seg_voice_select.value())
        seg.spoken_text = self._seg_spoken_area.text()
        seg.raw_text = self._seg_raw_area.text()
        self._canvas.set_preview_segment(seg)

    def _move_current_segment(self, delta: int) -> None:
        job = self.pipeline.jobs.get(self._current_inspect_job_id or "")
        row = self._current_seg_idx
        if not job or (row <= 0 and delta < 0):
            return
        if row == 0:
            Toast.show("Bài viết chính luôn nằm ở đầu video.", parent=self, variant="info")
            return
        target = row + delta
        if target < 1 or target >= len(job.segments):
            return
        job.segments[row], job.segments[target] = job.segments[target], job.segments[row]
        self._populate_inspector_segments(job, select_idx=target)

    def _step_segment_selection(self, delta: int) -> None:
        job = self.pipeline.jobs.get(self._current_inspect_job_id or "")
        if not job or not job.segments:
            return
        nxt = max(0, min(len(job.segments) - 1, self._current_seg_idx + delta))
        self._seg_table.select_rows([nxt])
        self._on_inspector_row_selected(nxt)

    def _renormalize_current_job(self) -> None:
        from ..ai.text_normalizer import normalize_segments_batch

        job = self.pipeline.jobs.get(self._current_inspect_job_id or "")
        if not job:
            return
        normalize_segments_batch(
            job.segments,
            gemini_api_key=self._gemini_key_input.text().strip(),
            gemini_model=self.settings.gemini_model,
            include_emotion_cues=any(v.startswith("vieneu:") for v in job.selected_voices),
        )
        self._on_inspector_row_selected(self._current_seg_idx)
        Toast.show("Đã chuẩn hóa lại từ viết tắt cho toàn bộ các đoạn.", parent=self, variant="success")

    def _approve_current_inspected_job(self) -> None:
        jid = self._current_inspect_job_id
        if not jid or jid not in self.pipeline.jobs:
            return
        job = self.pipeline.jobs[jid]
        job.layout = copy.deepcopy(self.settings.layout)
        self.pipeline.approve_and_render(jid)
        self._sidebar.set_current("queue")
        Toast.show(f"Job #{jid} đã chuyển sang tạo giọng đọc & ghép MP4!", parent=self, variant="success")

    # ------------------------------------------------------------------
    # Voice Audition & Preview
    # ------------------------------------------------------------------

    def _play_voice_audition(self) -> None:
        vid = str(self._audition_voice_select.value() or "edge:vi-VN-HoaiMyNeural")
        txt = self._audition_input.text().strip() or "Xin chào từ Threads Studio Pro."
        self.settings.preview_voice_id = vid
        self._synth_and_play_preview(txt, vid, self._btn_audition_play)

    def _preview_current_segment_audio(self) -> None:
        txt = self._seg_spoken_area.text().strip() or self._seg_raw_area.text().strip()
        vid = str(self._seg_voice_select.value() or "edge:vi-VN-HoaiMyNeural")
        if txt:
            self._synth_and_play_preview(txt, vid, self._btn_preview_seg_voice)

    def _synth_and_play_preview(self, text: str, voice_id: str, btn: Button) -> None:
        self._collect_live_settings()
        btn.set_loading(True)
        preview_wav = self.settings.user_data_dir() / "preview_voice.wav"

        def _worker() -> None:
            try:
                router = self.pipeline._make_tts_router()
                router.synthesize_single(text, voice_id, preview_wav, speed=self.settings.voice_speed)
                self._bridge.voice_preview_ready.emit(str(preview_wav))
            except Exception as exc:
                self._bridge.voice_preview_failed.emit(str(exc))

        self._ui_pool.submit(_worker)

    def _on_voice_preview_ready(self, wav_path: str) -> None:
        self._btn_audition_play.set_loading(False)
        self._btn_preview_seg_voice.set_loading(False)
        self._audio_player.stop()
        self._audio_player.setSource(QUrl.fromLocalFile(wav_path))
        self._audio_player.play()

    def _on_voice_preview_failed(self, err: str) -> None:
        self._btn_audition_play.set_loading(False)
        self._btn_preview_seg_voice.set_loading(False)
        Toast.show(f"Không phát được giọng thử: {err}", parent=self, variant="error")

    def _refresh_audio_segments_table(self, *, select_index: int | None = None) -> None:
        rows = []
        for index, segment in enumerate(self._audio_segments):
            text = str(segment.get("text", "")).strip()
            rows.append(
                {
                    "number": index + 1,
                    "snippet": text[:90] if text else "(Câu mới chưa có nội dung)",
                    "voice_label": segment.get("voice_label", "Giọng đọc"),
                    "source_index": index,
                }
            )
        self._audio_segments_table.set_rows(rows)
        if rows:
            target = min(select_index if select_index is not None else self._current_audio_segment, len(rows) - 1)
            self._audio_segments_table.select_rows([target])
            self._load_audio_segment(target)

    def _load_audio_segment(self, index: int) -> None:
        if not (0 <= index < len(self._audio_segments)):
            return
        self._current_audio_segment = index
        segment = self._audio_segments[index]
        self._syncing_audio_segment = True
        try:
            self._audio_export_text.set_text(str(segment.get("text", "")))
            self._audio_export_voice.set_value(str(segment.get("voice_id", "")))
        finally:
            self._syncing_audio_segment = False

    def _on_audio_segment_selected(self, rows: list[dict]) -> None:
        if rows:
            self._load_audio_segment(int(rows[0].get("source_index", 0)))

    def _on_audio_segment_edited(self, *_args) -> None:
        if self._syncing_audio_segment or not self._audio_segments:
            return
        index = min(self._current_audio_segment, len(self._audio_segments) - 1)
        voice_id = str(self._audio_export_voice.value() or "edge:vi-VN-HoaiMyNeural")
        self._audio_segments[index] = {
            "text": self._audio_export_text.text(),
            "voice_id": voice_id,
            "voice_label": self._voice_label_by_id.get(voice_id, voice_id),
        }
        rows = self._audio_segments_table.rows()
        if index < len(rows):
            text = self._audio_segments[index]["text"].strip()
            rows[index]["snippet"] = text[:90] if text else "(Câu mới chưa có nội dung)"
            rows[index]["voice_label"] = self._audio_segments[index]["voice_label"]
            self._audio_segments_table.model.dataChanged.emit(
                self._audio_segments_table.model.index(index, 0),
                self._audio_segments_table.model.index(index, 2),
            )

    def _add_audio_segment(self) -> None:
        current_voice = str(self._audio_export_voice.value() or "edge:vi-VN-HoaiMyNeural")
        self._audio_segments.append(
            {"text": "", "voice_id": current_voice, "voice_label": self._voice_label_by_id.get(current_voice, current_voice)}
        )
        self._invalidate_audio_preview()
        self._refresh_audio_segments_table(select_index=len(self._audio_segments) - 1)

    def _delete_audio_segment(self) -> None:
        if len(self._audio_segments) <= 1:
            self._audio_segments[0]["text"] = ""
            self._refresh_audio_segments_table(select_index=0)
            return
        index = min(self._current_audio_segment, len(self._audio_segments) - 1)
        self._audio_segments.pop(index)
        self._invalidate_audio_preview()
        self._refresh_audio_segments_table(select_index=max(0, index - 1))

    def _move_audio_segment(self, direction: int) -> None:
        index = self._current_audio_segment
        target = index + direction
        if not (0 <= index < len(self._audio_segments)) or not (0 <= target < len(self._audio_segments)):
            return
        self._audio_segments[index], self._audio_segments[target] = self._audio_segments[target], self._audio_segments[index]
        self._invalidate_audio_preview()
        self._refresh_audio_segments_table(select_index=target)

    def _audio_primary_action(self) -> None:
        preview = Path(self._audio_preview_path) if self._audio_preview_path else None
        if preview and preview.is_file() and self._audio_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._audio_player.pause()
            self._btn_export_audio_only.setText("Nghe Thử")
        elif preview and preview.is_file():
            current_source = self._audio_player.source().toLocalFile()
            same_source = False
            if current_source:
                try:
                    same_source = Path(current_source).resolve() == preview.resolve()
                except OSError:
                    same_source = os.path.normcase(current_source) == os.path.normcase(str(preview))
            if not same_source:
                self._audio_player.setSource(QUrl.fromLocalFile(str(preview)))
            if self._audio_player.duration() > 0 and self._audio_player.position() >= self._audio_player.duration() - 100:
                self._audio_player.setPosition(0)
            self._audio_player.play()
            self._btn_export_audio_only.setText("Dừng Nghe")
        else:
            self._export_audio_only()

    def _normalize_audio_export_text(self) -> None:
        from ..ai.text_normalizer import normalize_teencode_offline

        self._on_audio_segment_edited()
        if not any(str(segment.get("text", "")).strip() for segment in self._audio_segments):
            Toast.show("Hãy nhập nội dung trước khi bung từ viết tắt.", parent=self, variant="warning")
            return
        for segment in self._audio_segments:
            raw_text = str(segment.get("text", "")).strip()
            if raw_text:
                segment["text"] = normalize_teencode_offline(raw_text)
        self._invalidate_audio_preview()
        self._refresh_audio_segments_table(select_index=self._current_audio_segment)
        Toast.show("Đã bung từ viết tắt cho toàn bộ câu.", parent=self, variant="success")

    def _export_audio_only(self) -> None:
        self._on_audio_segment_edited()
        segments = [copy.deepcopy(segment) for segment in self._audio_segments if str(segment.get("text", "")).strip()]
        if not segments:
            Toast.show("Hãy nhập ít nhất một câu cần đọc.", parent=self, variant="warning")
            return
        speed = float(self._audio_export_speed.value())
        preview_dir = self.settings.user_data_dir() / "work"
        preview_dir.mkdir(parents=True, exist_ok=True)
        self._release_audio_preview()
        preview_wav = preview_dir / f"audio-preview-{uuid.uuid4().hex[:10]}.wav"
        self._audio_preview_request = str(preview_wav)
        self._btn_export_audio_only.set_loading(True)

        def _worker() -> None:
            segment_wavs: list[Path] = []
            try:
                router = self.pipeline._make_tts_router()
                for index, segment in enumerate(segments):
                    segment_wav = preview_dir / f"audio-part-{preview_wav.stem}-{index:03d}.wav"
                    router.synthesize_single(
                        str(segment["text"]).strip(),
                        str(segment.get("voice_id") or "edge:vi-VN-HoaiMyNeural"),
                        segment_wav,
                        speed=speed,
                    )
                    segment_wavs.append(segment_wav)
                self._join_audio_segments(segment_wavs, preview_wav)
                self._bridge.audio_export_ready.emit(str(preview_wav))
            except Exception as exc:
                preview_wav.unlink(missing_ok=True)
                self._bridge.audio_export_failed.emit(str(exc))
            finally:
                for segment_wav in segment_wavs:
                    segment_wav.unlink(missing_ok=True)

        self._ui_pool.submit(_worker)

    @staticmethod
    def _join_audio_segments(segment_wavs: list[Path], destination: Path) -> None:
        if not segment_wavs:
            raise RuntimeError("Không có đoạn âm thanh để ghép.")
        with wave.open(str(segment_wavs[0]), "rb") as first:
            channels = first.getnchannels()
            sample_width = first.getsampwidth()
            frame_rate = first.getframerate()
        silence = b"\x00" * int(frame_rate * 0.18) * channels * sample_width
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(sample_width)
            output.setframerate(frame_rate)
            for index, source in enumerate(segment_wavs):
                with wave.open(str(source), "rb") as audio:
                    if (
                        audio.getnchannels() != channels
                        or audio.getsampwidth() != sample_width
                        or audio.getframerate() != frame_rate
                    ):
                        raise RuntimeError("Các đoạn giọng đọc không cùng định dạng âm thanh.")
                    output.writeframes(audio.readframes(audio.getnframes()))
                if index < len(segment_wavs) - 1:
                    output.writeframes(silence)

    def _on_audio_export_ready(self, audio_path: str) -> None:
        self._btn_export_audio_only.set_loading(False)
        if getattr(self, "_audio_preview_request", "") != audio_path:
            self._unlink_audio_preview(Path(audio_path))
            return
        self._audio_preview_path = audio_path
        self._btn_export_audio_only.setText("Dừng Nghe")
        self._btn_save_audio_only.setEnabled(True)
        self._audio_player.stop()
        self._audio_player.setSource(QUrl.fromLocalFile(audio_path))
        self._audio_player.play()
        self._audio_seek_slider.setEnabled(True)
        Toast.show("Đang phát bản nghe thử · Nếu thấy ổn, bấm Lưu File Âm Thanh.", parent=self, variant="success")

    def _on_audio_export_failed(self, error: str) -> None:
        self._btn_export_audio_only.set_loading(False)
        self._audio_preview_request = ""
        self._btn_export_audio_only.setText("Nghe Thử")
        self._btn_save_audio_only.setEnabled(False)
        Toast.show(f"Không tạo được file âm thanh: {error}", parent=self, variant="error")

    def _invalidate_audio_preview(self, *_args) -> None:
        if getattr(self, "_syncing_audio_segment", False):
            return
        self._audio_preview_request = ""
        self._release_audio_preview()
        self._btn_export_audio_only.setText("Nghe Thử")
        self._btn_save_audio_only.setEnabled(False)

    def _release_audio_preview(self) -> None:
        old_path = Path(self._audio_preview_path) if self._audio_preview_path else None
        self._audio_player.stop()
        self._audio_player.setSource(QUrl())
        self._audio_seek_slider.setEnabled(False)
        self._audio_seek_slider.setRange(0, 1)
        self._audio_seek_slider.setValue(0)
        self._audio_time_label.setText("00:00 / 00:00")
        self._audio_preview_path = ""
        self._btn_export_audio_only.setText("Nghe Thử")
        self._btn_export_audio_only.set_variant("primary")
        self._btn_export_audio_only.set_icon("play")
        if old_path:
            QtCore.QTimer.singleShot(200, lambda path=old_path: self._unlink_audio_preview(path))

    def _on_audio_preview_playback_state_changed(self, state) -> None:
        if not self._audio_preview_path:
            return
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._btn_export_audio_only.setText("Dừng Nghe" if playing else "Nghe Thử")
        self._btn_export_audio_only.set_variant("danger" if playing else "primary")
        self._btn_export_audio_only.set_icon("stop" if playing else "play")

    def _on_audio_preview_duration_changed(self, duration_ms: int) -> None:
        if not self._audio_preview_path:
            return
        self._audio_seek_slider.setRange(0, max(1, duration_ms))
        self._audio_seek_slider.setEnabled(duration_ms > 0)
        self._update_audio_time_label(self._audio_player.position(), duration_ms)

    def _on_audio_preview_position_changed(self, position_ms: int) -> None:
        if not self._audio_preview_path:
            return
        if not self._audio_seek_slider.isSliderDown():
            self._audio_seek_slider.setValue(position_ms)
        duration_ms = max(self._audio_player.duration(), self._audio_seek_slider.maximum())
        self._update_audio_time_label(position_ms, duration_ms)

    def _seek_audio_preview(self) -> None:
        if self._audio_preview_path:
            self._audio_player.setPosition(self._audio_seek_slider.value())

    def _update_audio_time_label(self, position_ms: int, duration_ms: int) -> None:
        self._audio_time_label.setText(
            f"{self._format_audio_time(position_ms)} / {self._format_audio_time(duration_ms)}"
        )

    @staticmethod
    def _format_audio_time(milliseconds: int) -> str:
        total_seconds = max(0, int(milliseconds) // 1000)
        return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"

    def _unlink_audio_preview(self, path: Path, attempt: int = 0) -> None:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            if attempt < 10:
                QtCore.QTimer.singleShot(
                    200,
                    lambda target=path, retry=attempt + 1: self._unlink_audio_preview(target, retry),
                )

    def _save_audio_only(self) -> None:
        preview_wav = Path(getattr(self, "_audio_preview_path", ""))
        if not preview_wav.is_file():
            Toast.show("Hãy nghe thử nội dung trước khi lưu.", parent=self, variant="warning")
            self._btn_save_audio_only.setEnabled(False)
            return
        audio_format = str(self._audio_export_format.value() or "mp3")
        raw_name = self._audio_export_name.text().strip()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._-")
        if not safe_name:
            safe_name = f"audio-{QtCore.QDateTime.currentDateTime().toString('yyyyMMdd-HHmmss')}"
        output_root = self.settings.resolved_output_root()
        destination = output_root / f"{safe_name}.{audio_format}"
        counter = 2
        while destination.exists():
            destination = output_root / f"{safe_name}-{counter}.{audio_format}"
            counter += 1
        self._btn_save_audio_only.set_loading(True)

        def _worker() -> None:
            try:
                if audio_format == "wav":
                    shutil.copy2(preview_wav, destination)
                else:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-y", "-v", "error", "-i", str(preview_wav),
                            "-c:a", "libmp3lame", "-b:a", "192k", str(destination),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    if result.returncode != 0 or not destination.exists():
                        raise RuntimeError(f"Không chuyển được sang MP3: {result.stderr[-300:]}")
                self._bridge.audio_save_ready.emit(str(destination))
            except Exception as exc:
                destination.unlink(missing_ok=True)
                self._bridge.audio_save_failed.emit(str(exc))

        self._ui_pool.submit(_worker)

    def _on_audio_save_ready(self, audio_path: str) -> None:
        self._btn_save_audio_only.set_loading(False)
        self._release_audio_preview()
        self._audio_preview_request = ""
        self._btn_export_audio_only.setText("Nghe Thử")
        self._btn_save_audio_only.setEnabled(False)
        Toast.show(f"Đã lưu file âm thanh: {Path(audio_path).name}", parent=self, variant="success")

    def _on_audio_save_failed(self, error: str) -> None:
        self._btn_save_audio_only.set_loading(False)
        Toast.show(f"Không lưu được file âm thanh: {error}", parent=self, variant="error")

    # ------------------------------------------------------------------
    # Stage Layout Sync & Presets
    # ------------------------------------------------------------------

    def _sync_layout_spinners_from_cfg(self) -> None:
        self._syncing_layout_controls = True
        try:
            cfg = self.settings.layout
            self._spin_card_x.set_value(cfg.card_x_pct)
            self._spin_card_y.set_value(cfg.card_y_pct)
            self._spin_card_scale.set_value(cfg.card_scale_pct)
            self._spin_card_rad.set_value(cfg.card_radius_px)
            self._sel_card_theme.set_value(cfg.card_theme)
            self._chk_hide_name.setChecked(cfg.hide_username)

            self._chk_sub_video.setChecked(cfg.sub_enabled)
            self._spin_sub_x.set_value(cfg.sub_x_pct)
            self._spin_sub_y.set_value(cfg.sub_y_pct)
            self._spin_sub_w.set_value(cfg.sub_w_pct)
            self._spin_sub_h.set_value(cfg.sub_h_pct)
            self._spin_sub_rad.set_value(cfg.sub_radius_px)
            self._sel_sub_fit.set_value(cfg.sub_fit)
        finally:
            self._syncing_layout_controls = False

    def _on_layout_controls_changed(self, *_args) -> None:
        if self._syncing_layout_controls:
            return
        cfg = self.settings.layout
        cfg.card_x_pct = float(self._spin_card_x.value())
        cfg.card_y_pct = float(self._spin_card_y.value())
        cfg.card_scale_pct = float(self._spin_card_scale.value())
        cfg.card_radius_px = int(self._spin_card_rad.value())
        cfg.card_theme = str(self._sel_card_theme.value() or "light")
        cfg.hide_username = self._chk_hide_name.isChecked()

        cfg.sub_enabled = self._chk_sub_video.isChecked()
        cfg.sub_x_pct = float(self._spin_sub_x.value())
        cfg.sub_y_pct = float(self._spin_sub_y.value())
        cfg.sub_w_pct = float(self._spin_sub_w.value())
        cfg.sub_h_pct = float(self._spin_sub_h.value())
        cfg.sub_radius_px = int(self._spin_sub_rad.value())
        cfg.sub_fit = str(self._sel_sub_fit.value() or "cover")

        self._canvas.refresh_card()

    def _reset_layout_defaults(self) -> None:
        self.settings.layout = LayoutConfig()
        self._canvas.set_layout_config(self.settings.layout)
        self._sync_layout_spinners_from_cfg()

    def _export_stage_snapshot(self) -> None:
        out_png = self.settings.resolved_output_root() / "preview_snapshot_9x16.png"
        self._canvas.export_snapshot(str(out_png))
        Toast.show(f"Đã lưu ảnh chụp bố cục: {out_png.name}", parent=self, variant="success")

    def _apply_selected_preset(self) -> None:
        idx = self._preset_select.value()
        if idx is None or not (0 <= int(idx) < len(self.settings.presets)):
            return
        p = self.settings.presets[int(idx)]
        self._cmt_mode_select.set_value(p.comment_mode)
        self._cmt_count_spin.set_value(p.max_comments)
        self._speed_quick_spin.set_value(p.voice_speed)
        self._voice_assign_select.set_value(p.voice_assign_mode)
        valid_v = [v for v in p.selected_voices if v in self._voice_label_by_id]
        if valid_v:
            self._voice_pool_multiselect.set_values(valid_v)

        self.settings.media = copy.deepcopy(p.media)
        managed_bg = self._managed_media_library("video nền")
        managed_bgm = self._managed_media_library("nhạc nền")
        if managed_bg.exists() and any(managed_bg.iterdir()):
            self.settings.media.bg_video_dir = str(managed_bg)
        if managed_bgm.exists() and any(managed_bgm.iterdir()):
            self.settings.media.bgm_dir = str(managed_bgm)
        self.settings.media.sub_video_dir = str(self._managed_media_library("video phụ"))
        self._picker_sfx.set_paths([p.media.sfx_dir])
        self._chk_bgm.setChecked(p.media.bgm_enabled)
        self._spin_bgm.set_value(p.media.bgm_volume_pct)
        self._chk_sfx.setChecked(p.media.sfx_enabled)
        self._spin_sfx.set_value(p.media.sfx_volume_pct)

        self.settings.layout = copy.deepcopy(p.layout)
        self._canvas.set_layout_config(self.settings.layout)
        self._sync_layout_spinners_from_cfg()
        Toast.show(f"Đã nạp Preset: {p.name}", parent=self, variant="success")

    def _save_current_as_preset(self) -> None:
        name = self._new_preset_name.text().strip()
        if not name:
            Toast.show("Hãy nhập tên cho Preset mới trước khi lưu.", parent=self, variant="warning")
            return
        self._collect_live_settings()
        new_p = PresetProfile(
            name=name,
            comment_mode=self.settings.comment_mode,
            max_comments=self.settings.max_comments,
            voice_speed=self.settings.voice_speed,
            voice_assign_mode=self.settings.voice_assign_mode,
            selected_voices=list(self.settings.selected_voices),
            media=copy.deepcopy(self.settings.media),
            layout=copy.deepcopy(self.settings.layout),
        )
        self.settings.presets.append(new_p)
        self.settings.save()
        opts = [(p.name, i) for i, p in enumerate(self.settings.presets)]
        self._preset_select.set_options(opts)
        self._preset_select.set_value(len(opts) - 1)
        self._new_preset_name.clear_text()
        Toast.show(f"Đã lưu Preset '{name}'!", parent=self, variant="success")

    def _delete_selected_preset(self) -> None:
        idx = self._preset_select.value()
        if idx is None or not (0 <= int(idx) < len(self.settings.presets)):
            Toast.show("Chưa có Preset nào được chọn để xóa.", parent=self, variant="info")
            return
        removed = self.settings.presets.pop(int(idx))
        self.settings.save()
        options = [(preset.name, i) for i, preset in enumerate(self.settings.presets)]
        self._preset_select.set_options(options)
        if options:
            self._preset_select.set_value(min(int(idx), len(options) - 1))
        Toast.show(f"Đã xóa Preset '{removed.name}'.", parent=self, variant="success")

    def _start_threads_login(self) -> None:
        profile_dir = self.settings.user_data_dir() / "threads_profile"
        Toast.show(
            "Đang mở trình duyệt đăng nhập Threads. Đăng nhập xong hãy đóng cửa sổ trình duyệt để lưu phiên.",
            parent=self,
            variant="info",
            duration=5500,
        )

        def _run() -> None:
            try:
                open_threads_login_session(profile_dir)
                self._bridge.login_finished.emit("Đã lưu phiên đăng nhập Threads thành công!")
            except Exception as exc:
                logger.error("Lỗi mở trình duyệt đăng nhập: %s", exc)

        self._ui_pool.submit(_run)

    def _open_output_root(self) -> None:
        out = self.settings.resolved_output_root()
        if sys.platform == "win32":
            os.startfile(str(out))  # noqa: S606
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
