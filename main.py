"""AI Terminal — a Windows command prompt on the left, an assistant on the right.

Run:  python main.py
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from chat import ChatPanel
from config import ask_settings, load_config, window_size
from terminal import TerminalWidget

DARK_QSS = """
QMainWindow, QWidget { background:#151518; color:#e6e6e6; }
QLabel { color:#e6e6e6; }
QPushButton {
    background:#26262c; border:1px solid #35353d; border-radius:5px;
    padding:5px 12px; color:#e6e6e6;
}
QPushButton:hover { background:#31313a; }
QPushButton:pressed { background:#1f1f25; }
QPushButton:disabled { color:#6b6b74; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background:#1b1b1f; border:1px solid #2e2e33; border-radius:5px;
    padding:4px; color:#e6e6e6; selection-background-color:#3a96dd;
}
QGroupBox {
    border:1px solid #2e2e33; border-radius:6px; margin-top:14px; padding-top:10px;
}
QGroupBox::title { subcontrol-origin:margin; left:10px; color:#9aa0a6; }
QSplitter::handle { background:#2e2e33; width:3px; }
QMenuBar, QMenu { background:#1b1b1f; color:#e6e6e6; }
QMenu::item:selected, QMenuBar::item:selected { background:#31313a; }
QCheckBox { color:#c9c9d1; }
QScrollBar:vertical { background:#1b1b1f; width:12px; margin:0; border:none; }
QScrollBar::handle:vertical {
    background:#4a4a56; border-radius:6px; min-height:28px; margin:2px;
}
QScrollBar::handle:vertical:hover { background:#5e5e6d; }
QScrollBar:horizontal { background:#1b1b1f; height:12px; margin:0; border:none; }
QScrollBar::handle:horizontal {
    background:#4a4a56; border-radius:6px; min-width:28px; margin:2px;
}
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("AI Terminal")
        size = window_size(self.cfg.get("window_size", "standard"))
        if size["width"]:
            self.resize(size["width"], size["height"])

        # ---- left: terminal ------------------------------------------
        self.terminal = TerminalWidget(self.cfg)
        self.shell_label = QLabel(self.cfg.get("shell", "cmd.exe"))
        self.shell_label.setStyleSheet("color:#9aa0a6;")
        restart_btn = QPushButton("Restart shell")
        restart_btn.setFixedWidth(110)
        restart_btn.clicked.connect(self.restart_shell)

        term_header = QHBoxLayout()
        term_header.addWidget(QLabel("Terminal"))
        term_header.addWidget(self.shell_label, 1)
        term_header.addWidget(restart_btn)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addLayout(term_header)
        left_layout.addWidget(self.terminal, 1)

        # ---- right: chat ---------------------------------------------
        self.chat = ChatPanel(self.cfg, self.terminal)
        self.chat.open_settings.connect(self.edit_settings)
        self.chat.run_in_terminal.connect(self.terminal.run_command)
        self.terminal.command_output.connect(self.chat.on_command_output)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.chat)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([840, 560])
        self.setCentralWidget(splitter)

        self.terminal.title_changed.connect(self.shell_label.setText)
        self.status = self.statusBar()
        self.status.setStyleSheet("color:#9aa0a6;")
        self.status.showMessage(
            "Right-click copies the selection, or pastes when nothing is selected.",
            8000)
        self.terminal.notice.connect(lambda msg: self.status.showMessage(msg, 2500))
        self.chat.notice.connect(lambda msg: self.status.showMessage(msg, 2500))
        self._build_menu()
        self.terminal.setFocus()

    # ------------------------------------------------------------ menus

    def _build_menu(self):
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        settings = QAction("Settings…", self)
        settings.setShortcut(QKeySequence("Ctrl+,"))
        settings.triggered.connect(self.edit_settings)
        quit_action = QAction("Exit", self)
        quit_action.setShortcut(QKeySequence("Alt+F4"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(settings)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        term_menu = bar.addMenu("&Terminal")
        restart = QAction("Restart shell", self)
        restart.setShortcut(QKeySequence("Ctrl+Shift+R"))
        restart.triggered.connect(self.restart_shell)
        clear = QAction("Clear screen", self)
        clear.triggered.connect(lambda: self.terminal.run_command("cls"))
        focus_term = QAction("Focus terminal", self)
        focus_term.setShortcut(QKeySequence("Ctrl+1"))
        focus_term.triggered.connect(self.terminal.setFocus)
        term_menu.addAction(restart)
        term_menu.addAction(clear)
        term_menu.addAction(focus_term)

        chat_menu = bar.addMenu("&Chat")
        new_chat = QAction("New chat", self)
        new_chat.setShortcut(QKeySequence("Ctrl+N"))
        new_chat.triggered.connect(self.chat.new_chat)
        explain = QAction("Explain terminal output", self)
        explain.setShortcut(QKeySequence("Ctrl+E"))
        explain.triggered.connect(self.explain_output)
        focus_chat = QAction("Focus chat", self)
        focus_chat.setShortcut(QKeySequence("Ctrl+2"))
        focus_chat.triggered.connect(self.chat.input.setFocus)
        chat_menu.addAction(new_chat)
        chat_menu.addAction(explain)
        chat_menu.addAction(focus_chat)

        help_menu = bar.addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    # ---------------------------------------------------------- actions

    def edit_settings(self):
        new = ask_settings(self.cfg, self)
        if new is None:
            return
        shell_changed = new.get("shell") != self.cfg.get("shell")
        font_changed = (new.get("font_family") != self.cfg.get("font_family")
                        or new.get("font_size") != self.cfg.get("font_size"))
        size_changed = new.get("window_size") != self.cfg.get("window_size")
        self.cfg = new
        self.chat.refresh_config(new)
        self.terminal.cfg = new
        if shell_changed:
            self.terminal.restart(new)
            self.shell_label.setText(new.get("shell", "cmd.exe"))
        elif font_changed:
            self.terminal.apply_font(new.get("font_family", "Consolas"),
                                     int(new.get("font_size", 11)))
        if size_changed:
            size = window_size(new.get("window_size", "standard"))
            if size["width"] is None:
                self.showMaximized()
            else:
                if self.isMaximized():
                    self.showNormal()
                self.resize(size["width"], size["height"])

    def restart_shell(self):
        self.terminal.restart(self.cfg)
        self.terminal.setFocus()

    def explain_output(self):
        self.chat.include_output.setChecked(True)
        self.chat.input.setPlainText(
            "Explain what happened in the terminal above, and tell me what to do next.")
        self.chat.input.setFocus()

    def show_about(self):
        QMessageBox.about(
            self, "About AI Terminal",
            "<b>AI Terminal</b><br><br>"
            "A real cmd.exe session (ConPTY) beside a configurable AI assistant."
            "<br><br>Right-click copies the selection, or pastes when nothing is "
            "selected. Shift+right-click opens the menu."
            "<br><br>Ctrl+1 terminal · Ctrl+2 chat · Ctrl+E explain output · "
            "Ctrl+, settings")

    def closeEvent(self, event):
        self.chat.stop()
        self.terminal.close_session()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI Terminal")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    window = MainWindow()
    if window.cfg.get("window_size", "standard") == "maximized":
        window.showMaximized()
    else:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
