"""The right-hand pane: conversation, composer, and the bridge to the shell."""

import re
import time

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


# Sent with every request while auto-run is armed, so the model works the
# way the loop does: one command, look at the result, then the next.
AGENT_RULES = (
    "[Auto-run is on: your commands run in the terminal automatically and I "
    "send you each command's output.]\n"
    "Work one step at a time. Reply with a short note on what you are doing "
    "and exactly ONE fenced code block holding exactly one command (or one "
    "script that must run as a unit). Never list several commands to run in "
    "sequence; choose the next one only after you have seen the previous "
    "output. Do not use commands that wait for input (pagers, editors, "
    "interactive prompts); prefer non-interactive flags such as -y, "
    "--no-pager, | cat. When output may be long, filter it (grep, head, "
    "tail, a specific subcommand or field) rather than dumping everything. "
    "Never guess at options: if a command fails because of "
    "a bad option or unknown command, check its --help once, then use only "
    "options you saw there. If the same thing fails twice, stop trying "
    "variants. When the task is finished, or you cannot continue "
    "without me, reply starting with DONE and a short summary, and give no "
    "code block.")

DONE_RE = re.compile(r"^\W*DONE\b", re.M)

THINK_OPEN, THINK_CLOSE = "<think>", "</think>"


def split_thinking(text: str):
    """Pull <think>…</think> reasoning out of a reply. Returns (thinking, answer).

    Some servers put a reasoning model's thinking inline in the content
    instead of a separate field. Commands inside it are the model musing, not
    instructions, so they must never reach the run list. Handles an unclosed
    tag (still thinking) and a missing opening tag (some R1 templates).
    """
    thoughts, answer, rest = [], [], text
    if THINK_CLOSE in rest and THINK_OPEN not in rest.split(THINK_CLOSE, 1)[0]:
        before, rest = rest.split(THINK_CLOSE, 1)
        thoughts.append(before)
    while THINK_OPEN in rest:
        before, after = rest.split(THINK_OPEN, 1)
        answer.append(before)
        if THINK_CLOSE in after:
            inner, rest = after.split(THINK_CLOSE, 1)
            thoughts.append(inner)
        else:
            thoughts.append(after)
            rest = ""
    answer.append(rest)
    return "\n".join(t.strip() for t in thoughts if t.strip()), "".join(answer).strip()


# Output lines that mean the command itself was wrong, not just that it
# reported a problem on the machine.
FAIL_RE = re.compile(
    r"(unrecognized option|invalid option|unknown option|illegal option|"
    r"unrecognized arguments|invalid choice|command not found|"
    r"is not recognized as an internal|no such file or directory|"
    r"permission denied|^\s*usage:|^\s*error:|^E: )", re.I | re.M)


def looks_failed(output: str) -> bool:
    return bool(FAIL_RE.search(output or ""))


def normalise_cmd(cmd: str) -> str:
    return " ".join(cmd.split()).lower()


def is_repeating(text: str) -> bool:
    """True when a stream has fallen into a loop of the same few words.

    Models sometimes degenerate and emit one fragment ("--showxgbe… --")
    until they hit the token limit, which can take minutes and may yield a
    garbage command. Looks at the tail for one unit of 3-150 characters
    repeated back to back, covering at least 300 characters.
    """
    tail = " ".join(text[-1500:].split())
    if len(tail) < 300:
        return False
    for n in range(3, 151):
        unit = tail[-n:]
        if not unit.strip():
            continue
        count, end = 0, len(tail)
        while end - n >= 0 and tail[end - n:end] == unit:
            count += 1
            end -= n
        if count >= 8 and count * n >= 300:
            return True
    return False


def fit_output(text: str, budget: int):
    """Fit terminal output into budget characters, cutting whole lines.

    Keeps the start (headers, first errors) and a larger end (final state,
    summary). Returns (text, note): note tells the model exactly which lines
    it is not seeing, or is "" when nothing was cut.
    """
    if len(text) <= budget:
        return text, ""
    lines = text.split("\n")
    head_budget, tail_budget = int(budget * 0.35), int(budget * 0.65)
    head, used = [], 0
    for ln in lines:
        if used + len(ln) + 1 > head_budget:
            break
        head.append(ln)
        used += len(ln) + 1
    tail, used = [], 0
    for ln in reversed(lines[len(head):]):
        if used + len(ln) + 1 > tail_budget:
            break
        tail.append(ln)
        used += len(ln) + 1
    tail.reverse()
    if not head and not tail:          # one enormous line
        return (text[:head_budget] + "\n…\n" + text[-tail_budget:],
                f"(A single line of {len(text):,} characters was cut in the "
                "middle.)")
    first, last = len(head) + 1, len(lines) - len(tail)
    marker = f"… lines {first:,}–{last:,} not shown …"
    note = (f"(The output was {len(lines):,} lines / {len(text):,} characters "
            f"and lines {first:,}–{last:,} are not shown. If you need them, run "
            "a narrower command instead of repeating this one: filter with "
            "grep for the fields you need, or save the output to a file once "
            f"and read a range, e.g. `sed -n '{first},{min(last, first + 199)}p'`.)")
    return "\n".join(head + [marker] + tail), note


