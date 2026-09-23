"""A real Windows console pane.

cmd.exe runs under a ConPTY pseudo-console (pywinpty). Its output is fed to a
pyte terminal emulator, and the emulator's screen buffer is painted cell by
cell. That is why it behaves like the actual command prompt: it *is* cmd.exe,
including cursor movement, colours, doskey history and Ctrl+C.
"""

import os
import re
import sys
import time
from collections import deque

from PySide6.QtCore import QEvent, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetricsF, QGuiApplication, QPainter,
)
from PySide6.QtWidgets import QMenu, QMessageBox, QScrollBar, QWidget

SCROLLBAR_W = 12

# Escape sequences and control bytes, minus tab/newline/carriage return.
ANSI_RE = re.compile(
    r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][A-Za-z0-9]"                   # charset select
    r"|\x1b[=>NOM78]"                         # single-char escapes
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # stray control bytes

# A trailing shell prompt. Kept narrow on purpose: a loose "ends in > or %"
# rule fires on progress bars ("100%") and HTML, which made a still-running
# command look finished. Custom prompts are also matched exactly against the
# prompt that was on screen when the command started (see _begin_capture).
PROMPT_RE = re.compile(r"""
      ^[A-Za-z]:\\[^>]*>\s*$                        # cmd.exe     C:\Users\me>
    | ^PS\s.*>\s*$                                  # PowerShell  PS C:\x>
    | ^(\([^)]*\)\s*)?\[?[\w.-]+@[\w.-]+[^\n]*[$#%]\s*$   # user@host:~$  [root@h x]#
    | ^\S{0,40}[$#]\s*$                             # bare  $  #  bash-5.1$
    """, re.X)

# Output that is asking a question rather than finishing.
WAITING_RE = re.compile(
    r"(password[^:]*:|passphrase[^:]*:|\[y/n\]|\(yes/no[^)]*\)|"
    r"\[Y/n\]|\[y/N\]|continue\?|press any key|more\s*--)\s*$", re.I)

