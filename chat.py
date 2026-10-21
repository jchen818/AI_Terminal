"""The right-hand pane: conversation, composer, and the bridge to the shell."""

from collections import Counter
import hashlib
import json
import re
import time

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import (QActionGroup, QFont, QGuiApplication, QKeySequence,
                           QShortcut, QTextDocument)
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QTextBrowser, QVBoxLayout,
    QWidget,
)

import memory
from ai_client import (TOOL_BLOCK, TOOL_FENCE, ChatWorker, parse_text_tool_call,
                       strip_tool_blocks, text_tool_prompt)
from config import TOOL_MODES, save_config

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
    "variants. Never re-run a command whose output you already have: "
    "the log of earlier steps is authoritative, so act on it. To wait for "
    "something, use ONE command that loops until it is ready, with a "
    "timeout (e.g. `timeout 120 sh -c 'until <check>; do sleep 5; done'`), "
    "not the same check repeated. When the task is finished, or you cannot continue "
    "without me, reply starting with DONE and a short summary, and give no "
    "code block.")

DONE_RE = re.compile(r"^\W*DONE\b", re.M)

THINK_OPEN, THINK_CLOSE = "<think>", "</think>"

# Markdown with raw HTML switched off. Qt's parser otherwise passes <tags>
# straight to its HTML engine, so a stray <td>, <li> or <none> in a reply or
# in terminal output turns into tables and bullets and swallows the text
# after it.
_MD = QTextDocument.MarkdownFeature
MARKDOWN_TEXT_ONLY = _MD(_MD.MarkdownDialectGitHub.value | _MD.MarkdownNoHTML.value)


def set_markdown(view, text: str):
    view.document().setMarkdown(text, MARKDOWN_TEXT_ONLY)


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


_DIGITS = re.compile(r"\d+")


def output_signature(output: str) -> str:
    """Fingerprint of an output that ignores what changes on every run.

    Ages, timestamps, PIDs and byte counters differ between two runs of a
    status check even when nothing happened, so digits are dropped. Two runs
    of one command with the same signature mean no progress was made.
    """
    text = _DIGITS.sub("#", " ".join((output or "").split()).lower())
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


_KEY_LINE = re.compile(r"error|fail|denied|not found|no such|invalid|"
                       r"cannot|unable|warning|active:|status", re.I)


