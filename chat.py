"""The right-hand pane: conversation, composer, and the bridge to the shell."""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from ai_client import ChatWorker

CODE_BLOCK = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.DOTALL)

# Fence tags that mean "this is meant for a shell". Anything else (python,
# yaml, json, a sample of output) is never auto-run.
SHELL_LANGS = {
    "", "bash", "sh", "shell", "zsh", "fish", "console", "shell-session",
    "shellsession", "cmd", "bat", "batch", "dos", "powershell", "ps", "ps1",
    "terminal", "text",
}

# Commands that are hard to undo. These always stop and ask, even with
# auto-run armed. Not a security boundary — a determined model could phrase
# something past it — just a brake on the obvious ways to lose a machine.
DESTRUCTIVE = [re.compile(p, re.I) for p in (
    r"\brm\s+(-\S+\s+)*-\S*[rf]\S*\s+(/|~|\*|\$HOME|/\*)",   # rm -rf / ~ *
    r"\bmkfs(\.\w+)?\b",                                      # formatting
    r"\bdd\b[^\n]*\bof=/dev/",                                # raw disk write
    r">\s*/dev/(sd|nvme|hd|vd)",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+[06]\b",
    r":\(\)\s*\{.*\}\s*;?\s*:",                               # fork bomb
    r"\bchmod\s+(-\S+\s+)*777\s+/(\s|$)",
    r"\bchown\s+-R\s+\S+\s+/(\s|$)",
    r"\b(userdel|groupdel)\b",
    r"\bufw\s+(--force\s+)?(reset|disable)\b",                # locking yourself out
    r"\biptables\s+-F\b",
    r"\bcurl\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b",               # pipe to shell
    r"\bwget\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b",
    r"\b(format|diskpart)\b",
    r"\b(del|rd|rmdir)\s+/[sq]",                              # Windows recursive delete
    r"\bdrop\s+(database|table)\b",
    r"\bgit\s+(reset\s+--hard|clean\s+-\S*f)",
    r"\b:>\s*/",
)]


def is_destructive(block: str) -> bool:
    return any(pattern.search(block) for pattern in DESTRUCTIVE)


# Markers that make a block a single script rather than a list of commands.
# Any of these means the lines depend on each other and must run together.
SCRIPT_MARKERS = re.compile(r"""
      ^\s*(for|while|until|if|case|select|function)\b   # block openers
    | ^\s*\w[\w-]*\s*\(\)\s*\{?                         # name() {  function def
    | ^\s*(do|then|else|elif|fi|done|esac)\b            # block bodies
    | ^\s*[{}]\s*$                                      # brace group
    | \\\s*$                                            # line continuation
    | \^\s*$                                            # cmd.exe continuation
    | `\s*$                                             # PowerShell continuation
    | <<-?\s*['\"]?\w+                                  # heredoc
    | (\||&&|\|\||;)\s*$                                # trailing operator
    """, re.M | re.X | re.I)


def is_script(block: str) -> bool:
    """True when the lines belong together; False for independent commands."""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return False
    if SCRIPT_MARKERS.search(block):
        return True
    # An odd quote count means the statement carries on to the next line.
    for line in lines:
        bare = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if bare.count('"') % 2 or bare.count("'") % 2:
            return True
    return False


class QuickEditView(QTextBrowser):
    """Read-only transcript. Right-click copies a selection, else asks to paste."""

    notice = Signal(str)
    paste_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.NoContextMenu)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            if event.modifiers() & Qt.ShiftModifier:
                self._menu(event)
                return
            cursor = self.textCursor()
            if cursor.hasSelection():
                text = cursor.selectedText().replace("\u2029", "\n")
                QGuiApplication.clipboard().setText(text)
                lines = text.count("\n") + 1
                self.notice.emit(f"Copied {lines} line{'s' if lines > 1 else ''}")
                cursor.clearSelection()
                self.setTextCursor(cursor)
            else:
                self.paste_requested.emit()
            return
        if event.button() == Qt.MiddleButton:
            self.paste_requested.emit()
            return
        super().mousePressEvent(event)

    def _menu(self, event):
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPosition().toPoint())