class _CountingDeque(deque):
    """A deque that remembers how many items were ever appended, even ones
    since evicted by maxlen. Used to give scrollback lines a stable, ever-
    increasing position so a capture can resume where the last one left off,
    instead of only starting over from what's on screen right now."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.total_appended = len(self)

    def append(self, item):
        super().append(item)
        self.total_appended += 1


try:
    import pyte
except ImportError:  # pragma: no cover
    pyte = None

try:
    from winpty import PtyProcess
except ImportError:  # pragma: no cover
    PtyProcess = None


# Classic Windows console palette (the "campbell" scheme cmd.exe uses today).
PALETTE = {
    "black": "#0c0c0c", "red": "#c50f1f", "green": "#13a10e",
    "brown": "#c19c00", "yellow": "#c19c00", "blue": "#0037da",
    "magenta": "#881798", "cyan": "#3a96dd", "white": "#cccccc",
    "brightblack": "#767676", "brightred": "#e74856",
    "brightgreen": "#16c60c", "brightbrown": "#f9f1a5",
    "brightyellow": "#f9f1a5", "brightblue": "#3b78ff",
    "brightmagenta": "#b4009e", "brightcyan": "#61d6d6",
    "brightwhite": "#f2f2f2",
}
DEFAULT_FG = QColor("#cccccc")
DEFAULT_BG = QColor("#0c0c0c")
SELECTION_BG = QColor(58, 150, 221, 110)


def _color(name, default):
    if not name or name == "default":
        return default
    c = PALETTE.get(name)
    if c:
        return QColor(c)
    if len(name) == 6:  # pyte hands back bare hex for 256/true colour
        try:
            return QColor("#" + name)
        except ValueError:
            return default
    return default


class _Reader(QThread):
    """Blocking reads off the pty, handed back to the GUI thread."""

    data = Signal(str)
    closed = Signal()

    def __init__(self, proc, parent=None):
        super().__init__(parent)
        self.proc = proc
        self._run = True

    def stop(self):
        self._run = False

    def run(self):
        while self._run:
            try:
                text = self.proc.read(4096)
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - pty torn down
                break
            if text:
                self.data.emit(text)
            elif not self.proc.isalive():
                break
        self.closed.emit()


class TerminalWidget(QWidget):
    """Paints the emulator screen and forwards keystrokes to the shell."""

    session_ended = Signal()
    title_changed = Signal(str)
    notice = Signal(str)
    # command, cleaned output, status ("ok" | "waiting" | "timeout")
    command_output = Signal(str, str, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.proc = None
        self.reader = None
        self.screen = None
        self.stream = None
        self.cols, self.rows = 100, 30
        self.scroll_offset = 0
        self.sel_anchor = None
        self.sel_head = None
        self.cursor_visible = True
        self.error_text = ""
        self._capture_mark = 0  # scrollback position of the previous "Include
                                 # terminal screen" capture; see capture_since_last

        self._autoscroll_dir = 0
        self._autoscroll_x = 0.0
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.timeout.connect(self._autoscroll_tick)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.IBeamCursor)
        # Right-click is handled by hand (copy-or-paste), so Qt must not
        # open a context menu of its own. Shift+right-click still shows one.
        self.setContextMenuPolicy(Qt.NoContextMenu)

        self._syncing = False
        self._capturing = False
        self._capture_cmd = ""
        self._capture_buf = []
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._check_capture)
        self._capture_prompt = ""
        self._last_data = 0.0
        self._capture_deadline = QTimer(self)
        self._capture_deadline.setSingleShot(True)
        self._capture_deadline.timeout.connect(
            lambda: self._finish_capture("timeout"))
        self.scrollbar = QScrollBar(Qt.Vertical, self)
        self.scrollbar.setFocusPolicy(Qt.NoFocus)
        self.scrollbar.setCursor(Qt.ArrowCursor)
        self.scrollbar.valueChanged.connect(self._on_scrollbar)

        self.apply_font(cfg.get("font_family", "Consolas"),
                        int(cfg.get("font_size", 11)))

        self.blink = QTimer(self)
        self.blink.timeout.connect(self._toggle_cursor)
        self.blink.start(530)

        self.repaint_timer = QTimer(self)
        self.repaint_timer.setSingleShot(True)
        self.repaint_timer.timeout.connect(self.update)

        self.start()

    # ------------------------------------------------------------ setup

    def apply_font(self, family: str, size: int):
        font = QFont(family, size)
        font.setStyleHint(QFont.Monospace)
        font.setFixedPitch(True)
        font.setKerning(False)
        self.setFont(font)
        fm = QFontMetricsF(font)
        self.cell_w = max(1.0, fm.horizontalAdvance("M"))
        self.cell_h = max(1.0, fm.height())
        self.ascent = fm.ascent()
        self._layout_scrollbar()
        self._resize_session()
        self.update()

    def start(self):
        missing = []
        if pyte is None:
            missing.append("pyte")
        if PtyProcess is None:
            missing.append("pywinpty")
        if missing:
            self.error_text = ("Missing package(s): " + ", ".join(missing) +
                               "\r\n\r\nInstall with:  pip install " +
                               " ".join(missing))
            self.update()
            return
        if not sys.platform.startswith("win"):
            self.error_text = ("The terminal pane needs Windows (ConPTY).\r\n"
                               "The chat pane still works on other systems.")
            self.update()
            return

        history = int(self.cfg.get("scrollback", 5000))
        self.screen = pyte.HistoryScreen(self.cols, self.rows,
                                         history=history, ratio=0.5)
        self.screen.history = self.screen.history._replace(
            top=_CountingDeque(maxlen=history))
        self._capture_mark = 0
        self.screen.set_mode(pyte.modes.LNM)
        self.screen.write_process_input = self._write_back
        self.stream = pyte.Stream(self.screen)

        shell = self.cfg.get("shell", "cmd.exe") or "cmd.exe"
        # Remote programs reached over SSH read TERM to decide what escape
        # sequences they may emit. Without it they fall back to something
        # crippled, which breaks colour and line editing on the far end.
        env = dict(os.environ)
        term = self.cfg.get("term", "xterm-256color")
        if term:
            env["TERM"] = term
        try:
            self.proc = PtyProcess.spawn(shell, env=env,
                                         dimensions=(self.rows, self.cols))
        except Exception as exc:  # noqa: BLE001
            self.error_text = f"Could not start {shell}:\r\n{exc}"
            self.update()
            return

        self.title_changed.emit(shell)
        self.reader = _Reader(self.proc, self)
        self.reader.data.connect(self._on_data)
        self.reader.closed.connect(self._on_closed)
        self.reader.start()
        self._layout_scrollbar()
        self._sync_scrollbar()

    def restart(self, cfg: dict = None):
        if cfg is not None:
            self.cfg = cfg
        self.close_session()
        self.error_text = ""
        self.scroll_offset = 0
        self.sel_anchor = self.sel_head = None
        self.apply_font(self.cfg.get("font_family", "Consolas"),
                        int(self.cfg.get("font_size", 11)))
        self.start()
        self._sync_scrollbar()
        self.update()

    def close_session(self):
        self._stop_autoscroll()
        self.cancel_capture()
        if self.reader:
            self.reader.stop()
        if self.proc is not None:
            try:
                self.proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        if self.reader:
            self.reader.wait(1000)
            self.reader = None

    # ------------------------------------------------------------ pty io

    def _write_back(self, data):
        """pyte answers device-status queries through this."""
        self.send(data)

    def send(self, text: str):
        if self.proc is None:
            return
        try:
            self.proc.write(text)
        except Exception:  # noqa: BLE001
            pass

    def run_command(self, command: str, capture: bool = False):
        """Type one or more lines into the shell and run them.

        A multi-line block is sent as a unit with CR separators, so constructs
        that span lines (for/done, if/fi, backslash continuations) reach the
        shell as the single thing they are.

        With capture set, everything the command prints is collected and
        emitted on command_output once the shell goes quiet.
        """
        self._scroll_to_bottom()
        text = command.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not text:
            return
        if capture:
            self._begin_capture(text)
        self.send(text.replace("\n", "\r") + "\r")
        self.setFocus()

    # -------------------------------------------------------- capture

    @property
    def busy(self) -> bool:
        """A command we started is still being watched."""
        return self._capturing

    def _cursor_line(self) -> str:
        if self.screen is None:
            return ""
        try:
            return self.screen.display[self.screen.cursor.y].rstrip()
        except (IndexError, AttributeError):
            return ""

    def _begin_capture(self, command: str):
        self._capturing = True
        self._capture_cmd = command
        self._capture_buf = []
        # Whatever sits on the cursor line right now is the shell's prompt.
        # Seeing it again later means the command has handed control back,
        # even for prompts PROMPT_RE does not know.
        self._capture_prompt = self._cursor_line().strip()
        self._last_data = time.monotonic()
        idle = int(self.cfg.get("capture_idle_ms", 900))
        limit = int(self.cfg.get("capture_timeout_s", 180)) * 1000
        self._idle_timer.start(idle)
        self._capture_deadline.start(limit)

    def cancel_capture(self):
        self._capturing = False
        self._idle_timer.stop()
        self._capture_deadline.stop()

    def _check_capture(self):
        """The shell went quiet. Decide whether the command is really done.

        Silence alone is not enough: package installs, downloads, ssh and
        builds all pause. Done means the prompt is back. A question on the
        last line means it wants input. Anything else keeps waiting, until
        the output has been silent for capture_stall_s with no prompt.
        """
        if not self._capturing:
            return
        raw = "".join(self._capture_buf)
        _, at_prompt, waiting = self._clean_output(
            raw, self._capture_cmd, self._capture_prompt)
        if at_prompt:
            self._finish_capture("ok")
            return
        if waiting:
            self._finish_capture("waiting")
            return
        stall = float(self.cfg.get("capture_stall_s", 30))
        if time.monotonic() - self._last_data >= stall:
            self._finish_capture("stalled")
            return
        self._idle_timer.start(int(self.cfg.get("capture_idle_ms", 900)))

    def _finish_capture(self, status: str):
        if not self._capturing:
            return
        self._capturing = False
        self._idle_timer.stop()
        self._capture_deadline.stop()

        raw = "".join(self._capture_buf)
        self._capture_buf = []
        output, at_prompt, waiting = self._clean_output(
            raw, self._capture_cmd, self._capture_prompt)
        if status == "ok" and waiting and not at_prompt:
            status = "waiting"
        self.command_output.emit(self._capture_cmd, output, status)

    @staticmethod
    def _clean_output(raw: str, command: str, prompt: str = ""):
        """Turn a raw pty stream into something worth showing a model."""
        text = ANSI_RE.sub("", raw).replace("\r\n", "\n")

        lines = []
        for line in text.split("\n"):
            if "\r" in line:
                line = line.split("\r")[-1]   # keep only the final redraw
            lines.append(line.rstrip())

        # Drop the shell's echo of what we just typed.
        for cmd_line in [c.strip() for c in command.splitlines() if c.strip()]:
            if lines and cmd_line[:40] in lines[0]:
                lines.pop(0)

        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        last = lines[-1].strip() if lines else ""
        at_prompt = bool(last and (PROMPT_RE.search(last)
                                   or (prompt and last == prompt)))
        if at_prompt:
            lines.pop()                        # trailing prompt is not output
            while lines and not lines[-1].strip():
                lines.pop()

        waiting = bool(lines and WAITING_RE.search(lines[-1]))

        # No trimming here: the chat pane decides how much reaches the model
        # (Settings → Chat → Output sent to AI) and tells it what was left out.
        # This cap only guards memory against a runaway command.
        out = "\n".join(lines)
        if len(out) > 2_000_000:
            out = out[-2_000_000:]
        return out, at_prompt, waiting

    def _on_data(self, text: str):
        if self.stream is None:
            return
        if self._capturing:
            self._capture_buf.append(text)
            self._last_data = time.monotonic()
            self._idle_timer.start(int(self.cfg.get("capture_idle_ms", 900)))
        before = self._max_offset()
        self.stream.feed(text)
        grown = self._max_offset() - before
        # Scrolled back? Stay on the same content while output rolls past.
        if self.scroll_offset and grown > 0:
            self.scroll_offset = min(self.scroll_offset + grown, self._max_offset())
        self._sync_scrollbar()
        if not self.repaint_timer.isActive():
            self.repaint_timer.start(16)

    def _on_closed(self):
        self.session_ended.emit()
        if not self.error_text:
            self.error_text = ""
        self.update()

    # ------------------------------------------------------------ sizing

    def resizeEvent(self, event):
        self._layout_scrollbar()
        self._resize_session()
        super().resizeEvent(event)

    def _resize_session(self):
        cols = max(20, int(self._term_width() / self.cell_w))
        rows = max(5, int(self.height() / self.cell_h))
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        if self.screen is not None:
            try:
                self.screen.resize(rows, cols)
            except Exception:  # noqa: BLE001 - pyte history edge cases
                pass
            self._sync_scrollbar()
        if self.proc is not None:
            try:
                self.proc.setwinsize(rows, cols)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ paint

    def _toggle_cursor(self):
        self.cursor_visible = not self.cursor_visible
        if self.hasFocus():
            self.update(self._cursor_rect())

    def _cursor_rect(self) -> QRect:
        if self.screen is None:
            return QRect()
        c = self.screen.cursor
        return QRect(int(c.x * self.cell_w), int(c.y * self.cell_h),
                     int(self.cell_w) + 1, int(self.cell_h) + 1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), DEFAULT_BG)
        painter.setFont(self.font())

        if self.error_text:
            painter.setPen(QColor("#e57373"))
            painter.drawText(self.rect().adjusted(12, 12, -12, -12),
                             Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap,
                             self.error_text.replace("\r\n", "\n"))
            return
        if self.screen is None:
            return

        sel = self._normalised_selection()
        top_logical = self._top_logical()

        for y in range(self.rows):
            line = self._line(y)
            if line is None:
                continue
            logical_y = top_logical + y
            x = 0
            while x < self.cols:
                cell = line[x]
                style = self._style(cell, sel, x, logical_y)
                run = [cell.data or " "]
                x2 = x + 1
                while x2 < self.cols:
                    nxt = line[x2]
                    if self._style(nxt, sel, x2, logical_y) != style:
                        break
                    run.append(nxt.data or " ")
                    x2 += 1

                fg, bg, bold, underline = style
                rect = QRect(int(x * self.cell_w), int(y * self.cell_h),
                             int((x2 - x) * self.cell_w) + 1,
                             int(self.cell_h) + 1)
                if bg != DEFAULT_BG:
                    painter.fillRect(rect, bg)
                text = "".join(run)
                if text.strip():
                    f = painter.font()
                    if f.bold() != bold or f.underline() != underline:
                        f.setBold(bold)
                        f.setUnderline(underline)
                        painter.setFont(f)
                    painter.setPen(fg)
                    painter.drawText(int(x * self.cell_w),
                                     int(y * self.cell_h + self.ascent), text)
                x = x2

        # Block cursor, only when live and scrolled to the bottom.
        if (self.hasFocus() and self.cursor_visible and not self.scroll_offset
                and not self.screen.cursor.hidden):
            r = self._cursor_rect()
            painter.fillRect(r, DEFAULT_FG)
            ch = self.screen.buffer[self.screen.cursor.y][self.screen.cursor.x].data
            if ch and ch.strip():
                painter.setPen(DEFAULT_BG)
                painter.drawText(r.left(), int(self.screen.cursor.y * self.cell_h
                                               + self.ascent), ch)

    def _style(self, cell, sel, x, y):
        fg = _color(cell.fg, DEFAULT_FG)
        bg = _color(cell.bg, DEFAULT_BG)
        if cell.reverse:
            fg, bg = bg, fg
        if cell.bold and cell.fg != "default":
            bright = PALETTE.get("bright" + cell.fg)
            if bright:
                fg = QColor(bright)
        if sel and self._in_selection(sel, x, y):
            bg = SELECTION_BG
        return fg, bg, bool(cell.bold), bool(cell.underscore)

    # ------------------------------------------------------------ input

    KEYMAP = {int(k): v for k, v in {
        Qt.Key_Return: "\r", Qt.Key_Enter: "\r",
        Qt.Key_Backspace: "\x08", Qt.Key_Tab: "\t",
        Qt.Key_Backtab: "\x1b[Z",
        Qt.Key_Escape: "\x1b",
        Qt.Key_Up: "\x1b[A", Qt.Key_Down: "\x1b[B",
        Qt.Key_Right: "\x1b[C", Qt.Key_Left: "\x1b[D",
        Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F",
        Qt.Key_Insert: "\x1b[2~", Qt.Key_Delete: "\x1b[3~",
        Qt.Key_PageUp: "\x1b[5~", Qt.Key_PageDown: "\x1b[6~",
        Qt.Key_F1: "\x1bOP", Qt.Key_F2: "\x1bOQ", Qt.Key_F3: "\x1bOR",
        Qt.Key_F4: "\x1bOS", Qt.Key_F5: "\x1b[15~", Qt.Key_F6: "\x1b[17~",
        Qt.Key_F7: "\x1b[18~", Qt.Key_F8: "\x1b[19~", Qt.Key_F9: "\x1b[20~",
        Qt.Key_F10: "\x1b[21~", Qt.Key_F11: "\x1b[23~", Qt.Key_F12: "\x1b[24~",
    }.items()}

    KEY_1, KEY_2 = int(Qt.Key_1), int(Qt.Key_2)
    KEY_COMMA, KEY_SPACE = int(Qt.Key_Comma), int(Qt.Key_Space)
    KEY_PGUP, KEY_PGDN = int(Qt.Key_PageUp), int(Qt.Key_PageDown)
    KEY_HOME, KEY_END = int(Qt.Key_Home), int(Qt.Key_End)
    KEY_A, KEY_Z = int(Qt.Key_A), int(Qt.Key_Z)
    KEY_C, KEY_V = int(Qt.Key_C), int(Qt.Key_V)
    ENTER_KEYS = (int(Qt.Key_Return), int(Qt.Key_Enter))

    def keyPressEvent(self, event):
        key = int(event.key())
        if self.proc is None:
            if key in self.ENTER_KEYS:
                self.restart()
            return

        mods = event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        alt = bool(mods & Qt.AltModifier)

        # Windows Terminal conventions.
        if ctrl and shift and key == self.KEY_C:
            self.copy_selection()
            return
        if ctrl and shift and key == self.KEY_V:
            self.paste()
            return
        if ctrl and key == self.KEY_C and self.has_selection():
            self.copy_selection()
            self.clear_selection()
            return
        if ctrl and key == self.KEY_V:
            self.paste()
            return

        # Scrollback navigation, matching Windows Terminal.
        if ctrl and shift:
            if key == self.KEY_PGUP:
                self.scroll_by(self.rows - 1)
                return
            if key == self.KEY_PGDN:
                self.scroll_by(-(self.rows - 1))
                return
            if key == self.KEY_HOME:
                self.scroll_to_top()
                return
            if key == self.KEY_END:
                self._scroll_to_bottom()
                return

        # Work out what this key sends, if anything.
        if key in self.KEYMAP:
            payload = self.KEYMAP[key]
        elif ctrl and self.KEY_A <= key <= self.KEY_Z:
            payload = chr(key - self.KEY_A + 1)
        elif alt and not ctrl:
            # readline reads Meta as an ESC prefix: Alt+F is ESC f.
            ch = event.text()
            if not ch and self.KEY_SPACE <= key <= 0x7E:
                ch = chr(key) if shift else chr(key).lower()
            if not ch:
                return
            payload = "\x1b" + ch
        elif event.text():
            payload = event.text()
        else:
            # Bare modifiers, F13+, media keys and the like send nothing, so
            # they must not disturb the scrollback position either.
            return

        if self._capturing:
            self.cancel_capture()
            self.notice.emit("You took over — the assistant stopped watching "
                             "this command")

        # Typing snaps back to the live prompt.
        if self.scroll_offset:
            self._scroll_to_bottom()
        self.send(payload)

    def paste(self):
        text = QGuiApplication.clipboard().text()
        if not text:
            self.notice.emit("Clipboard is empty")
            return
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) > 1 and self.cfg.get("warn_multiline_paste", True):
            preview = "\n".join(lines[:6])
            if len(lines) > 6:
                preview += f"\n… and {len(lines) - 6} more"
            confirm = QMessageBox.question(
                self, "Paste multiple lines?",
                f"This pastes {len(lines)} lines. Every line break runs a "
                f"command straight away.\n\n{preview}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if confirm != QMessageBox.Yes:
                return
        self._scroll_to_bottom()
        self.send(text.replace("\r\n", "\r").replace("\n", "\r"))
        self.notice.emit("Pasted")

    # ------------------------------------------------------------ scroll

    def _max_offset(self) -> int:
        """How many lines of scrollback sit above the live screen."""
        if self.screen is None:
            return 0
        return len(self.screen.history.top)

    def _top_logical(self) -> int:
        """Logical line number currently shown at viewport row 0.

        Logical numbers count every line the session has ever produced and
        never shift under a selection: line N is always the same piece of
        text, no matter how far the view has since scrolled. Viewport row y
        is line `_top_logical() + y`.
        """
        if self.screen is None:
            return 0
        top = self.screen.history.top
        total = getattr(top, "total_appended", len(top))
        return total - self.scroll_offset

    def _row_by_logical(self, logical_row: int):
        """Cell row for a logical line number, independent of scroll position.

        Used for selection so a drag stays anchored to the actual text even
        after the view has scrolled — unlike viewport row lookups, which
        answer 'what's on screen right now' rather than 'where did the user
        click'.
        """
        if self.screen is None:
            return None
        top = self.screen.history.top
        total = getattr(top, "total_appended", len(top))
        retained_start = total - len(top)
        if logical_row < retained_start:
            return None  # scrolled out of scrollback entirely
        if logical_row < total:
            return top[logical_row - retained_start]
        r = logical_row - total
        if 0 <= r < self.rows:
            return self.screen.buffer[r]
        return None

    def _line(self, y: int):
        """Row y of the visible viewport, reaching into history when scrolled."""
        if self.screen is None:
            return None
        if self.scroll_offset:
            top = self.screen.history.top
            idx = len(top) - self.scroll_offset + y
            if 0 <= idx < len(top):
                return top[idx]
            y -= self.scroll_offset
            if y < 0:
                return None
        return self.screen.buffer[y]

    def scroll_by(self, lines: int):
        """Positive scrolls back into history, negative returns toward the prompt."""
        target = self.scroll_offset + lines
        self.scroll_offset = max(0, min(self._max_offset(), target))
        self._sync_scrollbar()
        self.update()

    def scroll_to_top(self):
        self.scroll_offset = self._max_offset()
        self._sync_scrollbar()
        self.update()

    def _scroll_to_bottom(self):
        if self.scroll_offset:
            self.scroll_offset = 0
            self._sync_scrollbar()
            self.update()

    def wheelEvent(self, event):
        if self.screen is None:
            return
        steps = event.angleDelta().y() / 120.0
        self.scroll_by(int(round(steps * 3)))

    def _on_scrollbar(self, value: int):
        if self._syncing:
            return
        self.scroll_offset = max(0, self.scrollbar.maximum() - value)
        self.update()

    def _sync_scrollbar(self):
        max_off = self._max_offset()
        if self.scroll_offset > max_off:
            self.scroll_offset = max_off
        self._syncing = True
        self.scrollbar.setRange(0, max_off)
        self.scrollbar.setSingleStep(1)
        self.scrollbar.setPageStep(max(1, self.rows))
        self.scrollbar.setValue(max_off - self.scroll_offset)
        self._syncing = False

    def _layout_scrollbar(self):
        self.scrollbar.setGeometry(self.width() - SCROLLBAR_W, 0,
                                   SCROLLBAR_W, self.height())

    def _term_width(self) -> int:
        return max(40, self.width() - SCROLLBAR_W)

    # --------------------------------------------------------- selection

    def _cell_at(self, pos):
        x = min(self.cols - 1, max(0, int(pos.x() / self.cell_w)))
        x = min(x, self.cols - 1)
        y = min(self.rows - 1, max(0, int(pos.y() / self.cell_h)))
        return x, y

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._right_click(event)
            return
        if event.button() == Qt.MiddleButton:
            self.paste()
            self.setFocus()
            return
        if event.button() == Qt.LeftButton:
            x, y = self._cell_at(event.position())
            self.sel_anchor = self.sel_head = (x, self._top_logical() + y)
            self.update()
        self.setFocus()

    def _right_click(self, event):
        """conhost QuickEdit behaviour: copy if something is selected, else paste."""
        self.setFocus()
        if event.modifiers() & Qt.ShiftModifier:
            self._context_menu(event.position().toPoint())
            return
        if self.has_selection():
            self.copy_selection()
            self.clear_selection()
        else:
            self.paste()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self.sel_anchor:
            pos = event.position()
            self._autoscroll_x = pos.x()
            direction = self._drag_scroll_direction(pos.y())
            if direction:
                self._start_autoscroll(direction)
            else:
                self._stop_autoscroll()
                x, y = self._cell_at(pos)
                self.sel_head = (x, self._top_logical() + y)
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._stop_autoscroll()

    def _drag_scroll_direction(self, y: float) -> int:
        """+1 scrolls back into history, -1 toward the prompt, 0 stays put."""
        if y < 0:
            return 1
        if y > self.height():
            return -1
        return 0

    AUTOSCROLL_INTERVAL_MS = 50

    def _start_autoscroll(self, direction: int):
        self._autoscroll_dir = direction
        if not self._autoscroll_timer.isActive():
            self._autoscroll_timer.start(self.AUTOSCROLL_INTERVAL_MS)

    def _stop_autoscroll(self):
        self._autoscroll_timer.stop()
        self._autoscroll_dir = 0

    def _autoscroll_tick(self):
        if self.sel_anchor is None or self._autoscroll_dir == 0:
            self._stop_autoscroll()
            return
        before = self.scroll_offset
        self.scroll_by(self._autoscroll_dir)
        x = min(self.cols - 1, max(0, int(self._autoscroll_x / self.cell_w)))
        row = 0 if self._autoscroll_dir > 0 else self.rows - 1
        self.sel_head = (x, self._top_logical() + row)
        self.update()
        if self.scroll_offset == before:
            self._stop_autoscroll()  # hit the top of history or the live prompt

    def mouseDoubleClickEvent(self, event):
        x, y = self._cell_at(event.position())
        line = self._line(y)
        if line is None:
            return
        start = end = x
        while start > 0 and (line[start - 1].data or " ").strip():
            start -= 1
        while end < self.cols - 1 and (line[end + 1].data or " ").strip():
            end += 1
        self.sel_anchor, self.sel_head = ((start, self._top_logical() + y),
                                          (end, self._top_logical() + y))
        self.update()

    def has_selection(self):
        return self.sel_anchor is not None and self.sel_anchor != self.sel_head

    def clear_selection(self):
        self.sel_anchor = self.sel_head = None
        self.update()

    def _normalised_selection(self):
        if not self.has_selection():
            return None
        a, b = self.sel_anchor, self.sel_head
        if (a[1], a[0]) > (b[1], b[0]):
            a, b = b, a
        return a, b

    @staticmethod
    def _in_selection(sel, x, y):
        (ax, ay), (bx, by) = sel
        if y < ay or y > by:
            return False
        if ay == by:
            return ax <= x <= bx
        if y == ay:
            return x >= ax
        if y == by:
            return x <= bx
        return True

    def selected_text(self) -> str:
        sel = self._normalised_selection()
        if not sel or self.screen is None:
            return ""
        (ax, ay), (bx, by) = sel
        out = []
        for y in range(ay, by + 1):
            line = self._row_by_logical(y)
            if line is None:
                continue
            first = ax if y == ay else 0
            last = bx if y == by else self.cols - 1
            out.append("".join(line[x].data or " "
                               for x in range(first, last + 1)).rstrip())
        return "\n".join(out)

    def copy_selection(self):
        text = self.selected_text()
        if not text:
            return False
        QGuiApplication.clipboard().setText(text)
        lines = text.count("\n") + 1
        self.notice.emit(f"Copied {lines} line{'s' if lines > 1 else ''}")
        return True


    def _row_text(self, row) -> str:
        return "".join((row[x].data or " ") for x in range(self.cols)).rstrip()

    def capture_since_last(self) -> str:
        """Terminal output for 'Include terminal screen'.

        Unlike screen_text() (just the current viewport), this returns
        everything since the previous capture — including lines that have
        scrolled out of view — plus what's live on screen now. The position
        is remembered on this widget, so a second capture picks up where the
        first one left off instead of re-grabbing whatever happens to be
        visible at that moment. The very first capture in a session returns
        everything currently retained in scrollback.
        """
        if self.screen is None:
            return ""

        top = self.screen.history.top
        total = getattr(top, "total_appended", len(top))
        retained_start = total - len(top)
        start = max(self._capture_mark, retained_start)
        dropped = max(0, retained_start - self._capture_mark)

        lines = []
        if start < total:
            for row in list(top)[start - retained_start:]:
                lines.append(self._row_text(row))
        for y in range(self.rows):
            lines.append(self._row_text(self.screen.buffer[y]))

        while lines and not lines[-1].strip():
            lines.pop()

        self._capture_mark = total

        if not lines:
            return ""

        # Sized for the model by the chat pane, which knows the limit.
        text = "\n".join(lines)

        if dropped:
            note = (f"(…{dropped} earlier line{'s' if dropped != 1 else ''} scrolled "
                    "out of scrollback since the last capture and could not be "
                    "included…)\n")
            text = note + text
        return text

    def screen_text(self) -> str:
        """What the user is currently looking at — used by 'Ask about output'."""
        if self.screen is None:
            return ""
        if not self.scroll_offset:
            return "\n".join(ln.rstrip() for ln in self.screen.display).strip()
        rows = []
        for y in range(self.rows):
            line = self._line(y)
            if line is None:
                continue
            rows.append("".join(line[x].data or " "
                                for x in range(self.cols)).rstrip())
        return "\n".join(rows).strip()

    def _context_menu(self, pos):
        menu = QMenu(self)
        copy = QAction("Copy\tCtrl+Shift+C", self)
        copy.setEnabled(self.has_selection())
        copy.triggered.connect(self.copy_selection)
        paste = QAction("Paste\tCtrl+Shift+V", self)
        paste.triggered.connect(self.paste)
        clear = QAction("Clear screen", self)
        clear.triggered.connect(lambda: self.run_command("cls"))
        restart = QAction("Restart shell", self)
        restart.triggered.connect(self.restart)
        menu.addAction(copy)
        menu.addAction(paste)
        menu.addSeparator()
        menu.addAction(clear)
        menu.addAction(restart)
        menu.exec(self.mapToGlobal(pos))

    def focusNextPrevChild(self, next_child):
        """Tab belongs to the shell, not to Qt's focus chain."""
        return False

    def event(self, e):
        """Give the shell first claim on key combinations.

        Qt resolves menu shortcuts and mnemonics before delivering a key to the
        focused widget, which would swallow readline bindings like Ctrl+E and
        Alt+F. Accepting the ShortcutOverride hands the key back to us. The
        app keeps Ctrl+Shift+..., Ctrl+1/2 and Ctrl+, for itself; readline does
        not use those.
        """
        if e.type() == QEvent.ShortcutOverride and self.proc is not None:
            mods = e.modifiers()
            ctrl = bool(mods & Qt.ControlModifier)
            alt = bool(mods & Qt.AltModifier)
            shift = bool(mods & Qt.ShiftModifier)
            key = int(e.key())
            app_owns = (ctrl and shift) or (
                ctrl and key in (self.KEY_1, self.KEY_2, self.KEY_COMMA))
            if (ctrl or alt) and not app_owns:
                e.accept()
                return True
        return super().event(e)

    def focusInEvent(self, event):
        self.cursor_visible = True
        self.update()
        super().focusInEvent(event)

    def closeEvent(self, event):
        self.close_session()
        super().closeEvent(event)
