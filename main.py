#!/usr/bin/env python3
"""VexoTTS - type text, hear it, and send it to a virtual microphone.

Requires (Debian/Ubuntu):
    sudo apt install espeak-ng pulseaudio-utils
    pip install PySide6
"""

import os
import random
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import QProcess, QRectF, QSettings, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "VexoTTS"
SINK = "vexotts_output"
SOURCE = "vexotts_mic"
MIC_LABEL = "VexoTTS_Microphone"
MAX_HISTORY = 25
MAX_CHARS = 5000

# Palette: warm slate, with amber reserved for "on air" and teal for "ready".
BASE = "#1A1E26"
PANEL = "#222733"
FIELD = "#151920"
LINE = "#323A4B"
TEXT = "#E9ECF2"
MUTED = "#8E96A8"
AMBER = "#F2A93B"
TEAL = "#43B8A6"
RED = "#E5675F"

STYLE = f"""
* {{ font-family: 'Inter', 'Noto Sans', 'Segoe UI', sans-serif; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BASE}; }}
QLabel#title {{ font-size: 26px; font-weight: 700; }}
QLabel#subtitle, QLabel#muted {{ color: {MUTED}; }}
QLabel#section {{ font-size: 14px; font-weight: 600; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#sidebar {{ background: {PANEL}; border-left: 1px solid {LINE}; }}
QFrame#divider {{ background: {LINE}; max-height: 1px; min-height: 1px; border: none; }}

QPlainTextEdit {{
    background: {FIELD}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 10px; font-size: 15px; selection-background-color: {AMBER}; selection-color: #1A1E26;
}}
QPlainTextEdit:focus {{ border-color: {AMBER}; }}

QComboBox {{
    background: {FIELD}; border: 1px solid {LINE}; border-radius: 8px; padding: 7px 10px; min-height: 20px;
}}
QComboBox:hover, QComboBox:focus {{ border-color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {PANEL}; border: 1px solid {LINE}; selection-background-color: {LINE}; outline: none;
}}

QSlider::groove:horizontal {{ height: 4px; background: {LINE}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {AMBER}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {AMBER}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid {MUTED}; background: {FIELD};
}}
QCheckBox::indicator:checked {{ background: {AMBER}; border-color: {AMBER}; }}

QPushButton {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 8px; padding: 8px 16px;
}}
QPushButton:hover {{ border-color: {MUTED}; }}
QPushButton:disabled {{ color: {MUTED}; background: {BASE}; }}
QPushButton#speak {{
    background: {AMBER}; color: #1A1E26; border: none; font-weight: 700; font-size: 15px; padding: 12px 28px;
}}
QPushButton#speak:hover {{ background: #F6B955; }}
QPushButton#speak:disabled {{ background: {LINE}; color: {MUTED}; }}
QPushButton#stop {{ font-size: 15px; padding: 12px 22px; }}
QPushButton#stop:enabled {{ border-color: {RED}; color: {RED}; }}
QPushButton#link {{ border: none; background: transparent; color: {MUTED}; padding: 4px 8px; }}
QPushButton#link:hover {{ color: {TEXT}; }}

QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 9px 8px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {LINE}; }}
QListWidget::item:selected {{ background: {LINE}; color: {TEXT}; }}

QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {LINE}; padding: 6px; }}
"""

VARIANTS = [
    ("Default", ""),
    ("Male 1", "m1"), ("Male 2", "m2"), ("Male 3", "m3"), ("Male 4", "m4"),
    ("Male 5", "m5"), ("Male 6", "m6"), ("Male 7", "m7"),
    ("Female 1", "f1"), ("Female 2", "f2"), ("Female 3", "f3"),
    ("Female 4", "f4"), ("Female 5", "f5"),
    ("Croak", "croak"), ("Whisper", "whisper"),
]

DEFAULTS = {"speed": 150, "pitch": 50, "volume": 100}


# --------------------------------------------------------------------------
# Audio plumbing
# --------------------------------------------------------------------------
class AudioError(RuntimeError):
    pass


def pactl(*args):
    try:
        return subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Could not run pactl: {exc}") from exc