def clip(text: str, limit: int) -> str:
    """Keep the head and tail of long output; the middle is usually repetition."""
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return (f"{text[:half]}\n… {len(text) - 2 * half} characters omitted …\n"
            f"{text[-half:]}")


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
            "Let the assistant work through the task step by step: it runs one "
            "command, reads the output, then decides the next.\n"
            "You get a few seconds to cancel each step, anything destructive "
            "still prompts, and typing in the terminal takes over. Resets when "
            "the app restarts.")
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

        # Ticks the "waiting / thinking for N s" line while a reply is pending.
        self._wait_timer = QTimer(self)
        self._wait_timer.timeout.connect(self._render)
        self._req_started = 0.0
        self._answer_started = 0.0
        self._chain_log = []    # auto-run this chain: (command, failed)
        self._thinking = {}     # display index -> reasoning streamed separately
        self._think_secs = {}   # display index -> seconds spent thinking

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
        self._thinking = {}
        self._think_secs = {}
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
                full = len(screen)
                screen, cut = fit_output(screen, self.output_budget)
                payload = (f"{text}\n\nHere is the terminal output since it was "
                           f"last attached:\n```\n{screen}\n```"
                           f"{chr(10) * 2 + cut if cut else ''}")
                display = (f"{text}\n\n*(terminal output attached — "
                           f"{self._size_note(full, len(screen) if cut else full)})*")

        if self.auto_run.isChecked():
            payload = f"{payload}\n\n{AGENT_RULES}"
            display += "\n\n*(auto-run: step by step)*"

        self.input.clear()
        self._auto_chain = 0          # a typed message starts a fresh chain
        self._chain_log = []
        self._dispatch(payload, display)

    def stop(self):
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(1500)
            self.worker = None
        self.streaming = False
        self._wait_timer.stop()
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)

    def _on_chunk(self, text: str):
        if not self.streaming:
            return
        role, current = self._display[-1]
        self._display[-1] = (role, current + text)
        if is_repeating(current + text):
            self._abort_runaway()
            return
        if not self._answer_started and split_thinking(current + text)[1]:
            self._answer_started = time.monotonic()
        if not self.render_timer.isActive():
            self.render_timer.start(90)

    def _on_thinking(self, text: str):
        if not self.streaming:
            return
        i = len(self._display) - 1
        self._thinking[i] = self._thinking.get(i, "") + text
        if is_repeating(self._thinking[i]):
            self._abort_runaway()
            return
        if not self.render_timer.isActive():
            self.render_timer.start(90)

    def _abort_runaway(self):
        """Cut off a reply that is looping, and hand control back."""
        worker = self.worker
        if worker is not None:
            for sig in (worker.chunk, worker.thinking,
                        worker.finished_ok, worker.failed):
                try:
                    sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
        i = len(self._display) - 1
        role, raw = self._display[-1]
        _, answer = split_thinking(raw)
        keep = answer[:600] + ("…" if len(answer) > 600 else "")
        if self._thinking.get(i, "") and len(self._thinking[i]) > 1500:
            self._thinking[i] = self._thinking[i][:1500] + " …"
        self._display[-1] = (role, (keep + "\n\n" if keep else "") +
                             "**Stopped — the model started repeating itself.** "
                             "Nothing from this reply was run.")
        # Keep the history consistent so the next message still makes sense.
        self.messages.append({"role": "assistant", "content":
                              "(My previous reply broke down into repeated text "
                              "and was cut off.)"})
        self.stop()
        self._clear_blocks()
        if self.auto_run.isChecked():
            self.auto_run.setChecked(False)
        self.notice.emit("Reply cut off — the model was looping. Auto-run paused")
        self._render()

    def _on_done(self):
        role, raw = self._display[-1]
        i = len(self._display) - 1
        if self._req_started and (self._thinking.get(i) or
                                  split_thinking(raw)[0]):
            end = self._answer_started or time.monotonic()
            self._think_secs[i] = end - self._req_started
        # Only the answer counts. Thinking is neither sent back to the model
        # nor searched for commands to run.
        content = split_thinking(raw)[1]
        if not content.strip():
            self._display[-1] = (role, raw if raw.strip() else "*(empty response)*")
        else:
            self.messages.append({"role": "assistant", "content": content})
        self.streaming = False
        self._render()
        self._collect_code_blocks(content)
        self.stop()
        if (self.auto_run.isChecked() and self._auto_chain
                and DONE_RE.search(content) and self.code_picker.count() == 0):
            self.notice.emit(f"Auto-run finished after {self._auto_chain} step"
                             f"{'s' if self._auto_chain != 1 else ''}")
            self._auto_chain = 0
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

    THINK_SHOWN = 4000   # characters of reasoning kept on screen

    def _assistant_block(self, i: int, raw: str) -> str:
        inline, answer = split_thinking(raw)
        thought = "\n".join(t for t in (self._thinking.get(i, ""), inline) if t)
        live = self.streaming and i == len(self._display) - 1
        now = time.monotonic()
        out = []
        if thought:
            if live and not answer:
                head = f"*Thinking… {now - self._req_started:.0f} s*"
            else:
                secs = self._think_secs.get(i)
                head = f"*Thought for {secs:.0f} s*" if secs else "*Thinking*"
            shown = thought.strip()
            if len(shown) > self.THINK_SHOWN:
                shown = "…" + shown[-self.THINK_SHOWN:]
            quoted = "\n".join("> " + ln if ln.strip() else ">"
                               for ln in shown.splitlines())
            out.append(f"{head}\n\n{quoted}")
        if answer:
            out.append(answer)
        elif live and not thought:
            out.append(f"*Waiting for the model… "
                       f"{now - self._req_started:.0f} s*")
        elif not live and not thought:
            out.append(raw or "…")
        return "\n\n".join(out)

    def _render(self, follow: bool = None):
        parts = []
        for i, (role, content) in enumerate(self._display):
            if role == "user":
                parts.append(f"**You**\n\n{content or '…'}")
            else:
                parts.append(f"**Assistant**\n\n{self._assistant_block(i, content)}")

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

        # Auto-run takes the first entry only; the rest are the model planning
        # ahead without having seen any output, so they are not run. It will
        # be asked for the next step once it has read this one's result.
        extra = self.code_picker.count() - 1
        if extra > 0 and self.auto_run.isChecked():
            self.notice.emit(
                f"Running step 1 of {self.code_picker.count()} suggested — the "
                f"next step is chosen after reading its output")

    # ---------------------------------------------------------- auto-run

    AUTO_RUN_DELAY = 4   # seconds to change your mind

    def _auto_run_toggled(self, on: bool):
        if on:
            # Auto-run without reading results is just running a list blind.
            # The loop only works if every output goes back to the model.
            self.check_results.setChecked(True)
            self.check_results.setEnabled(False)
            self.run_btn.setStyleSheet(
                "QPushButton{background:#5a4412;border-color:#8a6a1f;}")
            self.notice.emit("Auto-run armed — one command at a time, each "
                             "result read before the next")
            self._auto_chain = 0
            self._chain_log = []
            # Commands already on offer from the last reply: start with the
            # first of them now instead of waiting for another reply.
            if not self.streaming:
                self._maybe_auto_run()
        else:
            self.check_results.setEnabled(True)
            self.run_btn.setStyleSheet("")
            self._cancel_auto_run()

    @property
    def output_budget(self) -> int:
        return max(2000, int(self.cfg.get("ai_output_chars", 24000)))

    @staticmethod
    def _size_note(full: int, sent: int) -> str:
        if sent >= full:
            return f"all {full:,} characters sent to the assistant"
        return (f"{sent:,} of {full:,} characters sent to the assistant; the "
                f"middle was left out and it was told which lines")

    @property
    def max_steps(self) -> int:
        return max(1, int(self.cfg.get("auto_run_max_steps", 20)))

    def _maybe_auto_run(self):
        """Called once a reply is complete."""
        if not self.auto_run.isChecked() or self.code_picker.count() == 0:
            return
        if self._auto_timer.isActive():
            return
        if self.terminal is None or self.terminal.proc is None:
            self.notice.emit("Auto-run skipped — no shell session")
            return
        if getattr(self.terminal, "busy", False):
            self.notice.emit("Auto-run waiting — the last command is still running")
            return

        if self._auto_chain >= self.max_steps:
            self.notice.emit(
                f"Auto-run paused after {self.max_steps} steps — press Run to "
                f"continue, or send a message")
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
            if block and getattr(self.terminal, "busy", False):
                self.notice.emit("Auto-run held — the terminal is still busy")
                return
            if block:
                self._auto_chain += 1
                self.notice.emit(f"Auto-run step {self._auto_chain}/{self.max_steps}")
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

    def _advance_picker(self):
        """Step to the next command so repeated Run clicks walk the list."""
        i = self.code_picker.currentIndex()
        if 0 <= i < self.code_picker.count() - 1:
            self.code_picker.setCurrentIndex(i + 1)

    def _emit_run(self, block: str):
        self._last_run = block
        # Blocks belong to the reply that offered them. Once one runs, the
        # rest are stale until the model has seen this output.
        if self.auto_run.isChecked():
            self._clear_blocks()
        self.run_in_terminal.emit(
            block, self.check_results.isChecked() or self.auto_run.isChecked())

    def on_command_output(self, command: str, output: str, status: str):
        """The terminal finished a command we started. Send the result back."""
        auto = self.auto_run.isChecked()
        if not (self.check_results.isChecked() or auto):
            return
        if self.streaming:
            self.notice.emit("Command finished while a reply was in flight")
            return

        if status == "waiting":
            self._cancel_auto_run()
            self.notice.emit("Command is waiting for input — over to you")
            self.auto_run.setChecked(False)
            return

        note = ""
        if status in ("timeout", "stalled"):
            note = ("\n\n(The command had not returned to the prompt when this "
                    "was captured, so it may still be running and the output may "
                    "be partial.)")
            if auto:
                # Typing the next command now would feed it to the running one.
                self.auto_run.setChecked(False)
                self.notice.emit("Command did not finish — auto-run paused; "
                                 "check the terminal, then re-arm")
                auto = False

        warn = ""
        if auto:
            failed = looks_failed(output)
            key = normalise_cmd(command)
            repeat = any(k == key for k, _ in self._chain_log)
            self._chain_log.append((key, failed))
            streak = 0
            for _, f in reversed(self._chain_log):
                if not f:
                    break
                streak += 1
            if failed and (repeat or streak >= 3):
                # Retrying the same thing, or guessing variant after variant:
                # more steps will not help. Let the model explain, but stop.
                why = ("the same command failed again" if repeat
                       else f"{streak} commands in a row failed")
                self.auto_run.setChecked(False)
                self.notice.emit(f"Auto-run paused — {why}")
                auto = False
                warn = (f"\n\nAuto-run has been stopped because {why}. Do not "
                        "suggest another variant. Explain briefly what is going "
                        "wrong and what you need from me.")

        step = (f"Step {self._auto_chain} of at most {self.max_steps}. "
                if auto else "")
        full_output = output or ""
        output, cut = fit_output(full_output, self.output_budget)
        if cut:
            note = f"\n\n{cut}{note}"
        head = f"I ran this in the terminal:\n```\n{command}\n```\n\n"
        # What this turn shrinks to once newer results have arrived. Old
        # outputs have already been acted on; re-sending them in full on every
        # step made each request bigger and slower than the last.
        short = (f"{head}Output (shortened, already handled):\n```\n"
                 f"{clip(output or '(no output)', 800)}\n```")
        payload = (
            f"{head}"
            f"Output:\n```\n{output or '(no output)'}\n```{note}{warn}\n\n"
            f"{step}Read the output and check the result against the goal. If "
            "it failed, explain briefly and give the corrected command. If it "
            "worked and steps remain, give only the next single command, chosen "
            "from what this output shows. If the task is complete, reply DONE "
            "with a short summary and give no code block.")
        if auto:
            payload += f"\n\n{AGENT_RULES}"

        # The transcript shows a preview; the full text is in the terminal.
        preview = self.PREVIEW_CHARS
        shown = (full_output if len(full_output) <= preview
                 else full_output[:preview].rsplit("\n", 1)[0])
        display = (f"Ran:\n```\n{command}\n```\nOutput:\n```\n"
                   f"{shown or '(no output)'}\n```")
        if len(full_output) > preview:
            display += (f"\n*Preview only — "
                        f"{self._size_note(len(full_output), len(output) if cut else len(full_output))}.*")

        self._dispatch(payload, display, short=short)

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

    KEEP_FULL_RESULTS = 2   # newest command outputs sent in full
    PREVIEW_CHARS = 3000    # output shown in the transcript (display only)

    def _api_messages(self) -> list:
        """The history as sent: older command outputs cut down to a summary."""
        results = [i for i, m in enumerate(self.messages) if "short" in m]
        old = set(results[:-self.KEEP_FULL_RESULTS])
        return [{"role": m["role"],
                 "content": m["short"] if i in old else m["content"]}
                for i, m in enumerate(self.messages)]

    def _dispatch(self, payload: str, display: str, short: str = None):
        """Append a user turn and start a request."""
        self._clear_blocks()
        msg = {"role": "user", "content": payload}
        if short:
            msg["short"] = short
        self.messages.append(msg)
        self._display.append(("user", display))
        self._display.append(("assistant", ""))
        self.streaming = True
        self._req_started = time.monotonic()
        self._answer_started = 0.0
        self._render(follow=True)

        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self._wait_timer.start(1000)

        self.worker = ChatWorker(self.cfg, self._api_messages(), self)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.thinking.connect(self._on_thinking)
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
