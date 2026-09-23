"""Configuration storage and the settings dialog."""

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QVBoxLayout,
)

APP_NAME = "AI Terminal"

# Preset window sizes, keyed by id. "maximized" has no width/height — the
# window starts (or switches to) maximized instead of a fixed size.
WINDOW_SIZES = {
    "compact": {"label": "Compact (1100 × 700)", "width": 1100, "height": 700},
    "standard": {"label": "Standard (1400 × 800)", "width": 1400, "height": 800},
    "large": {"label": "Large (1700 × 950)", "width": 1700, "height": 950},
    "xlarge": {"label": "Extra large (2000 × 1150)", "width": 2000, "height": 1150},
    "maximized": {"label": "Maximized", "width": None, "height": None},
}


def window_size(preset_id: str) -> dict:
    return WINDOW_SIZES.get(preset_id, WINDOW_SIZES["standard"])

# Presets keyed by provider id. base_url/model are starting points the user can edit.
PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "api": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_hint": "sk-...",
    },
    "anthropic": {
        "label": "Anthropic",
        "api": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-5",
        "key_hint": "sk-ant-...",
    },
    "azure": {
        "label": "Azure OpenAI",
        "api": "openai",
        "base_url": "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
        "model": "your-deployment-name",
        "key_hint": "Azure API key",
    },
    "openrouter": {
        "label": "OpenRouter",
        "api": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "key_hint": "sk-or-...",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_hint": "sk-...",
    },
    "groq": {
        "label": "Groq",
        "api": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_hint": "gsk_...",
    },
    "ollama": {
        "label": "Ollama (local)",
        "api": "ollama",
        "base_url": "http://localhost:11434",
        "model": "llama3.1",
        "key_hint": "not required",
    },
    "lmstudio": {
        "label": "LM Studio (local)",
        "api": "openai",
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "key_hint": "not required",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "api": "openai",
        "base_url": "http://localhost:8000/v1",
        "model": "",
        "key_hint": "optional",
    },
}

DEFAULTS = {
    "provider": "openai",
    "api_style": "openai",          # openai | anthropic | ollama
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o-mini",
    "temperature": 0.7,
    "max_tokens": 2048,
    "stream": True,
    "system_prompt": (
        "You are a helpful assistant embedded next to a Windows command prompt "
        "that may be connected to a remote Linux host over SSH. Match your "
        "command syntax to whichever shell the terminal output shows. Keep "
        "answers short. When you want a command run, put exactly one command "
        "or script in a single fenced code block, and nothing the user should "
        "not execute. When you are shown the output of a command, say plainly "
        "whether it worked before moving on."
    ),
    "shell": "cmd.exe",
    "window_size": "standard",
    "font_family": "Consolas",
    "font_size": 11,
    "chat_font_size": 10,
    "scrollback": 5000,
    "warn_multiline_paste": True,
    # Reported to programs run in the shell, including over SSH. Set to "" to
    # leave it unset, or "vt100" if a full-screen remote program misbehaves.
    "term": "xterm-256color",
    # Result checking: how long the shell must be quiet before a command is
    # considered finished, and the hard ceiling on waiting for one.
    # capture_idle_ms is how often a quiet shell is checked for its prompt;
    # a command only counts as finished once the prompt is back. With no
    # prompt and no output for capture_stall_s, auto-run pauses.
    "capture_idle_ms": 900,
    "capture_stall_s": 30,
    "capture_timeout_s": 180,
    # Auto-run: most command→read→decide steps before handing control back.
    "auto_run_max_steps": 20,
    # Most terminal output sent to the model in one message. Beyond it the
    # middle is left out and the model is told which lines are missing.
    "ai_output_chars": 24000,
}

AI_OUTPUT_LIMITS = {
    8000: "Compact — 8,000 characters (small/local models)",
    24000: "Standard — 24,000 characters",
    60000: "Large — 60,000 characters",
    200000: "Full — 200,000 characters (large-context models)",
}

