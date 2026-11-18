"""Exercise real Qt widgets to prevent full-screen video from trapping the user."""
from __future__ import annotations

import importlib
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fluent_blue import Tabs
from fluent_blue.compatibility.qt import (
    BINDING,
    QApplication,
    QEvent,
    Qt,
    QVBoxLayout,
    QWidget,
)

from threadstudio.ui.widgets.video_player import StudioVideoPlayer

QTest = importlib.import_module(f"{BINDING}.QtTest").QTest


@unittest.skipUnless(StudioVideoPlayer.is_available(), "QtMultimedia is unavailable")
class VideoFullscreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(800, 700)
        layout = QVBoxLayout(self.host)
        self.tabs = Tabs()
        self.tabs.add_tab(QWidget(), "Layout")
        self.player = StudioVideoPlayer()
        self.tabs.add_tab(self.player, "Video")
        layout.addWidget(self.tabs)
        self.tabs.setCurrentIndex(1)
        self.player._stack.setCurrentWidget(self.player.video)
        self.host.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.player.exit_fullscreen()
        self.player.release()
        self.host.close()
        self.host.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def enter_fullscreen(self):
        self.player.fullscreen_button.click()
        self.app.processEvents()
        window = self.player._fullscreen_window
        self.assertIsNotNone(window)
        self.assertTrue(window.isFullScreen())
        window.activateWindow()
        self.player.video.setFocus()
        self.app.processEvents()
        return window

    def assert_inline(self) -> None:
        self.app.processEvents()
        self.assertIsNone(self.player._fullscreen_window)
        self.assertIs(self.player.stage.parentWidget(), self.player)
        self.assertIs(self.player.controls.parentWidget(), self.player)
        self.assertEqual(self.player.layout().count(), 2)
        self.assertIs(self.tabs.widget(1), self.player)
        self.assertEqual(self.tabs.count(), 2)
        self.assertFalse(self.player.video.isFullScreen())

    def test_playback_preview_stays_embedded_until_fullscreen_is_requested(self) -> None:
        self.assert_inline()
        self.assertFalse(self.host.isFullScreen())
        self.assertTrue(self.tabs.tabBar().isVisible())

    def test_fullscreen_includes_controls_and_visible_exit_button(self) -> None:
        window = self.enter_fullscreen()
        self.assertIs(self.player.stage.window(), window)
        self.assertIs(self.player.controls.window(), window)
        self.assertTrue(self.player.controls.isVisible())
        exit_button = window.findChild(QWidget, "exitFullscreenButton")
        self.assertIsNotNone(exit_button)
        self.assertTrue(exit_button.isVisible())
        exit_button.click()
        self.assert_inline()

    def test_escape_exits_when_video_has_focus(self) -> None:
        self.enter_fullscreen()
        QTest.keyClick(self.player.video, Qt.Key.Key_Escape)
        self.assert_inline()

    def test_escape_exits_when_playback_control_has_focus(self) -> None:
        self.enter_fullscreen()
        self.player.play_button.setFocus()
        QTest.keyClick(self.player.play_button, Qt.Key.Key_Escape)
        self.assert_inline()

    def test_fullscreen_control_can_exit_repeatedly(self) -> None:
        for _ in range(3):
            self.enter_fullscreen()
            self.player.fullscreen_button.click()
            self.assert_inline()

    def test_double_click_on_video_exits(self) -> None:
        self.enter_fullscreen()
        QTest.mouseDClick(self.player.video, Qt.MouseButton.LeftButton)
        self.assert_inline()

    def test_f_and_f11_exit(self) -> None:
        for key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            with self.subTest(key=key):
                self.enter_fullscreen()
                QTest.keyClick(self.player.video, key)
                self.assert_inline()

    def test_window_close_returns_to_preview_without_closing_app(self) -> None:
        self.enter_fullscreen().close()
        self.assert_inline()
        self.assertTrue(self.host.isVisible())

    def test_closing_host_also_closes_fullscreen_window(self) -> None:
        window = self.enter_fullscreen()
        self.host.close()
        self.assertFalse(window.isVisible())
        self.assert_inline()

    def test_loading_another_source_restores_inline_preview(self) -> None:
        self.enter_fullscreen()
        # No media backend work is needed to verify the window lifecycle.
        from unittest.mock import patch
        with patch("fluent_blue.VideoPlayer.set_source"):
            self.player.set_source("another-video.mp4")
        self.assert_inline()

    def test_hidden_controls_are_restored_after_fullscreen(self) -> None:
        self.player.controls.hide()
        self.enter_fullscreen()
        self.assertTrue(self.player.controls.isVisible())
        self.player.exit_fullscreen()
        self.assert_inline()
        self.assertTrue(self.player.controls.isHidden())

    def test_switching_tabs_exits_and_pauses_preview(self) -> None:
        self.enter_fullscreen()
        self.tabs.setCurrentIndex(0)
        self.assert_inline()
        self.assertEqual(self.player.state(), "stopped")
        self.assertTrue(self.host.isVisible())

    def test_enter_and_exit_preserve_media_player_and_playback_settings(self) -> None:
        media_player = self.player.player
        self.player.set_volume(0.35)
        self.player.set_speed(1.5)
        self.player.set_muted(True)
        self.enter_fullscreen()
        self.player.exit_fullscreen()
        self.assert_inline()
        self.assertIs(self.player.player, media_player)
        self.assertAlmostEqual(self.player.volume(), 0.35)
        self.assertTrue(self.player.is_muted())
        self.assertEqual(self.player.speed_rate(), 1.5)


if __name__ == "__main__":
    unittest.main()