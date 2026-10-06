"""The Jeeves video player (runs in the popups process).

Separate video + audio streams (up to 1080p) kept in sync, or one combined stream. Opens
fullscreen. The title bar and controls slide away after 5 seconds without input, or as soon
as the window loses focus, and come back on any mouse movement or key.

Mouse: click the picture = pause/play, double-click = fullscreen.
Keys: Space/K pause · ←/→ 10 s (J/L too) · ,/. one frame (pauses) · Shift+,/Shift+. speed ·
      PgUp/PgDn previous/next chapter · ↑/↓ volume · M mute · F fullscreen · Esc leave fullscreen/close.
Voice (through the daemon): pause, resume, forward/back, louder/quieter, fullscreen, faster/slower,
next/previous chapter, close.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QSlider, QVBoxLayout,
                               QWidget)

HIDE_AFTER_MS = 5000
SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


def fmt(ms: float) -> str:
    s = int(max(0, ms) // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class ChapterSlider(QSlider):
    """The seek bar, with a tick at each chapter start."""

    def __init__(self) -> None:
        super().__init__(Qt.Horizontal)
        self.chapters: list[dict[str, Any]] = []

    def paintEvent(self, e: Any) -> None:
        super().paintEvent(e)
        if not self.chapters or self.maximum() <= 0:
            return
        p = QPainter(self)
        p.setPen(QPen(QColor(255, 255, 255, 200), 2))
        for c in self.chapters[1:]:
            x = 8 + (self.width() - 16) * (c["start"] * 1000 / self.maximum())
            p.drawLine(int(x), self.height() // 2 - 6, int(x), self.height() // 2 + 6)
        p.end()


class VideoPlayer(QWidget):
    current: "VideoPlayer | None" = None

    def __init__(self, call: Callable[..., None]) -> None:
        """call(method, **params): talks to the daemon (quality changes, 'video closed')."""
        super().__init__(None, Qt.Window)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.call = call
        self.setWindowTitle("Jeeves — video")
        self.setStyleSheet("QWidget { background: #000; color: #eee; } QPushButton { background: #1d1d1d; "
                           "border: 1px solid #3a3a3a; border-radius: 6px; padding: 4px 10px; } "
                           "QPushButton:hover { background: #2c2c2c; } QMenu { background: #1d1d1d; } "
                           "QMenu::item:selected { background: #333; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # top bar: title and the current chapter
        self.top = QWidget()
        tl = QHBoxLayout(self.top)
        tl.setContentsMargins(12, 6, 12, 6)
        self.title = QLabel("")
        self.title.setStyleSheet("QLabel { font-size: 15px; font-weight: bold; }")
        self.chapter = QLabel("")
        self.chapter.setStyleSheet("QLabel { color: #aaa; }")
        tl.addWidget(self.title, 1)
        tl.addWidget(self.chapter)
        lay.addWidget(self.top)
        self.screen_w = QVideoWidget()
        lay.addWidget(self.screen_w, 1)
        # bottom bar: the controls
        self.bottom = QWidget()
        bar = QHBoxLayout(self.bottom)
        bar.setContentsMargins(10, 6, 10, 8)
        bar.setSpacing(8)

        def button(text: str, fn: Callable[[], Any], tip: str = "") -> QPushButton:
            b = QPushButton(text)
            b.clicked.connect(fn)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            return b
        self.play_btn = button("Pause", self.toggle, "Space")
        self.pos = ChapterSlider()
        self.pos.setFocusPolicy(Qt.NoFocus)
        self.pos.sliderMoved.connect(self.seek_to)
        self.time = QLabel("0:00 / 0:00")
        self.speed_btn = button("1×", self._speed_menu, "Speed (Shift+, / Shift+.)")
        self.quality_btn = button("Auto", self._quality_menu, "Quality")
        self.chapters_btn = button("Chapters", self._chapters_menu, "PgUp / PgDn")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(100)
        self.vol.setFocusPolicy(Qt.NoFocus)
        self.vol.valueChanged.connect(self._volume)
        widgets = [self.play_btn, button("◀|", lambda: self.step_frame(-1), "Previous frame (,)"),
                   button("|▶", lambda: self.step_frame(1), "Next frame (.)"), self.pos, self.time, self.speed_btn,
                   self.quality_btn, self.chapters_btn, QLabel("Vol"), self.vol,
                   button("YouTube", self._open_page), button("Fullscreen", self.fullscreen, "F"),
                   button("Close", self.close, "Esc")]
        for w in widgets:
            bar.addWidget(w, 1 if w is self.pos else 0)
        lay.addWidget(self.bottom)

        self.video = QMediaPlayer(self)
        self.video_out = QAudioOutput(self)
        self.video.setAudioOutput(self.video_out)
        self.video.setVideoOutput(self.screen_w)
        self.audio = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.audio.setAudioOutput(self.audio_out)
        self.video.durationChanged.connect(lambda d: self.pos.setRange(0, int(d)))
        self.video.positionChanged.connect(self._tick)
        self.video.mediaStatusChanged.connect(self._status)
        self.video.errorOccurred.connect(lambda _e, msg: self.title.setText(f"Couldn't play it: {msg}"))
        self.sync = QTimer(self)
        self.sync.setInterval(500)
        self.sync.timeout.connect(self._sync)
        # controls hide after a while without input
        self.idle = QTimer(self)
        self.idle.setSingleShot(True)
        self.idle.setInterval(HIDE_AFTER_MS)
        self.idle.timeout.connect(self.hide_controls)
        self.click_timer = QTimer(self)                 # one click = pause, two = fullscreen
        self.click_timer.setSingleShot(True)
        self.click_timer.setInterval(QApplication.doubleClickInterval() if QApplication.instance() else 250)
        self.click_timer.timeout.connect(self.toggle)
        self.controls_shown = True
        self._anims: list[QPropertyAnimation] = []
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)                # mouse moves over the native video surface too
        self.page = ""
        self.split = False
        self.fps = 30.0
        self.chapters: list[dict[str, Any]] = []
        self.heights: list[int] = []
        self.height_now = 0
        self.pending_seek: int | None = None
        self.rate = 1.0          # kept here: some backends report 0 or reset it when a new source loads
        self.resize(1100, 680)

    # ------------------------------------------------------------------ loading
    def play(self, d: dict[str, Any]) -> None:
        self.page = d.get("page", "")
        self.title.setText(f"{d.get('title', '')}  —  {d.get('channel', '')}")
        self.split = bool(d.get("audio"))
        self.fps = float(d.get("fps") or 30) or 30.0
        self.chapters = sorted(d.get("chapters") or [], key=lambda c: c.get("start", 0))
        self.pos.chapters = self.chapters
        self.chapters_btn.setVisible(bool(self.chapters))
        self.heights = sorted({int(h) for h in d.get("heights") or [] if h}, reverse=True)
        self.height_now = int(d.get("height") or 0)
        self.quality_btn.setText(f"{self.height_now}p" if self.height_now else "Auto")
        self.quality_btn.setVisible(bool(self.heights))
        self.video.setSource(QUrl(d["video"]))
        if self.split:                       # separate audio: it leads, the picture follows
            self.audio.setSource(QUrl(d["audio"]))
            self.video_out.setMuted(True)
            self.audio.play()
            self.sync.start()
        else:
            self.audio.setSource(QUrl())
            self.video_out.setMuted(False)
            self.sync.stop()
        self.pending_seek = int(d["start"]) if d.get("start") else None
        self.set_speed(self.rate)                 # a new source (quality change) keeps the speed
        self._volume(self.vol.value())
        self.video.play()
        self.play_btn.setText("Pause")
        fs = d.get("fullscreen", True)          # None (a quality change): stay as it is
        if fs or (fs is None and self.isFullScreen()):
            self.showFullScreen()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.show_controls()

    def _status(self, status: Any) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if self.pending_seek is not None and status in (QMediaPlayer.MediaStatus.LoadedMedia,
                                                        QMediaPlayer.MediaStatus.BufferedMedia):
            ms, self.pending_seek = self.pending_seek, None
            self.seek_to(ms)                 # a quality change continues where it was

    # ------------------------------------------------------------------ playback
    def _master(self) -> Any:
        return self.audio if self.split else self.video

    def _sync(self) -> None:
        a, v = self.audio.position(), self.video.position()
        if self.audio.playbackState() == self.audio.PlaybackState.PlayingState and abs(a - v) > 150:
            self.video.setPosition(a)

    def _tick(self, ms: int) -> None:
        if not self.pos.isSliderDown():
            self.pos.setValue(int(ms))
        self.time.setText(f"{fmt(ms)} / {fmt(self.video.duration())}")
        ch = self.chapter_at(ms)
        self.chapter.setText(ch.get("title", "") if ch else "")

    def _volume(self, v: int) -> None:
        (self.audio_out if self.split else self.video_out).setVolume(v / 100)

    def playing(self) -> bool:
        return self.video.playbackState() == self.video.PlaybackState.PlayingState

    def toggle(self) -> None:
        self.pause() if self.playing() else self.resume()

    def pause(self) -> None:
        self.video.pause()
        self.audio.pause()
        self.play_btn.setText("Play")
        self.show_controls()

    def resume(self) -> None:
        if self.split:
            self.audio.play()
        self.video.play()
        self.play_btn.setText("Pause")
        self.show_controls()

    def seek_to(self, ms: int) -> None:
        ms = max(0, int(ms))
        self.video.setPosition(ms)
        if self.split:
            self.audio.setPosition(ms)

    def seek(self, seconds: float) -> None:
        self.seek_to(self._master().position() + int(seconds * 1000))

    def step_frame(self, n: int) -> None:
        """Frame by frame: pauses, then moves one frame."""
        if self.playing():
            self.pause()
        self.seek_to(self.video.position() + int(round(n * 1000 / self.fps)))

    def set_speed(self, rate: float) -> None:
        rate = min(SPEEDS[-1], max(SPEEDS[0], float(rate)))
        self.rate = rate
        self.video.setPlaybackRate(rate)
        self.audio.setPlaybackRate(rate)
        self.speed_btn.setText(f"{rate:g}×")

    def change_speed(self, steps: int) -> None:
        i = min(range(len(SPEEDS)), key=lambda k: abs(SPEEDS[k] - self.rate))
        self.set_speed(SPEEDS[max(0, min(len(SPEEDS) - 1, i + steps))])

    def set_quality(self, height: int) -> None:
        """Ask the daemon for the streams at this height; playback continues from here."""
        if height == self.height_now:
            return
        self.quality_btn.setText(f"{height}p…")
        self.call("video.quality", height=height, position=int(self._master().position()))

    def chapter_at(self, ms: float) -> dict[str, Any] | None:
        cur = None
        for c in self.chapters:
            if c.get("start", 0) * 1000 <= ms + 1:
                cur = c
        return cur

    def jump_chapter(self, d: int) -> None:
        if not self.chapters:
            return
        ms = self._master().position()
        starts = [int(c.get("start", 0) * 1000) for c in self.chapters]
        if d > 0:
            nxt = next((s for s in starts if s > ms + 500), None)
        else:                                 # back: start of this chapter, or the previous one if just past it
            before = [s for s in starts if s < ms - 2000]
            nxt = before[-1] if before else 0
        if nxt is not None:
            self.seek_to(nxt)

    def louder(self, step: int = 15) -> None:
        self.vol.setValue(min(100, max(0, self.vol.value() + step)))

    def mute(self) -> None:
        out = self.audio_out if self.split else self.video_out
        out.setMuted(not out.isMuted())

    def fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _open_page(self) -> None:
        from PySide6.QtGui import QDesktopServices
        if self.page:
            self.pause()
            QDesktopServices.openUrl(QUrl(self.page))

    # ------------------------------------------------------------------ menus
    def _menu(self, items: list[tuple[str, Callable[[], Any], bool]], under: QWidget) -> None:
        m = QMenu(self)
        for text, fn, checked in items:
            a = m.addAction(text)
            a.setCheckable(True)
            a.setChecked(checked)
            a.triggered.connect(fn)
        self.idle.stop()
        m.exec(under.mapToGlobal(QPoint(0, 0)) - QPoint(0, m.sizeHint().height()))
        self.show_controls()

    def _speed_menu(self) -> None:
        self._menu([(f"{s:g}×", lambda _=False, s=s: self.set_speed(s), abs(s - self.rate) < 0.01) for s in SPEEDS],
                   self.speed_btn)

    def _quality_menu(self) -> None:
        self._menu([(f"{h}p", lambda _=False, h=h: self.set_quality(h), h == self.height_now) for h in self.heights],
                   self.quality_btn)

    def _chapters_menu(self) -> None:
        cur = self.chapter_at(self._master().position())
        self._menu([(f"{fmt(c.get('start', 0) * 1000)}  {c.get('title', '')}",
                     lambda _=False, c=c: self.seek_to(int(c.get("start", 0) * 1000)), c is cur)
                    for c in self.chapters], self.chapters_btn)

    # ------------------------------------------------------------------ controls that hide
    def _slide(self, w: QWidget, show: bool) -> None:
        full = w.sizeHint().height()
        anim = QPropertyAnimation(w, b"maximumHeight", self)
        anim.setDuration(220)
        anim.setStartValue(w.height() if w.isVisible() else 0)
        anim.setEndValue(full if show else 0)
        anim.setEasingCurve(QEasingCurve.OutCubic if show else QEasingCurve.InCubic)
        anim.start()
        self._anims = [a for a in self._anims if a.state() == QPropertyAnimation.Running] + [anim]

    def show_controls(self) -> None:
        self.idle.start()
        self.unsetCursor()
        if self.controls_shown:
            return
        self.controls_shown = True
        self._slide(self.top, True)
        self._slide(self.bottom, True)

    def hide_controls(self) -> None:
        if not self.controls_shown or (not self.playing() and self.isActiveWindow()):
            return                               # paused and looked at: keep them
        self.controls_shown = False
        self._slide(self.top, False)
        self._slide(self.bottom, False)
        if self.isActiveWindow():
            self.setCursor(Qt.BlankCursor)

    def changeEvent(self, e: Any) -> None:
        if e.type() == QEvent.ActivationChange:
            if self.isActiveWindow():
                self.show_controls()
            elif self.controls_shown:            # looking at something else: get out of the way now
                self.idle.stop()
                self.controls_shown = False
                self._slide(self.top, False)
                self._slide(self.bottom, False)
        super().changeEvent(e)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:
        t = e.type()
        mine = isinstance(obj, QWidget) and obj.window() is self
        if mine and t in (QEvent.MouseMove, QEvent.Wheel, QEvent.KeyPress, QEvent.MouseButtonPress):
            self.show_controls()
        on_picture = mine and (obj is self.screen_w or self.screen_w.isAncestorOf(obj))
        if on_picture:
            if t == QEvent.MouseButtonRelease and e.button() == Qt.LeftButton:
                self.click_timer.start()         # a click on the picture: pause/play (unless a double-click)
                return True
            if t == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
                self.click_timer.stop()
                self.fullscreen()
                return True
        return False

    def keyPressEvent(self, e: Any) -> None:
        k, shift = e.key(), bool(e.modifiers() & Qt.ShiftModifier)
        self.show_controls()
        if k in (Qt.Key_Space, Qt.Key_K):
            self.toggle()
        elif k in (Qt.Key_Right, Qt.Key_L):
            self.seek(10)
        elif k in (Qt.Key_Left, Qt.Key_J):
            self.seek(-10)
        elif k in (Qt.Key_Less,) or (k == Qt.Key_Comma and shift):
            self.change_speed(-1)
        elif k in (Qt.Key_Greater,) or (k == Qt.Key_Period and shift):
            self.change_speed(1)
        elif k == Qt.Key_Comma:
            self.step_frame(-1)
        elif k == Qt.Key_Period:
            self.step_frame(1)
        elif k == Qt.Key_PageDown:
            self.jump_chapter(1)
        elif k == Qt.Key_PageUp:
            self.jump_chapter(-1)
        elif k == Qt.Key_Up:
            self.louder(5)
        elif k == Qt.Key_Down:
            self.louder(-5)
        elif k == Qt.Key_M:
            self.mute()
        elif k == Qt.Key_F:
            self.fullscreen()
        elif k == Qt.Key_Escape:
            self.showNormal() if self.isFullScreen() else self.close()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e: Any) -> None:
        self.video.stop()
        self.audio.stop()
        self.sync.stop()
        self.idle.stop()
        self.unsetCursor()
        self.call("video.closed")
        super().closeEvent(e)

    def control(self, d: dict[str, Any]) -> None:
        act = d.get("action")
        secs = float(d.get("seconds") or 10)
        {"pause": self.pause, "resume": self.resume, "stop": self.close, "fullscreen": self.fullscreen,
         "forward": lambda: self.seek(secs), "back": lambda: self.seek(-secs),
         "louder": lambda: self.louder(15), "quieter": lambda: self.louder(-15),
         "faster": lambda: self.change_speed(1), "slower": lambda: self.change_speed(-1),
         "next_chapter": lambda: self.jump_chapter(1), "previous_chapter": lambda: self.jump_chapter(-1),
         }.get(act, lambda: None)()


__all__ = ["VideoPlayer", "fmt"]