class QuickEditInput(QPlainTextEdit):
    """Composer with the same one-button copy-or-paste rule."""

    notice = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.NoContextMenu)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.setFocus()
            if event.modifiers() & Qt.ShiftModifier:
                self.createStandardContextMenu().exec(
                    event.globalPosition().toPoint())
                return
            cursor = self.textCursor()
            if cursor.hasSelection():
                text = cursor.selectedText().replace("\u2029", "\n")
                QGuiApplication.clipboard().setText(text)
                lines = text.count("\n") + 1
                self.notice.emit(f"Copied {lines} line{'s' if lines > 1 else ''}")
                cursor.clearSelection()
                self.setTextCursor(cursor)
            elif QGuiApplication.clipboard().text():
                self.paste()
                self.notice.emit("Pasted")
            else:
                self.notice.emit("Clipboard is empty")
            return
        if event.button() == Qt.MiddleButton:
            self.setFocus()
            self.paste()
            return
        super().mousePressEvent(event)


class ChatPanel(QWidget):
    """Holds the transcript, sends requests, and can hand commands to the shell."""

    run_in_terminal = Signal(str, bool)
    open_settings = Signal()
    notice = Signal(str)

    def __init__(self, cfg: dict, terminal, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.terminal = terminal
        self.messages = []        # [{role, content}] sent to the API
        self.worker = None
        self.streaming = False

        # --- header ------------------------------------------------------
        self.model_label = QLabel()
        self.model_label.setStyleSheet("color:#9aa0a6;")
        settings_btn = QPushButton("Settings")
        settings_btn.setFixedWidth(80)
        settings_btn.clicked.connect(self.open_settings.emit)
        new_btn = QPushButton("New chat")
        new_btn.setFixedWidth(90)
        new_btn.clicked.connect(self.new_chat)

        header = QHBoxLayout()
        header.addWidget(QLabel("Assistant"))
        header.addWidget(self.model_label, 1)
        header.addWidget(new_btn)
        header.addWidget(settings_btn)

        # --- transcript --------------------------------------------------
        self.view = QuickEditView()
        self.view.setOpenExternalLinks(True)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setStyleSheet(
            "QTextBrowser{background:#1b1b1f;color:#e6e6e6;border:1px solid #2e2e33;"
            "border-radius:6px;padding:8px;}")
        self.view.document().setDefaultStyleSheet(
            "code,pre{background:#111114;color:#9ee493;}"
            "h1,h2,h3{color:#ffffff;}")

        # --- code block runner -------------------------------------------
        self.code_picker = QComboBox()
        self.code_picker.setVisible(False)
        self.run_btn = QPushButton("Run in terminal")
        self.run_btn.setVisible(False)
        self.run_btn.clicked.connect(self._run_selected)
        self.check_results = QCheckBox("Check results")
        self.check_results.setChecked(True)
        self.check_results.setVisible(False)
        self.check_results.setToolTip(
            "After a command runs, send its output back so the assistant can "
            "confirm it worked before anything else happens.")
        self.auto_run = QCheckBox("Auto-run")
        self.auto_run.setVisible(False)
        self.auto_run.setToolTip(
            "Run the first code block of each reply without asking.\n"
            "You get a few seconds to cancel, and anything destructive still "
            "prompts. Resets when the app restarts.")
        self.auto_run.toggled.connect(self._auto_run_toggled)
        code_row = QHBoxLayout()
        code_row.addWidget(self.code_picker, 1)
        code_row.addWidget(self.check_results)
        code_row.addWidget(self.auto_run)
        code_row.addWidget(self.run_btn)

        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._auto_tick)
        self._countdown = 0
        self._auto_chain = 0
        self._last_run = ""

        # --- composer ------------------------------------------------------
        self.input = QuickEditInput()
        self.input.setPlaceholderText(
            "Ask anything — Enter to send, Shift+Enter for a new line")
        self.input.setFixedHeight(90)
        self.input.setStyleSheet(
            "QPlainTextEdit{background:#1b1b1f;color:#e6e6e6;border:1px solid #2e2e33;"
            "border-radius:6px;padding:6px;}")

        self.include_output = QCheckBox("Include terminal screen")
        self.include_output.setToolTip(
            "Attach terminal output to your next message: everything since the "
            "last time you attached it (or the full scrollback, the first time), "
            "not just what's currently visible.")
        self.send_btn = QPushButton("Send")
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self.send)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.stop)

        controls = QHBoxLayout()
        controls.addWidget(self.include_output)
        controls.addStretch(1)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.send_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        layout.addLayout(code_row)
        layout.addWidget(self.input)
        layout.addLayout(controls)

        self.view.notice.connect(self.notice.emit)
        self.input.notice.connect(self.notice.emit)
        self.view.paste_requested.connect(self._paste_into_input)

        QShortcut(QKeySequence("Return"), self.input,
                  context=Qt.WidgetShortcut, activated=self.send)
        QShortcut(QKeySequence("Ctrl+Return"), self.input,
                  context=Qt.WidgetShortcut, activated=self.send)

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._render)

        self.refresh_config(cfg)
        self.new_chat()

    # -------------------------------------------------------------- state

    def refresh_config(self, cfg: dict):
        self.cfg = cfg
        model = cfg.get("model") or "no model set"
        from config import PROVIDERS
        label = PROVIDERS.get(cfg.get("provider", ""), {}).get("label", "custom")
        self.model_label.setText(f"{label} · {model}")
        self.apply_font(int(cfg.get("chat_font_size", 10)))

    def apply_font(self, size: int):
        font = QFont(self.view.font())
        font.setPointSize(max(1, size))
        self.view.setFont(font)
        self.view.document().setDefaultFont(font)
        self.input.setFont(font)

    def new_chat(self):
        self.stop()
        self._cancel_auto_run()
        self._auto_chain = 0
        self.messages = []
        self._display_list = []
        self.code_picker.clear()
        self.code_picker.setVisible(False)
        self.run_btn.setVisible(False)
        self.view.setMarkdown(
            "Ask a question, or tick **Include terminal screen** to ask about "
            "what the shell just printed.\n\n"
            "Commands the assistant suggests in code blocks can be sent "
            "straight to the terminal.")
        self.input.setFocus()

    # -------------------------------------------------------------- send

    def send(self):
        if self.streaming:
            return
        self._cancel_auto_run()
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.cfg.get("model") or not self.cfg.get("base_url"):
            QMessageBox.information(
                self, "Set up a provider first",
                "Open Settings and choose an AI provider, then enter your API "
                "key and model name.")
            self.open_settings.emit()
            return

        display = text
        payload = text
        if self.include_output.isChecked():
            screen = self.terminal.capture_since_last() if self.terminal else ""
            if screen:
                payload = (f"{text}\n\nHere is the terminal output since it was "
                           f"last attached:\n```\n{screen}\n```")
                display = f"{text}\n\n*(terminal output attached)*"

        self.input.clear()
        self._auto_chain = 0          # a typed message starts a fresh chain
        self._dispatch(payload, display)

    def stop(self):
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(1500)
            self.worker = None
        self.streaming = False
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)

    def _on_chunk(self, text: str):
        role, current = self._display[-1]
        self._display[-1] = (role, current + text)
        if not self.render_timer.isActive():
            self.render_timer.start(90)

    def _on_done(self):
        role, content = self._display[-1]
        if not content.strip():
            self._display[-1] = (role, "*(empty response)*")
        else:
            self.messages.append({"role": "assistant", "content": content})
        self._render()
        self._collect_code_blocks(content)
        self.stop()
        self._maybe_auto_run()
        self.input.setFocus()

    def _on_failed(self, message: str):
        role, content = self._display[-1]
        self._display[-1] = (role, content + f"\n\n**Request failed.** {message}")
        self._render()
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()   # let the user retry cleanly
        self.stop()

    # ------------------------------------------------------------ render

    @property
    def _display(self):
        if not hasattr(self, "_display_list"):
            self._display_list = []
        return self._display_list

    def _render(self, follow: bool = None):
        parts = []
        for role, content in self._display:
            who = "You" if role == "user" else "Assistant"
            parts.append(f"**{who}**\n\n{content or '…'}")

        bar = self.view.verticalScrollBar()
        # Follow the stream only while the reader is already at the bottom.
        at_bottom = follow if follow is not None else (
            bar.value() >= bar.maximum() - 40)
        previous = bar.value()

        self.view.setMarkdown("\n\n---\n\n".join(parts))

        if at_bottom:
            bar.setValue(bar.maximum())
        else:
            # setMarkdown rebuilds the document and resets the bar to the top,
            # so put the reader back where they were.
            bar.setValue(min(previous, bar.maximum()))

    # -------------------------------------------------------- code blocks

    def _collect_code_blocks(self, content: str):
        """Build the run list from a reply.

        A block of independent commands becomes one entry per line, so nothing
        runs more than a single command at a time. A script — anything with a
        loop, conditional, heredoc or line continuation — stays whole, because
        its lines mean nothing apart.
        """
        self.code_picker.clear()
        found = False
        for lang, body in CODE_BLOCK.findall(content):
            block = body.strip("\n").rstrip()
            if not block:
                continue

            if is_script(block):
                lines = block.splitlines()
                head = lines[0].strip()
                if len(head) > 48:
                    head = head[:48] + "…"
                label = f"script: {head}  ({len(lines)} lines)"
                if lang:
                    label = f"[{lang}] {label}"
                self.code_picker.addItem(
                    label, {"text": block, "lang": lang.lower(), "script": True})
                found = True
            else:
                commands = [ln.strip() for ln in block.splitlines()
                            if ln.strip() and not ln.strip().startswith("#")]
                for i, cmd in enumerate(commands, 1):
                    shown = cmd if len(cmd) <= 60 else cmd[:60] + "…"
                    label = (f"{i}/{len(commands)}  {shown}"
                             if len(commands) > 1 else shown)
                    if lang:
                        label = f"[{lang}] {label}"
                    self.code_picker.addItem(
                        label, {"text": cmd, "lang": lang.lower(), "script": False})
                    found = True

            if self.code_picker.count() >= 25:
                break

        self.code_picker.setVisible(found)
        self.run_btn.setVisible(found)
        self.auto_run.setVisible(found or self.auto_run.isChecked())
        self.check_results.setVisible(found or self.auto_run.isChecked())

        # Auto-run takes the first entry only. Say so rather than quietly
        # dropping the rest — the others stay selectable in the dropdown.
        extra = self.code_picker.count() - 1
        if extra > 0 and self.auto_run.isChecked():
            self.notice.emit(
                f"Running the first of {self.code_picker.count()} commands — "
                f"{extra} more in the dropdown")

    # ---------------------------------------------------------- auto-run

    AUTO_RUN_DELAY = 4   # seconds to change your mind

    def _auto_run_toggled(self, on: bool):
        if on:
            self.run_btn.setStyleSheet(
                "QPushButton{background:#5a4412;border-color:#8a6a1f;}")
            self.notice.emit("Auto-run armed — replies will run without asking")
        else:
            self.run_btn.setStyleSheet("")
            self._cancel_auto_run()

    def _maybe_auto_run(self):
        """Called once a reply is complete."""
        if not self.auto_run.isChecked() or self.code_picker.count() == 0:
            return
        if self.terminal is None or self.terminal.proc is None:
            self.notice.emit("Auto-run skipped — no shell session")
            return

        if self._auto_chain >= self.MAX_CHAIN:
            self.notice.emit(
                f"Auto-run paused after {self.MAX_CHAIN} rounds — press Run to continue")
            return

        self.code_picker.setCurrentIndex(0)
        data = self.code_picker.currentData() or {}
        block, lang = data.get("text", ""), data.get("lang", "")

        if not block:
            return
        if lang not in SHELL_LANGS:
            self.notice.emit(f"Auto-run skipped — first block is {lang}, not shell")
            return
        if is_destructive(block):
            self.notice.emit("Auto-run held — this one looks destructive")
            self._run_selected()      # falls through to the usual confirmation
            return

        self._countdown = self.AUTO_RUN_DELAY
        self._auto_tick_label()
        self._auto_timer.start(1000)

    def _auto_tick(self):
        self._countdown -= 1
        if self._countdown <= 0:
            self._auto_timer.stop()
            self.run_btn.setText("Run in terminal")
            data = self.code_picker.currentData() or {}
            block = data.get("text", "")
            if block:
                self._auto_chain += 1
                self.notice.emit(f"Auto-running (round {self._auto_chain})")
                self._emit_run(block)
            return
        self._auto_tick_label()

    def _auto_tick_label(self):
        self.run_btn.setText(f"Cancel ({self._countdown})")

    def _cancel_auto_run(self, message: str = ""):
        if self._auto_timer.isActive():
            self._auto_timer.stop()
            if message:
                self.notice.emit(message)
        self._countdown = 0
        self.run_btn.setText("Run in terminal")

    MAX_CHAIN = 6   # automatic run→check rounds before handing back control

    def _advance_picker(self):
        """Step to the next command so repeated Run clicks walk the list."""
        i = self.code_picker.currentIndex()
        if 0 <= i < self.code_picker.count() - 1:
            self.code_picker.setCurrentIndex(i + 1)

    def _emit_run(self, block: str):
        self._last_run = block
        self.run_in_terminal.emit(block, self.check_results.isChecked())

    def on_command_output(self, command: str, output: str, status: str):
        """The terminal finished a command we started. Send the result back."""
        if not self.check_results.isChecked():
            return
        if self.streaming:
            self.notice.emit("Command finished while a reply was in flight")
            return

        if status == "waiting":
            self._cancel_auto_run()
            self.notice.emit("Command is waiting for input — over to you")
            self.auto_run.setChecked(False)
            return

        note = ("\n\n(The command was still running after the timeout; this is "
                "partial output.)" if status == "timeout" else "")

        payload = (
            f"I ran this in the terminal:\n```\n{command}\n```\n\n"
            f"Output:\n```\n{output or '(no output)'}\n```{note}\n\n"
            "Check the result. If it failed, explain briefly and give the "
            "corrected command. If it worked and steps remain, give only the "
            "next command. If the task is complete, reply DONE and give no "
            "code block.")

        shown = output if len(output) <= 1500 else output[:1500] + "\n… truncated …"
        display = (f"Ran:\n```\n{command}\n```\nOutput:\n```\n"
                   f"{shown or '(no output)'}\n```")

        self._dispatch(payload, display)

    def _clear_blocks(self):
        """Drop the previous round's commands.

        Blocks belong to one reply only. Leaving them in the picker while the
        next reply is in flight means the Run button would fire a command the
        assistant has already moved past — the exact thing that goes wrong
        after a failed command is corrected.
        """
        self._cancel_auto_run()
        self.code_picker.clear()
        self.code_picker.setVisible(False)
        self.run_btn.setVisible(False)

    def _dispatch(self, payload: str, display: str):
        """Append a user turn and start a request."""
        self._clear_blocks()
        self.messages.append({"role": "user", "content": payload})
        self._display.append(("user", display))
        self._display.append(("assistant", ""))
        self._render(follow=True)

        self.streaming = True
        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)

        self.worker = ChatWorker(self.cfg, list(self.messages), self)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _run_selected(self):
        if self._auto_timer.isActive():
            self._cancel_auto_run("Auto-run cancelled")
            return
        data = self.code_picker.currentData() or {}
        block = data.get("text", "")
        if not block:
            return
        lines = block.splitlines()
        preview = "\n".join(lines[:15])
        if len(lines) > 15:
            preview += f"\n… and {len(lines) - 15} more lines"
        note = ("\n\nThis is a script — the whole thing is sent as one unit."
                if data.get("script") else "")
        if is_destructive(block):
            note += "\n\nThis looks hard to undo. Read it carefully."
        confirm = QMessageBox.question(
            self, "Run in terminal?",
            f"Send this to the shell?{note}\n\n{preview}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm == QMessageBox.Yes:
            self._emit_run(block)
            self._advance_picker()

    def _paste_into_input(self):
        """Right-click in the transcript drops the clipboard into the composer."""
        text = QGuiApplication.clipboard().text()
        if not text:
            self.notice.emit("Clipboard is empty")
            return
        self.input.setFocus()
        self.input.insertPlainText(text)
        self.notice.emit("Pasted into the message box")

    def new_chat_shortcut(self):
        self.new_chat()