class VirtualMic:
    """Creates the virtual output + microphone, and removes only what it created."""

    def __init__(self):
        self._owned_modules = []

    @staticmethod
    def _exists(kind, name):
        result = pactl("list", "short", kind)
        if result.returncode != 0:
            raise AudioError(result.stderr.strip() or f"Could not list audio {kind}.")
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == name:
                return True
        return False

    def is_ready(self):
        try:
            return self._exists("sinks", SINK) and self._exists("sources", SOURCE)
        except AudioError:
            return False

    def _load(self, module, *params, error):
        result = pactl("load-module", module, *params)
        if result.returncode != 0:
            raise AudioError(error + "\n" + (result.stderr.strip() or "The audio server returned an error."))
        module_id = result.stdout.strip()
        if module_id.isdigit():
            self._owned_modules.append(int(module_id))

    def ensure(self):
        if not self._exists("sinks", SINK):
            self._load(
                "module-null-sink",
                f"sink_name={SINK}",
                "sink_properties=device.description=VexoTTS_Output",
                error="Could not create the VexoTTS virtual output.",
            )
        if not self._exists("sources", SOURCE):
            self._load(
                "module-remap-source",
                f"master={SINK}.monitor",
                f"source_name={SOURCE}",
                "source_properties=device.description=" + MIC_LABEL,
                error="Could not create the VexoTTS virtual microphone.",
            )
        if not self._exists("sources", SOURCE):
            raise AudioError("The VexoTTS microphone was not found after setup.")

    def teardown(self):
        for module_id in reversed(self._owned_modules):
            try:
                pactl("unload-module", str(module_id))
            except AudioError:
                pass
        self._owned_modules.clear()


