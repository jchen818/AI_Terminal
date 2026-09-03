# AI Terminal

A single-window Windows app: a real command prompt on the left, a configurable AI
assistant on the right.

The left pane is not a fake console. It spawns `cmd.exe` inside a ConPTY
pseudo-console (the same mechanism Windows Terminal uses) and renders the output
through a VT emulator, so tab completion, `doskey` history, arrow keys, colours,
`Ctrl+C`, and interactive programs all behave the way they do in the real command
prompt.

## Install

Requires Windows 10 1809 or later (ConPTY) and Python 3.9+.

```
pip install -r requirements.txt
python main.py
```

Or just double-click `run.bat`, which builds a virtual environment the first time
and launches the app afterwards.

## Set up a provider

Open **Settings** (button in the chat header, or `Ctrl+,`). Pick a provider — the
base URL and a starting model name are filled in for you — then paste your API key
and press **Test connection**.

| Provider | What it needs |
| --- | --- |
| OpenAI, DeepSeek, Groq, OpenRouter | API key |
| Anthropic | API key |
| Azure OpenAI | Replace `YOUR-RESOURCE` in the base URL, use your deployment name as the model |
| Ollama, LM Studio | Nothing — just have the local server running |
| Custom | Any OpenAI-compatible endpoint |

Settings live in `%APPDATA%\AITerminal\config.json`. The API key is stored there in
plain text, so treat that file the way you would treat any other credential file.

## Using the two panes together

- **Include terminal screen** — tick this before sending, and terminal output is
  attached to your question: everything since the last time you attached it (or
  the full scrollback, the first time), not just whatever is currently visible.
  Consecutive captures pick up where the last one left off, so they stitch
  together into one continuous record instead of each one only grabbing what's
  on screen at that moment. Good for "why did this fail?".
- **Explain terminal output** (`Ctrl+E`) — fills in that question for you.
- **Run in terminal** — commands from a reply appear in the dropdown below the
  transcript. A block of independent commands is split into one entry per line
  (`1/3`, `2/3`, …) so a single click never runs more than one command. A script —
  anything with a loop, conditional, function, heredoc or line continuation — stays
  whole, because its lines mean nothing on their own; those entries are labelled
  `script:` with a line count. After a manual run the dropdown steps to the next
  command, so you can click through a list one at a time.
- **Check results** — on by default. After a command run from the chat finishes,
  its output is captured, cleaned of escape codes and the trailing prompt, and sent
  back so the assistant can confirm it worked before anything else happens. The
  result appears in the transcript, so you see exactly what the model saw.
- **Auto-run** — tick this and the first code block of each reply runs on its own.
  The button turns into a `Cancel (4)` countdown first, so there is always a moment
  to stop it. Blocks tagged as something other than shell (`python`, `yaml`, output
  samples) are skipped, and anything matching the destructive-command list still
  stops and asks. It is off at every start and is not saved to the config file.

With both ticked you get a run→check→fix loop: a command runs, its output goes back,
the assistant either corrects it or moves to the next step. The loop stops on any of
four conditions — the assistant replies without a code block (it is told to answer
`DONE` when finished), a command asks for input such as a sudo password, six rounds
elapse, or you type in the terminal yourself. Typing by hand always cancels an
in-flight capture, on the assumption that you have taken over.

There is no stored plan of commands to replay. The dropdown is rebuilt from each
reply, so a correction simply becomes the next thing to run, and the block list is
emptied while a request is in flight — a command the assistant has already moved
past can never be fired by a stray click on Run.

Auto-run takes the **first** entry only — one command, or one script. If a reply
carries more, a note says so and the rest stay selectable in the dropdown.

A command counts as finished once the shell has been quiet for 900 ms; adjust
`capture_idle_ms` in `config.json` if commands on a slow link get cut short.

## Scrolling

Both panes have a scrollbar and a mouse wheel. The terminal keeps the number of
lines set under **Settings → Terminal → Scrollback lines** (5000 by default).

| Key | Action |
| --- | --- |
| `Ctrl+Shift+PageUp` / `PageDown` | Scroll the terminal a screen at a time |
| `Ctrl+Shift+Home` / `End` | Jump to the oldest line / back to the prompt |

Plain `PageUp` and `PageDown` go to the shell, not the scrollback, so programs like
`more` and `less` keep working.

## Keys the shell gets first

The shell has priority over the app for key combinations, so `Tab` completion,
`Ctrl+E`, `Ctrl+A`, `Ctrl+R`, `Alt+F` and the rest of readline work normally over
SSH. The app reserves only `Ctrl+Shift+...`, `Ctrl+1`, `Ctrl+2` and `Ctrl+,` — none
of which readline binds. `Alt`+key is sent as an ESC prefix, which is how Meta is
transmitted, and `Shift+Tab` is sent as `ESC [ Z`.

`TERM` is set to `xterm-256color` for the shell so remote programs know what they
are drawing on. If a full-screen program over SSH garbles the display, change
`"term"` in `config.json` to `"vt100"` or `""`.

Two behaviours worth knowing. In the terminal, scrolling back **holds its place**
while new output streams past, instead of yanking you to the bottom — but typing
anything snaps straight back to the live prompt. In the chat, a reply that is still
streaming only auto-follows when you are already at the bottom, so you can read
back through an earlier answer without the view jumping out from under you.
Sending a new message always returns you to the newest content.

## Shortcuts

| Key | Action |
| --- | --- |
| `Ctrl+1` / `Ctrl+2` | Focus the terminal / the chat box |
| `Enter` / `Shift+Enter` | Send message / new line in the chat box |
| `Ctrl+E` | Ask about the terminal screen |
| `Ctrl+N` | New chat |
| `Ctrl+,` | Settings |
| `Ctrl+Shift+R` | Restart the shell |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste in the terminal |

In the terminal, `Ctrl+C` copies when text is selected and sends a break otherwise —
the same rule Windows Terminal uses.

**Mouse (both panes):** drag to select, then **right-click to copy** it. In the
terminal, dragging past the top or bottom edge scrolls the view to keep selecting
into content that was off-screen, the way most editors handle it. With
nothing selected, **right-click pastes**. That is the QuickEdit behaviour from the
classic console, and the chat side follows the same rule so one button does the
same thing everywhere. Middle-click also pastes, and Shift+right-click opens the
usual menu.

In the transcript, which you cannot type into, right-clicking with nothing selected
drops the clipboard into the message box below and focuses it — handy for copying
an error out of the terminal and asking about it. Double-click in the terminal
selects a word, and the wheel scrolls back through history.

Pasting more than one line asks for confirmation first, because every line break in
pasted text runs a command the moment it arrives. Turn that off under
**Settings → Terminal → Paste safety** if it gets in your way.

## Files

```
main.py        window, splitter, menus
terminal.py    ConPTY session + pyte emulator + custom painting
chat.py        transcript, composer, code-block runner
ai_client.py   streaming clients for OpenAI / Anthropic / Ollama wire formats
config.py      config file and settings dialog
```

## Packaging to a single .exe

```
pip install pyinstaller
pyinstaller --noconsole --onefile --name AITerminal main.py
```

The result lands in `dist\AITerminal.exe`. Keep `--noconsole`: the app opens its own
console through ConPTY and does not need one of its own.

## Notes and limits

- Windows only for the terminal pane. On other platforms the app still starts and
  the chat works, but the left pane shows a message instead of a shell.
- The shell is configurable — `powershell.exe`, `pwsh.exe`, and `wsl.exe` are in the
  dropdown, and you can type any other executable. Changing it restarts the session.
- Conversation history is kept in memory only and is cleared by **New chat**.