AUTO_RUN_LIMITS = {
    5: "5 steps — short fixes",
    10: "10 steps",
    20: "20 steps — default",
    40: "40 steps — long jobs",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = Path(base) / "AITerminal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    with open(config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


class SettingsDialog(QDialog):
    """Everything the app needs to reach a model, plus terminal appearance."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.cfg = dict(cfg)

        # --- AI provider -------------------------------------------------
        ai_box = QGroupBox("AI provider")
        form = QFormLayout(ai_box)

        self.provider = QComboBox()
        for pid, meta in PROVIDERS.items():
            self.provider.addItem(meta["label"], pid)
        idx = self.provider.findData(self.cfg.get("provider", "openai"))
        self.provider.setCurrentIndex(max(idx, 0))
        self.provider.currentIndexChanged.connect(self._apply_preset)

        self.base_url = QLineEdit(self.cfg.get("base_url", ""))
        self.api_key = QLineEdit(self.cfg.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.Password)
        self.model = QLineEdit(self.cfg.get("model", ""))

        show_key = QPushButton("Show")
        show_key.setCheckable(True)
        show_key.setFixedWidth(60)
        show_key.toggled.connect(
            lambda on: self.api_key.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key)
        key_row.addWidget(show_key)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setValue(float(self.cfg.get("temperature", 0.7)))

        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(64, 32768)
        self.max_tokens.setSingleStep(256)
        self.max_tokens.setValue(int(self.cfg.get("max_tokens", 2048)))

        self.system_prompt = QPlainTextEdit(self.cfg.get("system_prompt", ""))
        self.system_prompt.setFixedHeight(90)

        form.addRow("Provider", self.provider)
        form.addRow("Base URL", self.base_url)
        form.addRow("API key", key_row)
        form.addRow("Model", self.model)
        form.addRow("Temperature", self.temperature)
        form.addRow("Max tokens", self.max_tokens)
        form.addRow("System prompt", self.system_prompt)

        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        form.addRow("", self._wrap(test_row))

        # --- Window ----------------------------------------------------
        window_box = QGroupBox("Window")
        wform = QFormLayout(window_box)

        self.window_size = QComboBox()
        for pid, meta in WINDOW_SIZES.items():
            self.window_size.addItem(meta["label"], pid)
        idx = self.window_size.findData(self.cfg.get("window_size", "standard"))
        self.window_size.setCurrentIndex(max(idx, 0))

        wform.addRow("Size", self.window_size)

        # --- Chat ----------------------------------------------------------
        chat_box = QGroupBox("Chat")
        cform = QFormLayout(chat_box)

        self.chat_font_size = QSpinBox()
        self.chat_font_size.setRange(7, 28)
        self.chat_font_size.setValue(int(self.cfg.get("chat_font_size", 10)))

        self.auto_steps = QComboBox()
        for n, label in AUTO_RUN_LIMITS.items():
            self.auto_steps.addItem(label, n)
        idx = self.auto_steps.findData(int(self.cfg.get("auto_run_max_steps", 20)))
        self.auto_steps.setCurrentIndex(idx if idx >= 0 else 2)
        self.auto_steps.setToolTip(
            "How many commands auto-run may execute on its own, each after "
            "reading the previous output, before it pauses and hands back.")

        cform.addRow("Font size", self.chat_font_size)
        self.ai_output = QComboBox()
        for n, label in AI_OUTPUT_LIMITS.items():
            self.ai_output.addItem(label, n)
        idx = self.ai_output.findData(int(self.cfg.get("ai_output_chars", 24000)))
        self.ai_output.setCurrentIndex(idx if idx >= 0 else 1)
        self.ai_output.setToolTip(
            "How much terminal output goes to the assistant per message. Longer "
            "output keeps its start and end; the assistant is told which lines "
            "were left out so it can ask for them with grep, sed or tail.\n"
            "Pick a size your model's context window can hold.")

        cform.addRow("Auto-run limit", self.auto_steps)
        cform.addRow("Output sent to AI", self.ai_output)

        # --- Terminal ----------------------------------------------------
        term_box = QGroupBox("Terminal")
        tform = QFormLayout(term_box)

        self.shell = QComboBox()
        self.shell.setEditable(True)
        for s in ("cmd.exe", "powershell.exe",
                  "pwsh.exe", "wsl.exe"):
            self.shell.addItem(s)
        self.shell.setCurrentText(self.cfg.get("shell", "cmd.exe"))

        self.font_family = QComboBox()
        self.font_family.setEditable(True)
        for f in ("Consolas", "Cascadia Mono", "Lucida Console",
                  "Courier New", "Fira Code"):
            self.font_family.addItem(f)
        self.font_family.setCurrentText(self.cfg.get("font_family", "Consolas"))

        self.font_size = QSpinBox()
        self.font_size.setRange(7, 28)
        self.font_size.setValue(int(self.cfg.get("font_size", 11)))

        self.scrollback = QSpinBox()
        self.scrollback.setRange(200, 50000)
        self.scrollback.setSingleStep(500)
        self.scrollback.setValue(int(self.cfg.get("scrollback", 5000)))

        self.warn_paste = QCheckBox(
            "Ask before pasting more than one line")
        self.warn_paste.setChecked(bool(self.cfg.get("warn_multiline_paste", True)))
        self.warn_paste.setToolTip(
            "Only applies when the shell lacks bracketed paste (cmd.exe, "
            "PowerShell): the lines then run one by one as each finishes. "
            "bash/zsh over SSH take the block whole and wait for Enter.")

        tform.addRow("Shell", self.shell)
        tform.addRow("Font", self.font_family)
        tform.addRow("Font size", self.font_size)
        tform.addRow("Scrollback lines", self.scrollback)
        tform.addRow("Paste safety", self.warn_paste)
        tform.addRow("", QLabel("Right-click copies the selection, or pastes when "
                                "nothing is selected."))
        tform.addRow("", QLabel("Shell and scrollback apply to new terminal sessions."))

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(ai_box)
        layout.addWidget(window_box)
        layout.addWidget(chat_box)
        layout.addWidget(term_box)
        note = QLabel(f"Saved to {config_path()} — the API key is stored in plain text.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(note)
        layout.addWidget(buttons)

    @staticmethod
    def _wrap(layout):
        from PySide6.QtWidgets import QWidget
        w = QWidget()
        w.setLayout(layout)
        return w

    def _apply_preset(self):
        pid = self.provider.currentData()
        meta = PROVIDERS[pid]
        self.base_url.setText(meta["base_url"])
        self.model.setText(meta["model"])
        self.api_key.setPlaceholderText(meta["key_hint"])

    def _test(self):
        from ai_client import test_connection
        self.test_result.setText("Testing…")
        self.test_btn.setEnabled(False)
        self.test_result.repaint()
        ok, msg = test_connection(self.values())
        self.test_btn.setEnabled(True)
        self.test_result.setText(msg)
        self.test_result.setStyleSheet(
            "color:#4caf50;" if ok else "color:#e57373;")

    def values(self) -> dict:
        pid = self.provider.currentData()
        cfg = dict(self.cfg)
        cfg.update({
            "provider": pid,
            "api_style": PROVIDERS[pid]["api"],
            "base_url": self.base_url.text().strip().rstrip("/"),
            "api_key": self.api_key.text().strip(),
            "model": self.model.text().strip(),
            "temperature": self.temperature.value(),
            "max_tokens": self.max_tokens.value(),
            "system_prompt": self.system_prompt.toPlainText(),
            "window_size": self.window_size.currentData(),
            "chat_font_size": self.chat_font_size.value(),
            "auto_run_max_steps": self.auto_steps.currentData(),
            "ai_output_chars": self.ai_output.currentData(),
            "shell": self.shell.currentText().strip(),
            "font_family": self.font_family.currentText().strip(),
            "font_size": self.font_size.value(),
            "scrollback": self.scrollback.value(),
            "warn_multiline_paste": self.warn_paste.isChecked(),
        })
        return cfg


def ask_settings(cfg: dict, parent=None):
    dlg = SettingsDialog(cfg, parent)
    if dlg.exec() == QDialog.Accepted:
        new = dlg.values()
        try:
            save_config(new)
        except OSError as exc:
            QMessageBox.warning(parent, "Could not save settings", str(exc))
        return new
    return None