def load_voices():
    """Return [(language_id, label)] from espeak-ng, English first."""
    try:
        result = subprocess.run(
            ["espeak-ng", "--voices"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    voices = []
    if result and result.returncode == 0:
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4:
                lang, name = fields[1], fields[3].replace("_", " ")
                voices.append((lang, f"{name}  ({lang})"))
    if not voices:
        voices = [("en-us", "English (America)  (en-us)")]
    voices.sort(key=lambda v: (not v[0].lower().startswith("en"), v[1].lower()))
    return voices


class SpeechEngine(QObject):
    """Runs espeak-ng once, then plays the WAV to the chosen outputs with QProcess."""

    status = Signal(str)
    finished = Signal(str, str)  # kind: "ok" | "error" | "stopped", message

    def __init__(self, parent=None):
        super().__init__(parent)
        self._procs = set()
        self._wav = None
        self.busy = False

    def speak(self, text, voice, speed, pitch, volume, to_local, to_virtual):
        if self.busy:
            return
        self.busy = True
        self._to_local, self._to_virtual = to_local, to_virtual

        fd, self._wav = tempfile.mkstemp(prefix="vexotts_", suffix=".wav")
        os.close(fd)

        self.status.emit("Generating speech…")
        generator = self._spawn(
            "gen",
            "espeak-ng",
            ["-v", voice, "-s", str(speed), "-p", str(pitch), "-a", str(volume), "--stdin", "--stdout"],
            stdout_file=self._wav,
        )
        # Text goes through stdin, so long text and text starting with "-" are safe.
        generator.write(text.encode("utf-8"))
        generator.closeWriteChannel()

    def stop(self):
        if self.busy:
            self._kill_all()
            self._finish("stopped", "Stopped.")

    def shutdown(self):
        self._kill_all(wait=True)
        self._cleanup_file()
        self.busy = False

    # -- internals ---------------------------------------------------------
    def _spawn(self, role, program, args, stdout_file=None):
        proc = QProcess(self)
        proc.setProperty("role", role)
        if stdout_file:
            proc.setStandardOutputFile(stdout_file)
        proc.errorOccurred.connect(lambda err, p=proc: self._on_error(p, err))
        proc.finished.connect(lambda code, st, p=proc: self._on_finished(p, code, st))
        proc.finished.connect(proc.deleteLater)
        self._procs.add(proc)
        proc.start(program, args)
        return proc

    def _on_error(self, proc, error):
        if proc in self._procs and error == QProcess.ProcessError.FailedToStart:
            self._procs.discard(proc)
            self._fail(f"Could not start {proc.program()}. Is it installed?")

    def _on_finished(self, proc, code, exit_status):
        if proc not in self._procs:
            return  # cancelled or already handled
        self._procs.discard(proc)
        detail = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace").strip()
        crashed = exit_status != QProcess.ExitStatus.NormalExit

        if proc.property("role") == "gen":
            if code != 0 or crashed:
                return self._fail(detail or "eSpeak NG could not generate the speech.")
            if not self._wav or os.path.getsize(self._wav) < 44:
                return self._fail("eSpeak NG produced an empty audio file.")
            self._start_playback()
        else:
            if code != 0 or crashed:
                return self._fail(detail or "Audio playback failed.")
            if not any(p.property("role") == "play" for p in self._procs):
                self._finish("ok", "Finished. Ready for more.")

    def _start_playback(self):
        targets = []
        if self._to_local:
            targets.append(["paplay", self._wav])
        if self._to_virtual:
            targets.append(["paplay", f"--device={SINK}", self._wav])
        if not targets:
            return self._fail("No output selected.")
        self.status.emit("Speaking…")
        for cmd in targets:
            self._spawn("play", cmd[0], cmd[1:])

    def _kill_all(self, wait=False):
        procs = list(self._procs)
        self._procs.clear()
        for proc in procs:
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()
        if wait:
            for proc in procs:
                proc.waitForFinished(500)

    def _cleanup_file(self):
        if self._wav:
            try:
                os.unlink(self._wav)
            except OSError:
                pass
            self._wav = None

    def _fail(self, message):
        self._kill_all()
        self._finish("error", message)

    def _finish(self, kind, message):
        self._cleanup_file()
        self.busy = False
        self.finished.emit(kind, message)


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------
class WaveStrip(QWidget):
    """A row of bars that moves while speaking and settles when idle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self._levels = [0.1] * 48
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._tick)

    def set_active(self, active):
        self._active = active
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self):
        settled = True
        for i, level in enumerate(self._levels):
            target = random.uniform(0.2, 1.0) if self._active else 0.1
            level += (target - level) * 0.35
            self._levels[i] = level
            if not self._active and level > 0.12:
                settled = False
        if not self._active and settled:
            self._timer.stop()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(AMBER if self._active else LINE))
        n, gap = len(self._levels), 4
        bar_w = max(2.0, (self.width() - gap * (n - 1)) / n)
        for i, level in enumerate(self._levels):
            h = max(4.0, level * self.height())
            rect = QRectF(i * (bar_w + gap), (self.height() - h) / 2, bar_w, h)
            painter.drawRoundedRect(rect, bar_w / 2, bar_w / 2)


class SliderRow(QWidget):
    def __init__(self, label, low, high, value, unit="", parent=None):
        super().__init__(parent)
        self._unit = unit
        self._default = value
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        name = QLabel(label)
        name.setFixedWidth(60)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(low, high)
        self.slider.setValue(value)
        self.readout = QLabel()
        self.readout.setObjectName("muted")
        self.readout.setFixedWidth(70)
        self.readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider.valueChanged.connect(self._update)
        layout.addWidget(name)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.readout)
        self._update(value)

    def _update(self, value):
        self.readout.setText(f"{value}{self._unit}")

    def value(self):
        return self.slider.value()

    def set_value(self, value):
        self.slider.setValue(int(value))

    def reset(self):
        self.slider.setValue(self._default)


def divider():
    line = QFrame()
    line.setObjectName("divider")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 720)
        self.setMinimumSize(860, 640)

        self.settings = QSettings(APP_NAME, APP_NAME)
        self.mic = VirtualMic()
        self.engine = SpeechEngine(self)
        self.engine.status.connect(lambda text: self.set_status(text, "busy"))
        self.engine.finished.connect(self.on_engine_finished)

        self._build_ui()
        self._load_settings()
        QTimer.singleShot(0, self.setup_mic)

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.on_speak)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.on_speak)
        QShortcut(QKeySequence("Escape"), self, activated=self.on_stop)

    # -- layout ------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        main = QVBoxLayout()
        main.setContentsMargins(28, 24, 28, 20)
        main.setSpacing(16)
        outer.addLayout(main, 1)

        # Header
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("Type it. Hear it. Send it to your calls and streams.")
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.mic_pill = QPushButton()
        self.mic_pill.setObjectName("link")
        self.mic_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic_pill.clicked.connect(self.setup_mic)
        header.addWidget(self.mic_pill, 0, Qt.AlignmentFlag.AlignTop)
        main.addLayout(header)

        # Text + wave
        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(16, 16, 16, 12)
        pl.setSpacing(10)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Type what you want to say…")
        self.editor.textChanged.connect(self._on_text_changed)
        pl.addWidget(self.editor, 1)
        self.wave = WaveStrip()
        pl.addWidget(self.wave)
        self.counter = QLabel()
        self.counter.setObjectName("muted")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        pl.addWidget(self.counter)
        main.addWidget(panel, 1)

        # Voice settings
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        self.voice_box = QComboBox()
        for lang, label in load_voices():
            self.voice_box.addItem(label, lang)
        self.variant_box = QComboBox()
        for label, code in VARIANTS:
            self.variant_box.addItem(label, code)
        grid.addWidget(QLabel("Language"), 0, 0)
        grid.addWidget(self.voice_box, 0, 1)
        grid.addWidget(QLabel("Voice"), 0, 2)
        grid.addWidget(self.variant_box, 0, 3)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        main.addLayout(grid)

        self.speed = SliderRow("Speed", 80, 300, DEFAULTS["speed"], " wpm")
        self.pitch = SliderRow("Pitch", 0, 99, DEFAULTS["pitch"])
        self.volume = SliderRow("Volume", 0, 200, DEFAULTS["volume"], "%")
        for row in (self.speed, self.pitch, self.volume):
            main.addWidget(row)

        main.addWidget(divider())

        # Outputs
        outputs = QHBoxLayout()
        outputs.setSpacing(24)
        self.chk_local = QCheckBox("Play on my speakers")
        self.chk_virtual = QCheckBox("Send to virtual microphone")
        self.chk_cleanup = QCheckBox("Remove virtual mic on exit")
        self.chk_cleanup.setToolTip("Only removes devices that VexoTTS created itself.")
        for chk in (self.chk_local, self.chk_virtual, self.chk_cleanup):
            outputs.addWidget(chk)
        outputs.addStretch(1)
        reset = QPushButton("Reset voice")
        reset.setObjectName("link")
        reset.clicked.connect(self.reset_voice)
        outputs.addWidget(reset)
        main.addLayout(outputs)

        # Actions + status
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.speak_btn = QPushButton("Speak")
        self.speak_btn.setObjectName("speak")
        self.speak_btn.setToolTip("Ctrl+Enter")
        self.speak_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speak_btn.clicked.connect(self.on_speak)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setToolTip("Esc")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        actions.addWidget(self.speak_btn)
        actions.addWidget(self.stop_btn)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        actions.addSpacing(8)
        actions.addWidget(self.status_label, 1)
        main.addLayout(actions)
        self.set_status("Ready.", "ok")

        # Sidebar: history
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(250)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 22, 14, 16)
        sl.setSpacing(8)
        top = QHBoxLayout()
        heading = QLabel("Recent")
        heading.setObjectName("section")
        top.addWidget(heading, 1)
        clear = QPushButton("Clear")
        clear.setObjectName("link")
        clear.clicked.connect(self.clear_history)
        top.addWidget(clear)
        sl.addLayout(top)
        self.history = QListWidget()
        self.history.setWordWrap(False)
        self.history.itemClicked.connect(self.load_history_item)
        sl.addWidget(self.history, 1)
        self.history_hint = QLabel("Phrases you speak will show up here.")
        self.history_hint.setObjectName("muted")
        self.history_hint.setWordWrap(True)
        sl.addWidget(self.history_hint)
        outer.addWidget(side)

    # -- state helpers -------------------------------------------------------
    def set_status(self, text, level="info"):
        color = {"ok": TEAL, "busy": AMBER, "err": RED, "warn": AMBER}.get(level, MUTED)
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setText(text)

    def set_busy(self, busy):
        self.speak_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.wave.set_active(busy)

    def _on_text_changed(self):
        text = self.editor.toPlainText()
        if len(text) > MAX_CHARS:
            self.editor.blockSignals(True)
            self.editor.setPlainText(text[:MAX_CHARS])
            self.editor.moveCursor(QTextCursor.MoveOperation.End)
            self.editor.blockSignals(False)
            text = text[:MAX_CHARS]
        self.counter.setText(f"{len(text)} / {MAX_CHARS}")

    def refresh_mic_pill(self):
        if self.mic.is_ready():
            self.mic_pill.setText("●  Virtual mic ready")
            self.mic_pill.setStyleSheet(f"color: {TEAL};")
            self.mic_pill.setToolTip(f"In Discord, OBS or your browser, choose “{MIC_LABEL}” as the microphone.")
        else:
            self.mic_pill.setText("●  Virtual mic off – click to set up")
            self.mic_pill.setStyleSheet(f"color: {RED};")
            self.mic_pill.setToolTip("Creates the virtual microphone.")

    def setup_mic(self):
        try:
            self.mic.ensure()
        except AudioError as exc:
            self.set_status("Virtual mic setup failed.", "err")
            QMessageBox.warning(self, APP_NAME, str(exc))
        self.refresh_mic_pill()

    # -- actions -------------------------------------------------------------
    def on_speak(self):
        if self.engine.busy:
            return
        text = self.editor.toPlainText().strip()
        if not text:
            self.set_status("Type something first.", "warn")
            return
        to_local, to_virtual = self.chk_local.isChecked(), self.chk_virtual.isChecked()
        if not (to_local or to_virtual):
            self.set_status("Turn on at least one output.", "warn")
            return
        if to_virtual:
            try:
                self.mic.ensure()
            except AudioError as exc:
                self.set_status("Virtual mic setup failed.", "err")
                QMessageBox.warning(self, APP_NAME, str(exc))
                return
            finally:
                self.refresh_mic_pill()

        self.add_history(text)
        voice = self.voice_box.currentData()
        variant = self.variant_box.currentData()
        if variant:
            voice = f"{voice}+{variant}"
        self.set_busy(True)
        self.engine.speak(
            text, voice, self.speed.value(), self.pitch.value(), self.volume.value(), to_local, to_virtual
        )

    def on_stop(self):
        if self.engine.busy:
            self.engine.stop()

    def on_engine_finished(self, kind, message):
        self.set_busy(False)
        if kind == "ok":
            self.set_status(message, "ok")
        elif kind == "stopped":
            self.set_status(message, "info")
        else:
            self.set_status("Something went wrong.", "err")
            QMessageBox.warning(self, APP_NAME, message)

    def reset_voice(self):
        for row in (self.speed, self.pitch, self.volume):
            row.reset()
        self.variant_box.setCurrentIndex(0)

    # -- history -------------------------------------------------------------
    def add_history(self, text):
        for i in range(self.history.count()):
            if self.history.item(i).data(Qt.ItemDataRole.UserRole) == text:
                self.history.takeItem(i)
                break
        self._insert_history(0, text)
        while self.history.count() > MAX_HISTORY:
            self.history.takeItem(self.history.count() - 1)
        self.history_hint.setVisible(self.history.count() == 0)

    def _insert_history(self, index, text):
        item = QListWidgetItem(" ".join(text.split()))
        item.setData(Qt.ItemDataRole.UserRole, text)
        item.setToolTip(text[:400])
        self.history.insertItem(index, item)

    def load_history_item(self, item):
        self.editor.setPlainText(item.data(Qt.ItemDataRole.UserRole))

    def clear_history(self):
        self.history.clear()
        self.history_hint.setVisible(True)

    # -- persistence -----------------------------------------------------------
    def _load_settings(self):
        s = self.settings
        self.editor.setPlainText(s.value("text", "Hello everyone! This is VexoTTS."))
        self._select(self.voice_box, s.value("voice", "en-us"))
        self._select(self.variant_box, s.value("variant", "m3"))
        self.speed.set_value(int(s.value("speed", DEFAULTS["speed"])))
        self.pitch.set_value(int(s.value("pitch", DEFAULTS["pitch"])))
        self.volume.set_value(int(s.value("volume", DEFAULTS["volume"])))
        self.chk_local.setChecked(s.value("local", True, type=bool))
        self.chk_virtual.setChecked(s.value("virtual", True, type=bool))
        self.chk_cleanup.setChecked(s.value("cleanup", True, type=bool))
        saved = s.value("history", []) or []
        if isinstance(saved, str):
            saved = [saved]
        for text in saved[:MAX_HISTORY]:
            self._insert_history(self.history.count(), text)
        self.history_hint.setVisible(self.history.count() == 0)
        geometry = s.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    @staticmethod
    def _select(combo, data):
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _save_settings(self):
        s = self.settings
        s.setValue("text", self.editor.toPlainText())
        s.setValue("voice", self.voice_box.currentData())
        s.setValue("variant", self.variant_box.currentData())
        s.setValue("speed", self.speed.value())
        s.setValue("pitch", self.pitch.value())
        s.setValue("volume", self.volume.value())
        s.setValue("local", self.chk_local.isChecked())
        s.setValue("virtual", self.chk_virtual.isChecked())
        s.setValue("cleanup", self.chk_cleanup.isChecked())
        s.setValue(
            "history",
            [self.history.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.history.count())],
        )
        s.setValue("geometry", self.saveGeometry())

    def closeEvent(self, event):
        self._save_settings()
        self.engine.shutdown()
        if self.chk_cleanup.isChecked():
            self.mic.teardown()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    missing = [p for p in ("espeak-ng", "paplay", "pactl") if not shutil.which(p)]
    if missing:
        QMessageBox.critical(
            None,
            APP_NAME,
            f"Missing required program(s): {', '.join(missing)}\n\n"
            "Install them with:\nsudo apt install espeak-ng pulseaudio-utils",
        )
        return 1

    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