def summarize_output(output: str, limit: int = 160) -> str:
    """One line for the step log: the first and last meaningful lines."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "(no output)"
    picked = [lines[0]]
    # The line that says what went wrong is usually neither first nor last.
    key = next((ln for ln in lines[1:-1] if _KEY_LINE.search(ln)), None)
    if key:
        picked.append(key)
    if len(lines) > 1:
        picked.append(lines[-1])
    text = " … ".join(picked)
    if len(lines) > 2:
        text += f"  [{len(lines)} lines]"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _cycle_period(keys: list) -> int:
    """Length of a cycle the tail of keys keeps repeating (A,B,A,B,A,B), or 0.

    One step repeated is caught separately; this catches the model bouncing
    between two or three commands that each give the same answer as before
    (A,B,A,B or A,B,C,A,B,C).
    """
    for p in (2, 3):
        reps = 2
        if len(keys) < p * reps:
            continue
        tail = keys[-p * reps:]
        unit = tail[-p:]
        if len(set(unit)) == p and all(tail[i:i + p] == unit
                                       for i in range(0, len(tail), p)):
            return p
    return 0


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
    return _repeats_loosely(text)


def _repeats_loosely(text: str, window: int = 4000, n: int = 6,
                     min_words: int = 150, share: float = 0.6) -> bool:
    """Catch loops whose wording drifts between repeats.

    Reasoning models often restate one idea with a new lead-in each time
    ("Maybe use X…", "Let's think of using X…", "Could use X…"), or cycle
    through several sentences ("the output says X… wait… the output is
    just (no output)…"), so no exact unit repeats back to back. Instead:
    split the tail into 6-word phrases and flag it when 60% of them occur
    3+ times. Normal
    prose, and even code, almost never reuses that many long phrases.
    """
    words = text[-window:].lower().split()
    if len(words) < min_words:
        return False
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    counts = Counter(grams)
    repeated = sum(1 for g in grams if counts[g] >= 3)
    return repeated / len(grams) >= share


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


def _msg_chars(m: dict) -> int:
    n = len(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += len(c.get("name", "")) + len(json.dumps(c.get("arguments") or {}))
    return n


class ToolWorker(QThread):
    """Runs approved MCP tool calls one after another, off the GUI thread."""

    result = Signal(str, str, bool)   # call id, text, is_error

    def __init__(self, manager, jobs, timeout, parent=None):
        super().__init__(parent)
        self.manager, self.jobs, self.timeout = manager, jobs, timeout
        self._stop = False

    def cancel(self):
        # A call already sent finishes (or times out) on its own; nothing
        # after it is started.
        self._stop = True

    def run(self):
        for call, tool in self.jobs:
            if self._stop:
                return
            text, is_error = self.manager.call(tool.key, call["arguments"],
                                               self.timeout)
            if self._stop:
                return
            self.result.emit(call["id"], text, is_error)


def tool_policy(cfg: dict, tool) -> str:
    """allow | ask | never. Tools their server marks read-only default to
    allow; everything else asks until told otherwise."""
    chosen = (cfg.get("mcp_tool_policy") or {}).get(tool.key)
    if chosen in ("allow", "ask", "never"):
        return chosen
    return "allow" if tool.read_only and not tool.destructive else "ask"


def _pretty_args(args) -> str:
    try:
        return json.dumps(args or {}, indent=1, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


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

    def __init__(self, cfg: dict, terminal, mcp=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.terminal = terminal
        self.mcp = mcp            # MCPManager, or None without MCP
        self.messages = []        # [{role, content}] sent to the API
        self.worker = None
        self.streaming = False
        self._retired = []        # cancelled threads, kept until they end
        self._agent_turn = False  # the request in flight is an auto-run step
        # Tool calls in progress: the round being run and its results so far.
        self._tool_worker = None
        self._tools_busy = False
        self._tool_round = None   # {calls, results, display_index, over}
        self._text_only = set()   # (base_url, model) that refused native tools

        # --- header ------------------------------------------------------
        self.model_label = QLabel()
        self.model_label.setStyleSheet("color:#9aa0a6;")
        settings_btn = QPushButton("Settings")
        settings_btn.setFixedWidth(80)
        settings_btn.clicked.connect(self.open_settings.emit)
        new_btn = QPushButton("New chat")
        new_btn.setFixedWidth(90)
        new_btn.clicked.connect(self.new_chat)
        self.history_btn = QPushButton("History")
        self.history_btn.setFixedWidth(80)
        self.history_btn.setToolTip(
            "Reopen an earlier chat, or view and edit what the assistant "
            "remembers between chats.")
        self.history_menu = QMenu(self)
        self.history_menu.aboutToShow.connect(self._fill_history_menu)
        self.history_btn.setMenu(self.history_menu)

        self.tools_btn = QPushButton("Tools")
        self.tools_btn.setMinimumWidth(80)
        self.tools_menu = QMenu(self)
        self.tools_menu.aboutToShow.connect(self._fill_tools_menu)
        self.tools_btn.setMenu(self.tools_menu)
        if self.mcp is not None:
            self.mcp.changed.connect(self._update_tools_button)

        header = QHBoxLayout()
        header.addWidget(QLabel("Assistant"))
        header.addWidget(self.model_label, 1)
        header.addWidget(self.tools_btn)
        header.addWidget(self.history_btn)
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
        self._last_trim = 0

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
        self._chain_log = []    # auto-run steps: {cmd, key, sig, failed, result, summary}
        self._nudged = set()    # commands already refused once this chain
        self._thinking = {}     # display index -> reasoning streamed separately
        self._think_secs = {}   # display index -> seconds spent thinking

        # Saved history and memory of earlier chats.
        self.chat_id = memory.new_chat_id()
        self.chat_title = ""
        self.chat_created = time.time()
        self._run_log = []      # [{command, result}] for this chat
        self._memory = ""       # digest of earlier chats, fixed per chat

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
        self._load_memory()
        self._update_tools_button()

    def apply_font(self, size: int):
        font = QFont(self.view.font())
        font.setPointSize(max(1, size))
        self.view.setFont(font)
        self.view.document().setDefaultFont(font)
        self.input.setFont(font)

    def _reset_state(self):
        self.stop()
        self._agent_turn = False
        self._cancel_auto_run()
        self._auto_chain = 0
        self._reset_chain()
        self.messages = []
        self._display_list = []
        self._thinking = {}
        self._think_secs = {}
        self._run_log = []
        self.code_picker.clear()
        self.code_picker.setVisible(False)
        self.run_btn.setVisible(False)

    def new_chat(self):
        self.save_chat()
        self._reset_state()
        self.chat_id = memory.new_chat_id()
        self.chat_title = ""
        self.chat_created = time.time()
        self._load_memory()
        intro = ("Ask a question, or tick **Include terminal screen** to ask "
                 "about what the shell just printed.\n\n"
                 "Commands the assistant suggests in code blocks can be sent "
                 "straight to the terminal.")
        if self._memory:
            intro += ("\n\n*The assistant remembers your recent chats. "
                      "**History** reopens one, or shows what it remembers.*")
        set_markdown(self.view, intro)
        self.input.setFocus()

    # ------------------------------------------------------------ memory

    @property
    def memory_sessions(self) -> int:
        return max(0, int(self.cfg.get("memory_sessions", 10)))

    def _load_memory(self):
        """Build the digest of earlier chats once per chat.

        Fixed for the life of a chat so every request in it sends the same
        system prompt, which keeps servers' prompt caches warm.
        """
        try:
            self._memory = memory.memory_prompt(
                self.memory_sessions, exclude=self.chat_id,
                budget=min(memory.MEMORY_CHARS, self.context_budget // 4))
        except OSError:
            self._memory = ""

    def _request_cfg(self) -> dict:
        cfg = dict(self.cfg)
        if self._memory:
            base = cfg.get("system_prompt", "").strip()
            cfg["system_prompt"] = f"{base}\n\n{self._memory}" if base else self._memory
        return cfg

    def save_chat(self):
        """Write this chat to disk. Chats with nothing sent are not kept."""
        if not self.messages:
            return
        display = [[r, c] for r, c in self._display]
        if self.streaming and display and display[-1][0] == "assistant":
            display[-1][1] = (display[-1][1] + "\n\n*(Interrupted — the app "
                              "was closed or the chat switched.)*")
        chat = {
            "id": self.chat_id,
            "title": self.chat_title or "Untitled chat",
            "created": self.chat_created,
            "updated": time.time(),
            "model": self.cfg.get("model", ""),
            "messages": self.messages,
            "display": display,
            "thinking": {str(k): v for k, v in self._thinking.items()},
            "think_secs": {str(k): v for k, v in self._think_secs.items()},
            "log": self._run_log,
        }
        try:
            memory.save_chat(chat)
        except OSError as exc:
            self.notice.emit(f"Could not save chat history: {exc}")

    def open_chat(self, chat_id: str):
        if chat_id == self.chat_id:
            return
        data = memory.load_chat(chat_id)
        if data is None:
            self.notice.emit("That chat could not be opened")
            return
        self.save_chat()
        self._reset_state()
        self.chat_id = data.get("id", chat_id)
        self.chat_title = data.get("title", "")
        self.chat_created = data.get("created", time.time())
        self.messages = data.get("messages", [])
        self._display_list = [tuple(x) for x in data.get("display", [])]
        self._thinking = {int(k): v for k, v in data.get("thinking", {}).items()}
        self._think_secs = {int(k): v for k, v in data.get("think_secs", {}).items()}
        self._run_log = data.get("log", [])
        self._load_memory()
        self._render(follow=True)
        self.notice.emit(f"Opened: {self.chat_title}")
        self.input.setFocus()

    def _fill_history_menu(self):
        menu = self.history_menu
        menu.clear()
        chats = memory.list_chats(limit=30)
        if not chats:
            empty = menu.addAction("No saved chats yet")
            empty.setEnabled(False)
        today = time.strftime("%Y-%m-%d")
        for c in chats:
            stamp = time.localtime(c["updated"])
            when = (time.strftime("%H:%M", stamp)
                    if time.strftime("%Y-%m-%d", stamp) == today
                    else time.strftime("%b %d %H:%M", stamp))
            act = menu.addAction(f"{when}   {c['title']}")
            act.setCheckable(True)
            act.setChecked(c["id"] == self.chat_id)
            act.triggered.connect(lambda _=False, cid=c["id"]: self.open_chat(cid))
        menu.addSeparator()
        delete = menu.addAction("Delete this chat")
        delete.setEnabled(bool(self.messages))
        delete.triggered.connect(self._delete_current)
        menu.addAction("Memory…").triggered.connect(self._edit_memory)

    def _delete_current(self):
        if QMessageBox.question(
                self, "Delete chat?",
                f"Delete “{self.chat_title or 'this chat'}” from history? The "
                "assistant will no longer remember it.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        memory.delete_chat(self.chat_id)
        self.messages = []          # so new_chat does not save it again
        self.new_chat()
        self.notice.emit("Chat deleted")

    def _edit_memory(self):
        dlg = MemoryDialog(self.memory_sessions, self.chat_id, self)
        dlg.exec()
        if dlg.cleared:
            self.messages = []      # the open chat went with the rest
            self.new_chat()
        else:
            self._load_memory()

    # -------------------------------------------------------------- send

    def send(self):
        if self.streaming or self._tools_busy:
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
        short = None
        if self.include_output.isChecked():
            screen = self.terminal.capture_since_last() if self.terminal else ""
            if screen:
                full = len(screen)
                screen, cut = fit_output(screen, self.output_budget)
                payload = (f"{text}\n\nHere is the terminal output since it was "
                           f"last attached:\n```\n{screen}\n```"
                           f"{chr(10) * 2 + cut if cut else ''}")
                short = (f"{text}\n\n(Terminal output was attached here; "
                         f"shortened, already handled):\n```\n"
                         f"{clip(screen, 800)}\n```")
                display = (f"{text}\n\n*(terminal output attached — "
                           f"{self._size_note(full, len(screen) if cut else full)})*")

        if self.auto_run.isChecked():
            payload = f"{payload}\n\n{AGENT_RULES}"
            if short:
                short = f"{short}\n\n{AGENT_RULES}"
            display += "\n\n*(auto-run: step by step)*"

        if not self.chat_title:
            self.chat_title = memory.title_from(text)
        self.input.clear()
        self._auto_chain = 0          # a typed message starts a fresh chain
        self._reset_chain()
        self._dispatch(payload, display, short=short, typed=True)

    def stop(self):
        if self.worker is not None:
            self._retire(self.worker)
            self.worker = None
        self._cancel_tools()
        self.streaming = False
        self._wait_timer.stop()
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)

    def _on_chunk(self, text: str):
        if not self.streaming or self._stale(self.worker):
            return
        role, current = self._display[-1]
        self._display[-1] = (role, current + text)
        if is_repeating(current + text):
            self._abort_runaway()
            return
        answer = split_thinking(current + text)[1]
        if not self._answer_started and answer:
            self._answer_started = time.monotonic()
        # Text protocol: the reply is over once a tool block is closed. Models
        # often carry on and invent the tool's result; stop them first.
        if (self.worker is not None and self.worker.mode_used == "text"
                and TOOL_BLOCK.search(answer)):
            self._finish_early()
            return
        if not self.render_timer.isActive():
            self.render_timer.start(90)

    def _on_thinking(self, text: str):
        if not self.streaming or self._stale(self.worker):
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
        self.save_chat()
        self._render()

    def _finish_early(self):
        """End a text-protocol reply at its tool block. Called from _on_chunk,
        so _on_done sees this worker's chunk as the sender and accepts it."""
        self._on_done()

    def _on_done(self):
        if not self.streaming or self._stale(self.worker):
            return
        worker = self.worker
        mode = getattr(worker, "mode_used", "off") if worker else "off"
        calls = list(getattr(worker, "tool_calls", []) or []) if worker else []
        role, raw = self._display[-1]
        i = len(self._display) - 1
        if self._req_started and (self._thinking.get(i) or
                                  split_thinking(raw)[0]):
            end = self._answer_started or time.monotonic()
            self._think_secs[i] = end - self._req_started
        # Only the answer counts. Thinking is neither sent back to the model
        # nor searched for commands to run.
        inline, content = split_thinking(raw)
        if mode != "off":
            if not calls:
                # The text protocol, or a model leaking <tool_call> tags
                # because the server is not parsing them for it.
                content, call = parse_text_tool_call(content)
                if call:
                    calls = [call]
            content = strip_tool_blocks(content)
        if calls:
            self._on_tool_reply(role, inline, content, calls)
            return
        if not content.strip():
            self._display[-1] = (role, raw if raw.strip() else "*(empty response)*")
        else:
            self.messages.append({"role": "assistant", "content": content})
            saved = memory.add_notes(content)
            if saved:
                self.notice.emit("Remembered: " + "; ".join(saved)[:120])
        self.streaming = False
        self.save_chat()
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
        if self._tools_busy or not self.streaming or self._stale(self.worker):
            return
        role, content = self._display[-1]
        self._display[-1] = (role, content + f"\n\n**Request failed.** {message}")
        self._render()
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()   # let the user retry cleanly
        self.stop()
        self.save_chat()

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
            elif role == "tool":
                parts.append(content)
            else:
                parts.append(f"**Assistant**\n\n{self._assistant_block(i, content)}")

        bar = self.view.verticalScrollBar()
        # Follow the stream only while the reader is already at the bottom.
        at_bottom = follow if follow is not None else (
            bar.value() >= bar.maximum() - 40)
        previous = bar.value()

        set_markdown(self.view, "\n\n---\n\n".join(parts))

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
            if not block or lang.lower() == TOOL_FENCE:
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
            self._reset_chain()
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
        wanted = max(2000, int(self.cfg.get("ai_output_chars", 24000)))
        # One output may take at most half the conversation budget, or a
        # single result pushes everything else out.
        return min(wanted, self.context_budget // 2)

    @property
    def context_budget(self) -> int:
        return max(8000, int(self.cfg.get("context_chars", 96000)))

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
        if self._refuse_repeat(block):
            return
        if is_destructive(block):
            self.notice.emit("Auto-run held — this one looks destructive")
            self._run_selected()      # falls through to the usual confirmation
            return

        self._countdown = self.AUTO_RUN_DELAY
        self._auto_tick_label()
        self._auto_timer.start(1000)

    def _refuse_repeat(self, block: str) -> bool:
        """Hold a command the chain has already run twice.

        The first time, hand it back to the model with the earlier results;
        if it proposes it yet again, stop auto-run. True when held.
        """
        key = normalise_cmd(block)
        runs = [s for s in self._chain_log if s["key"] == key]
        if len(runs) < 2:
            return False
        if key in self._nudged:
            self.auto_run.setChecked(False)
            self.notice.emit("Auto-run paused — the assistant keeps proposing "
                             "a command it has already run")
            return True
        self._nudged.add(key)
        self._clear_blocks()
        seen = "\n".join(f"- step {s['step']}: {s['result']}: {s['summary']}"
                         for s in runs)
        payload = (
            f"You proposed this command again:\n```\n{block}\n```\n"
            f"It was NOT run. It has already run {len(runs)} times for this "
            f"task:\n{seen}\n\nRunning it again will not tell you anything "
            "new. Decide from the outputs you already have: give a different "
            "next command, or reply DONE with a summary (or say what you need "
            f"from me if you are stuck).\n\n{AGENT_RULES}")
        display = (f"*(auto-run held `{clip(block, 80)}` — already run "
                   f"{len(runs)} times; asked the assistant for a different step)*")
        self.notice.emit("Auto-run held a repeated command")
        self._dispatch(payload, display, agent=True)
        return True

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
        result = {"timeout": "did not finish (timed out)",
                  "stalled": "did not finish (stalled)",
                  "waiting": "stopped waiting for input"}.get(
            status, "failed" if looks_failed(output) else "ok")
        self._run_log.append({"command": command, "result": result,
                              "time": time.time()})
        self.save_chat()
        auto = self.auto_run.isChecked()
        # Decided before auto-run can be switched off below: the model's answer
        # to a stopped chain must still be built from the chain's state.
        in_chain = auto
        if not (self.check_results.isChecked() or auto):
            return
        if self.streaming or self._tools_busy:
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
        stopped = False
        if in_chain:
            # Logged even when the step did not finish, so the step log
            # always ends with the command shown in full below.
            why, warn = self._check_progress(command, output, status)
            if not auto:
                warn = ""
            elif why:
                # More steps will not help. Let the model explain, but stop.
                self.auto_run.setChecked(False)
                self.notice.emit(f"Auto-run paused — {why}")
                auto = False
                stopped = True
                warn = (f"\n\nAuto-run has been stopped because {why}. Do not "
                        "suggest another command. Explain briefly what the "
                        "outputs show, what is going wrong and what you need "
                        "from me.")

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
        body = (f"Output:\n```\n{output}\n```" if output.strip() else
                "Output: none. The command printed nothing and the shell "
                "returned to the prompt. For commands like systemctl enable, "
                "mkdir or cp that normally means it succeeded."
                if status not in ("timeout", "stalled") else
                "Output: none so far.")
        if stopped:
            ask = ("Read the outputs and tell me what they show, without a "
                   "code block.")
        else:
            ask = (f"{step}Read the output and check the result against the "
                   "goal. If it failed, explain briefly and give the corrected "
                   "command. If it worked and steps remain, give only the next "
                   "single command, chosen from what this output shows. If the "
                   "task is complete, reply DONE with a short summary and give "
                   "no code block.")
        payload = f"{head}{body}{note}{warn}\n\n{ask}"
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

        self._dispatch(payload, display, short=short, agent=in_chain)

    def _check_progress(self, command: str, output: str, status: str):
        """Log a step and decide whether the chain is still getting anywhere.

        Returns (why, warn): why is set when auto-run must stop, warn is a
        note for the model when it is starting to go round in circles. The
        old check only looked at failures, so a command that succeeded but
        was re-run forever (a status check, a cat, an ls) never stopped.
        """
        failed = looks_failed(output)
        key, sig = normalise_cmd(command), output_signature(output)
        earlier = [s for s in self._chain_log if s["key"] == key]
        same = [s for s in earlier if s["sig"] == sig]
        self._chain_log.append({
            "cmd": command, "key": key, "sig": sig, "failed": failed,
            "result": ("failed" if failed else
                       "unfinished" if status in ("timeout", "stalled") else "ok"),
            "summary": summarize_output(output),
            "step": len(self._chain_log) + 1})
        streak = 0
        for s in reversed(self._chain_log):
            if not s["failed"]:
                break
            streak += 1

        if failed and earlier:
            return "the same command failed again", ""
        if streak >= 3:
            return f"{streak} commands in a row failed", ""
        if len(same) >= 2:
            return (f"`{clip(command, 60)}` returned the same output "
                    f"{len(same) + 1} times — no progress"), ""
        period = _cycle_period([(s["key"], s["sig"]) for s in self._chain_log])
        if period:
            return (f"the last {period} commands keep repeating with the same "
                    "results — it is going round in circles"), ""
        if same:
            steps = ", ".join(str(s["step"]) for s in same)
            return "", (
                f"\n\nNOTE: this exact command already ran at step {steps} "
                "and gave the same output. Running it again will not change "
                "anything. Act on what the output says: take a different next "
                "step, or reply DONE if the goal is met or you are stuck.")
        return "", ""

    def _reset_chain(self):
        self._chain_log = []
        self._nudged = set()

    # The parts of the step-by-step prompt, in characters.
    GOAL_CHARS = 4000
    BACKGROUND_CHARS = 3000
    LOG_STEPS = 30
    PREV_REPLY_CHARS = 1500

    def _chain_goal(self) -> tuple:
        """(goal, background): the request this chain works on, and what came
        before it. The goal is the last message the user typed."""
        idx = next((i for i in range(len(self.messages) - 1, -1, -1)
                    if self.messages[i].get("typed")), None)
        if idx is None:
            idx = next((i for i, m in enumerate(self.messages)
                        if m["role"] == "user"), None)
        if idx is None:
            return "", ""
        goal = clip(self.messages[idx]["content"].replace(AGENT_RULES, "").strip(),
                    self.GOAL_CHARS)
        before, used = [], 0
        for m in reversed(self.messages[:idx]):
            text = clip(m.get("short", m["content"]).replace(AGENT_RULES, "").strip(), 600)
            if used + len(text) > self.BACKGROUND_CHARS:
                break
            before.append(f"{'Me' if m['role'] == 'user' else 'You'}: {text}")
            used += len(text)
        return goal, "\n\n".join(reversed(before))

    def _agent_request(self, latest: str) -> list:
        """The request for one auto-run step, rebuilt from state every time.

        Replaying the whole chat made each step longer than the last, until
        the newest output was buried under (or, on servers that silently
        truncate, cut away with) dozens of near-identical turns. The model
        then answered an older turn and ran the same command again and
        again. Instead every step sends a fixed-shape prompt: the goal, a
        one-line-per-step log of what has run, the model's last note, and
        the newest output in full, last, where it cannot be missed.
        """
        goal, background = self._chain_goal()
        parts = ["[Auto-run step. This message is rebuilt every step from the "
                 "task, the log of commands run so far, and the newest output. "
                 "Base your answer on the NEWEST OUTPUT section.]"]
        if goal:
            parts.append(f"## Task (what I asked for)\n{goal}")
        if background:
            parts.append(f"## Earlier in our conversation (abridged)\n{background}")
        # The newest step is shown in full below, so the log stops before it.
        log = self._chain_log[:-1] if latest.startswith("I ran this") else self._chain_log
        if log:
            shown = log[-self.LOG_STEPS:]
            lines = []
            if len(log) > len(shown):
                lines.append(f"(steps 1–{len(log) - len(shown)} not listed)")
            counts = Counter(s["key"] for s in log)
            for s in shown:
                again = (f"  ← run {counts[s['key']]}× in total"
                         if counts[s["key"]] > 1 else "")
                lines.append(f"{s['step']}. `{clip(s['cmd'], 200)}` → "
                             f"{s['result']}: {s['summary']}{again}")
            parts.append("## Commands already run for this task (oldest first)\n"
                         + "\n".join(lines))
        prev = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "assistant"), "")
        if prev and log:
            parts.append("## Your previous reply (abridged)\n"
                         + clip(prev, self.PREV_REPLY_CHARS))
        parts.append(f"## NEWEST OUTPUT\n{latest}")
        return [{"role": "user", "content": "\n\n".join(parts)}]

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
        """The history as sent: fitted to the model's context window.

        Older command outputs and attachments are cut down to a summary, then
        the oldest turns are dropped until the whole thing fits the context
        budget. A request bigger than the model's window does not always fail
        cleanly: some servers silently cut the start (including the chat
        template) and the model answers with gibberish.
        """
        # Tool results of the round in progress are always sent whole.
        last_user = max((i for i, m in enumerate(self.messages)
                         if m["role"] == "user"), default=-1)
        results = [i for i, m in enumerate(self.messages)
                   if "short" in m and not (m["role"] == "tool" and i > last_user)]
        old = set(results[:-self.KEEP_FULL_RESULTS])
        msgs = [self._api_view(m, i in old) for i, m in enumerate(self.messages)]

        budget = (self.context_budget - len(self.cfg.get("system_prompt", ""))
                  - len(self._memory) - self._tools_prompt_chars())
        dropped = 0
        while len(msgs) > 1 and sum(_msg_chars(m) for m in msgs) > budget:
            msgs.pop(0)
            dropped += 1
            # A conversation must start with a user turn.
            while len(msgs) > 1 and msgs[0]["role"] != "user":
                msgs.pop(0)
                dropped += 1
        if dropped:
            msgs[0] = {"role": "user", "content":
                       f"(Earlier conversation — {dropped} messages — was left "
                       f"out to fit your context window.)\n\n{msgs[0]['content']}"}
            if dropped != self._last_trim:
                self.notice.emit(f"Oldest {dropped} messages left out to fit the "
                                 "model's context window")
        self._last_trim = dropped
        return msgs

    def _dispatch(self, payload: str, display: str, short: str = None,
                  typed: bool = False, agent: bool = False):
        """Append a user turn and start a request.

        agent: an auto-run step. The full turn still goes into the history
        (for the transcript, saving and later questions), but the request is
        the compact step prompt from _agent_request instead of the replay.
        """
        self._clear_blocks()
        msg = {"role": "user", "content": payload}
        if short:
            msg["short"] = short
        if typed:
            msg["typed"] = True
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

        self._agent_turn = agent
        api = self._agent_request(payload) if agent else self._api_messages()
        self._start_worker(api)

    def _start_worker(self, api: list, no_tools: bool = False):
        mode = "off" if no_tools else self._tool_mode()
        tools = self._offered_tools() if mode != "off" else []
        self.worker = ChatWorker(self._request_cfg(), api, self,
                                 tools=tools, tool_mode=mode)
        self.worker.chunk.connect(self._on_chunk)
        self.worker.thinking.connect(self._on_thinking)
        self.worker.finished_ok.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.fell_back.connect(self._on_fell_back)
        self.worker.start()

    def _stale(self, current) -> bool:
        """True when a signal comes from a thread that has been replaced or
        cancelled. Its events can still be queued after it was let go."""
        sender = self.sender()
        return sender is not None and sender is not current

    def _retire(self, worker):
        """Cancel a thread and keep it referenced until it really ends;
        dropping the last reference to a running QThread crashes Qt. Its
        queued signals are ignored by _stale()."""
        worker.cancel()
        if worker.wait(300):
            return
        self._retired.append(worker)
        worker.finished.connect(
            lambda w=worker: self._retired.remove(w) if w in self._retired else None)

    # ------------------------------------------------------------ MCP tools

    def _tool_mode(self) -> str:
        """How tools are offered on the next request: native | text | auto | off."""
        mode = self.cfg.get("tool_mode", "auto")
        if mode not in TOOL_MODES or self.mcp is None or not self._offered_tools():
            return "off"
        if mode == "auto" and self._model_key() in self._text_only:
            return "text"
        return mode

    def _model_key(self):
        return (self.cfg.get("base_url", ""), self.cfg.get("model", ""))

    def _offered_tools(self) -> list:
        if self.mcp is None or self.cfg.get("tool_mode", "auto") == "off":
            return []
        return [t for t in self.mcp.tools() if tool_policy(self.cfg, t) != "never"]

    def _tools_prompt_chars(self) -> int:
        tools = self._offered_tools() if self._tool_mode() != "off" else []
        return len(text_tool_prompt(tools)) if tools else 0

    @staticmethod
    def _api_view(m: dict, use_short: bool) -> dict:
        """A history entry as sent: the neutral fields only."""
        out = {"role": m["role"],
               "content": m["short"] if use_short and "short" in m else m["content"]}
        for key in ("tool_calls", "tool_call_id", "name", "is_error"):
            if key in m:
                out[key] = m[key]
        return out

    def _on_fell_back(self, reason: str):
        if self._stale(self.worker):
            return
        self._text_only.add(self._model_key())
        self.notice.emit("The server refused native tool calls — using the text "
                         "protocol for this model")
        self._update_tools_button()

    def _on_tool_reply(self, role, inline, content, calls):
        """The model asked for tools: record the request, then run them."""
        shown = (f"<think>{inline}</think>" if inline else "") + (
            content or "*(calls a tool)*")
        self._display[-1] = (role, shown)
        self.messages.append({
            "role": "assistant", "content": content,
            "tool_calls": [{"id": c["id"], "name": c["name"],
                            "arguments": c.get("arguments") or {}} for c in calls]})
        saved = memory.add_notes(content)
        if saved:
            self.notice.emit("Remembered: " + "; ".join(saved)[:120])
        self.streaming = False
        self.stop()
        self._start_tools(calls)

    def _rounds_this_turn(self) -> int:
        n = 0
        for m in reversed(self.messages):
            if m["role"] == "user":
                break
            if m["role"] == "assistant" and m.get("tool_calls"):
                n += 1
        return n

    def _start_tools(self, calls: list):
        limit = max(1, int(self.cfg.get("mcp_max_rounds", 10)))
        over = self._rounds_this_turn() > limit
        self._tools_busy = True
        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self._display.append(("tool", ""))
        self._tool_round = {"calls": calls, "results": {}, "over": over,
                            "index": len(self._display) - 1, "cancelled": False}
        results = self._tool_round["results"]
        self._render_tools(follow=True)

        jobs = []
        for call in calls:
            if over:
                results[call["id"]] = (
                    f"Not run: the limit of {limit} tool calls for one message "
                    "was reached. Answer now with what you have, without "
                    "calling more tools.", True)
                continue
            if call.get("error"):
                results[call["id"]] = (call["error"] + " Write the call again "
                                       "correctly.", True)
                continue
            tool = self.mcp.find(call["name"]) if self.mcp else None
            if tool is None or tool_policy(self.cfg, tool) == "never":
                names = ", ".join(t.key for t in self._offered_tools()) or "none"
                results[call["id"]] = (f"There is no tool named "
                                       f"“{call['name']}”. Available: {names}.", True)
                continue
            if not self._approve(call, tool):
                results[call["id"]] = ("I declined to run this tool call. Take "
                                       "another approach, or ask me.", True)
                continue
            if self._tool_round is None or self._tool_round["cancelled"]:
                return              # stopped while the dialog was open
            jobs.append((call, tool))
        self._render_tools()

        if not jobs:
            self._tools_finished()
            return
        timeout = float(self.cfg.get("mcp_tool_timeout_s", 120))
        self._tool_worker = ToolWorker(self.mcp, jobs, timeout, self)
        self._tool_worker.result.connect(self._on_tool_result)
        self._tool_worker.finished.connect(self._on_tools_thread_done)
        self._tool_worker.start()

    def _approve(self, call: dict, tool) -> bool:
        """Ask before running, unless the tool is allowed and the arguments
        do not look destructive."""
        args = _pretty_args(call.get("arguments"))
        risky = is_destructive(args)
        if tool_policy(self.cfg, tool) == "allow" and not risky:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Run tool?")
        box.setIcon(QMessageBox.Warning if risky or tool.destructive
                    else QMessageBox.Question)
        desc = " ".join(tool.description.split())
        if len(desc) > 300:
            desc = desc[:300] + "…"
        warn = ("\n\nThese arguments look hard to undo. Read them carefully."
                if risky else
                "\n\nThe server says this tool can delete or overwrite data."
                if tool.destructive else "")
        box.setText(f"The assistant wants to run “{tool.name}” from the "
                    f"{tool.server} server.{warn}")
        box.setInformativeText(f"{desc}\n\nArguments:\n{clip(args, 1500)}"
                               if desc else f"Arguments:\n{clip(args, 1500)}")
        run = box.addButton("Run", QMessageBox.AcceptRole)
        always = None
        if not risky:
            always = box.addButton("Always allow this tool", QMessageBox.AcceptRole)
        no = box.addButton("Don't run", QMessageBox.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        clicked = box.clickedButton()
        if always is not None and clicked is always:
            self._set_policy(tool.key, "allow")
            return True
        return clicked is run

    def _on_tool_result(self, call_id: str, text: str, is_error: bool):
        if self._tool_round is None or self._stale(self._tool_worker):
            return
        self._tool_round["results"][call_id] = (text, is_error)
        self._render_tools()

    def _on_tools_thread_done(self):
        if self._tool_worker is None or self._stale(self._tool_worker):
            return
        self._tool_worker = None
        self._tools_finished()

    def _tools_finished(self):
        """Every call in the round has a result: record them and go on."""
        rnd = self._tool_round
        if rnd is None or rnd["cancelled"]:
            return
        for call in rnd["calls"]:
            if call["id"] not in rnd["results"]:
                rnd["results"][call["id"]] = ("No result — the tool did not "
                                              "return.", True)
        self._record_results(rnd)
        self._tool_round = None
        self._tools_busy = False
        self._render_tools_final(rnd)
        self._continue(no_tools=rnd["over"])

    def _record_results(self, rnd):
        for call in rnd["calls"]:
            text, is_error = rnd["results"][call["id"]]
            full = text or "(empty)"
            sent, cut = fit_output(full, self.output_budget)
            if cut:
                sent = f"{sent}\n\n{cut}"
            self.messages.append({
                "role": "tool", "tool_call_id": call["id"],
                "name": call["name"], "content": sent, "is_error": is_error,
                "short": clip(full, 800)})
            self._run_log.append({
                "command": f"[tool] {call['name']} "
                           f"{json.dumps(call.get('arguments') or {}, ensure_ascii=False)}",
                "result": "failed" if is_error else "ok", "time": time.time()})
        self.save_chat()

    def _cancel_tools(self):
        """Stop a round of tool calls. Unfinished calls get a result saying
        so, because every call in the history must have one."""
        rnd = self._tool_round
        if self._tool_worker is not None:
            self._retire(self._tool_worker)
            self._tool_worker = None
        if rnd is None:
            self._tools_busy = False
            return
        rnd["cancelled"] = True
        self._tool_round = None
        self._tools_busy = False
        for call in rnd["calls"]:
            rnd["results"].setdefault(call["id"], ("Cancelled by me before it "
                                                   "finished.", True))
        self._record_results(rnd)
        self._render_tools_final(rnd)

    def _continue(self, no_tools: bool = False):
        """Ask the model to carry on after a round of tool results."""
        self._display.append(("assistant", ""))
        self.streaming = True
        self._req_started = time.monotonic()
        self._answer_started = 0.0
        self._render(follow=True)
        self.send_btn.setVisible(False)
        self.stop_btn.setVisible(True)
        self._wait_timer.start(1000)
        if self._agent_turn:
            last = max(i for i, m in enumerate(self.messages) if m["role"] == "user")
            api = (self._agent_request(self.messages[last]["content"]) +
                   [self._api_view(m, False) for m in self.messages[last + 1:]])
        else:
            api = self._api_messages()
        self._start_worker(api, no_tools=no_tools)

    def _tool_text(self, rnd, running: bool) -> str:
        parts = []
        for call in rnd["calls"]:
            res = rnd["results"].get(call["id"])
            name = call.get("name") or "?"
            tool = self.mcp.find(name) if self.mcp else None
            label = f"`{tool.name}` · {tool.server}" if tool else f"`{name}`"
            if res is None:
                state = "*running…*" if running else "*waiting*"
            elif res[1]:
                state = "**error**"
            else:
                state = "done"
            block = [f"**Tool** {label} — {state}"]
            args = call.get("arguments")
            if args:
                block.append(f"```json\n{clip(_pretty_args(args), 600)}\n```")
            if res is not None:
                text = res[0] or "(empty)"
                shown = (text if len(text) <= self.TOOL_PREVIEW_CHARS
                         else text[:self.TOOL_PREVIEW_CHARS].rsplit("\n", 1)[0]
                         + f"\n… ({len(text):,} characters in all)")
                block.append(f"```\n{shown}\n```")
            parts.append("\n".join(block))
        return "\n\n".join(parts)

    TOOL_PREVIEW_CHARS = 1500   # of each tool result, in the transcript

    def _render_tools(self, follow=None):
        rnd = self._tool_round
        if rnd is None:
            return
        self._display[rnd["index"]] = ("tool", self._tool_text(rnd, True))
        self._render(follow=follow)

    def _render_tools_final(self, rnd):
        if rnd["index"] < len(self._display):
            self._display[rnd["index"]] = ("tool", self._tool_text(rnd, False))
        self.send_btn.setVisible(True)
        self.stop_btn.setVisible(False)
        self._render()

    # Tools menu --------------------------------------------------------

    def _set_policy(self, key: str, policy: str):
        policies = dict(self.cfg.get("mcp_tool_policy") or {})
        policies[key] = policy
        self.cfg["mcp_tool_policy"] = policies
        try:
            save_config(self.cfg)
        except OSError as exc:
            self.notice.emit(f"Could not save the tool setting: {exc}")
        self._update_tools_button()

    def _update_tools_button(self):
        if not hasattr(self, "tools_btn"):
            return
        if self.mcp is None or not self.mcp.states():
            self.tools_btn.setText("Tools")
            self.tools_btn.setToolTip("No MCP servers. Add them in Settings → "
                                      "MCP tools.")
            return
        n = len(self._offered_tools())
        failed = any(s.status == "error" for s in self.mcp.states())
        self.tools_btn.setText(f"Tools ({n})" + (" ⚠" if failed else ""))
        self.tools_btn.setToolTip(self.mcp.summary())

    def _fill_tools_menu(self):
        menu = self.tools_menu
        menu.clear()
        mode = self.cfg.get("tool_mode", "auto")
        states = self.mcp.states() if self.mcp else []
        head = menu.addAction(self.mcp.summary() if states else
                              "No MCP servers yet")
        head.setEnabled(False)
        if states:
            how = {"auto": "Auto", "native": "Native", "text": "Text protocol",
                   "off": "Off"}.get(mode, mode)
            if mode == "auto" and self._model_key() in self._text_only:
                how += " — using text protocol for this model"
            menu.addAction(f"Tool calls: {how}").setEnabled(False)
        menu.addSeparator()
        labels = {"allow": "Allow — run without asking",
                  "ask": "Ask each time",
                  "never": "Never — hide from the assistant"}
        short = {"allow": "allow", "ask": "ask", "never": "never"}
        for st in states:
            status = {"ready": f"{len(st.tools)} tools", "error": "failed",
                      "connecting": "connecting…", "disabled": "disabled",
                      "off": "stopped"}.get(st.status, st.status)
            sub = menu.addMenu(f"{st.name}  ({status})")
            if st.error:
                err = sub.addAction(clip(st.error, 200))
                err.setEnabled(False)
            for tool in st.tools:
                current = tool_policy(self.cfg, tool)
                tmenu = sub.addMenu(f"{tool.name}   [{short[current]}]")
                if tool.description:
                    d = tmenu.addAction(clip(" ".join(tool.description.split()), 160))
                    d.setEnabled(False)
                    tmenu.addSeparator()
                group = QActionGroup(tmenu)
                group.setExclusive(True)
                for key, label in labels.items():
                    act = tmenu.addAction(label)
                    act.setCheckable(True)
                    act.setChecked(key == current)
                    group.addAction(act)
                    act.triggered.connect(
                        lambda _=False, k=tool.key, p=key: self._set_policy(k, p))
        if states:
            menu.addSeparator()
            menu.addAction("Reconnect servers").triggered.connect(self._reconnect)
        menu.addAction("MCP settings…").triggered.connect(self.open_settings.emit)

    def _reconnect(self):
        if self.mcp is not None:
            self.mcp.reconnect()
            self.notice.emit("Reconnecting MCP servers…")

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


class MemoryDialog(QDialog):
    """Shows what the assistant remembers, and lets the notes be edited."""

    def __init__(self, sessions: int, current_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Memory")
        self.setMinimumSize(620, 560)
        self.cleared = False
        self._sessions, self._current = sessions, current_id

        notes_label = QLabel(
            "<b>Saved notes</b> — facts the assistant keeps across chats. It "
            "adds to these itself (lines it starts with REMEMBER:); edit or "
            "delete freely, one per line.")
        notes_label.setWordWrap(True)
        self.notes = QPlainTextEdit(memory.load_notes())
        self.notes.setPlaceholderText(
            "- web01 is reached with: ssh admin@10.0.0.5 -p 2222\n"
            "- Prefer apt over snap on the Ubuntu hosts")

        preview_label = QLabel(
            "<b>What a new chat starts with</b> — notes plus a digest of your "
            "most recent chats (set how many in Settings → Chat → Memory).")
        preview_label.setWordWrap(True)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self._refresh_preview()

        clear_btn = QPushButton("Clear chat history…")
        clear_btn.clicked.connect(self._clear)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(clear_btn)
        row.addStretch(1)
        row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(notes_label)
        layout.addWidget(self.notes, 1)
        layout.addWidget(preview_label)
        layout.addWidget(self.preview, 2)
        note = QLabel(f"Stored in {memory.chats_dir().parent}")
        note.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(note)
        layout.addLayout(row)

    def _refresh_preview(self):
        text = memory.memory_prompt(self._sessions, exclude=self._current)
        if self._sessions <= 0:
            text = "Memory is off. Turn it on in Settings → Chat → Memory."
        self.preview.setPlainText(text or "Nothing remembered yet.")

    def _save(self):
        try:
            memory.save_notes(self.notes.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Could not save notes", str(exc))
            return
        self.accept()

    def _clear(self):
        if QMessageBox.question(
                self, "Clear chat history?",
                "Delete every saved chat? Saved notes are kept.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        n = memory.clear_history()
        self.cleared = True
        self._refresh_preview()
        QMessageBox.information(self, "History cleared",
                                f"Deleted {n} saved chat{'s' if n != 1 else ''}.")
