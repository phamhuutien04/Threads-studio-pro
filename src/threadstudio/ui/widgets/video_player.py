"""Studio video preview with controls and an explicit way out of full screen."""
from __future__ import annotations

from pathlib import Path

from fluent_blue import Button, ThemeManager, VideoPlayer
from fluent_blue.compatibility.qt import (
    QDialog,
    QEvent,
    QHBoxLayout,
    QKeySequence,
    QShortcut,
    Qt,
    QUrl,
    QVBoxLayout,
)


class StudioVideoPlayer(VideoPlayer):
    """Keep the picture and transport controls together in a full-screen window."""

    def __init__(self, *args, **kwargs) -> None:
        self._fullscreen_window: QDialog | None = None
        self._controls_were_visible = True
        self._ignore_video_release = False
        super().__init__(*args, **kwargs)
        self.fullscreen_button.setToolTip("Toàn màn hình (F)")
        self.fullscreen_button.setAccessibleName(self.fullscreen_button.toolTip())

    def toggle_fullscreen(self) -> None:
        if self._fullscreen_window is not None:
            self.exit_fullscreen()
            return
        if self.video is None:
            return

        # QVideoWidget.setFullScreen() detaches only the picture, leaving all
        # controls behind. Move both widgets into an owned window instead.
        window = QDialog(self.window())
        window.setWindowTitle("Xem video — Esc để thoát toàn màn hình")
        self._fullscreen_window = window
        self._controls_were_visible = not self.controls.isHidden()
        column = QVBoxLayout(window)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        header = QHBoxLayout()
        margin = ThemeManager.spacing(2)
        header.setContentsMargins(margin, margin, margin, margin)
        header.addStretch(1)
        exit_button = Button("Thoát toàn màn hình (Esc)", variant="secondary", icon="x")
        exit_button.setObjectName("exitFullscreenButton")
        exit_button.setAutoDefault(False)
        exit_button.clicked.connect(self.exit_fullscreen)
        header.addWidget(exit_button)
        column.addLayout(header)
        column.addWidget(self.stage, 1)
        column.addWidget(self.controls)
        self.stage.show()
        self.controls.show()
        self.fullscreen_button.set_icon("minimize")
        self.fullscreen_button.setToolTip("Thoát toàn màn hình (Esc)")
        self.fullscreen_button.setAccessibleName(self.fullscreen_button.toolTip())
        for key in ("Escape", "F", "F11"):
            shortcut = QShortcut(QKeySequence(key), window)
            shortcut.activated.connect(self.exit_fullscreen)
        window.finished.connect(self._restore_inline)
        window.showFullScreen()
        self.video.setFocus(Qt.FocusReason.OtherFocusReason)

    def exit_fullscreen(self) -> None:
        if self._fullscreen_window is not None:
            self._fullscreen_window.close()

    def _restore_inline(self) -> None:
        window = self._fullscreen_window
        if window is None:
            return
        self._fullscreen_window = None
        self.layout().addWidget(self.stage, 1)
        self.layout().addWidget(self.controls)
        self.stage.show()
        self.controls.setVisible(self._controls_were_visible)
        self.fullscreen_button.set_icon("maximize")
        self.fullscreen_button.setToolTip("Toàn màn hình (F)")
        self.fullscreen_button.setAccessibleName(self.fullscreen_button.toolTip())
        window.deleteLater()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.video:
            if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self._ignore_video_release = True
                self.toggle_fullscreen()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if self._ignore_video_release:
                    self._ignore_video_release = False
                    return True
                if self._fullscreen_window is not None:
                    self.video.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.toggle()
                    return True
            if event.type() == QEvent.Type.KeyPress and self._fullscreen_window is not None:
                self.keyPressEvent(event)
                return event.isAccepted()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape and self._fullscreen_window is not None:
            self.exit_fullscreen()
            event.accept()
        elif event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_source(self, source: str | Path | QUrl) -> None:
        self.exit_fullscreen()
        super().set_source(source)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.exit_fullscreen()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.exit_fullscreen()
        super().closeEvent(event)