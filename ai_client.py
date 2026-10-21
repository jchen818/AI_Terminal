"""Talks to the model. Three wire formats: OpenAI-compatible, Anthropic, Ollama.

Tool calls (MCP) go one of two ways:

- native: the tool list is sent in the request's `tools` field and the model
  answers with structured tool calls. Needs a model trained for it and, on
  vLLM, a server started with --enable-auto-tool-choice --tool-call-parser.
- text: the tool list and a calling convention go into the system prompt and
  the model writes a ```tool block in its reply, which the app parses. Works
  with any model that follows instructions.

"auto" tries native and drops to text for the rest of the session when the
server refuses tools.

The chat keeps history in one neutral shape and this module converts it to
whichever wire format and tool mode a request uses:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str,
     "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str,
     "is_error": bool}
"""

import json
import re
import uuid

import requests
from PySide6.QtCore import QThread, Signal

TIMEOUT = (10, 300)  # connect, read

TOOL_FENCE = "tool"
# A finished ```tool block. Also accepts <tool_call>…</tool_call>, which
# Hermes-style models (Qwen and others) write when the server is not parsing
# tool calls for them.
TOOL_BLOCK = re.compile(
    r"```[ \t]*" + TOOL_FENCE + r"[ \t]*\n(?P<a>.*?)\n?[ \t]*```"
    r"|<tool_call>\s*(?P<b>.*?)\s*</tool_call>", re.DOTALL)
TOOL_OPEN = re.compile(r"```[ \t]*" + TOOL_FENCE + r"[ \t]*\n|<tool_call>")


# ---------------------------------------------------------------- helpers

def _headers(cfg: dict) -> dict:
    style = cfg.get("api_style", "openai")
    key = cfg.get("api_key", "").strip()
    h = {"Content-Type": "application/json"}
    if style == "anthropic":
        h["x-api-key"] = key
        h["anthropic-version"] = "2023-06-01"
    elif style == "openai" and key:
        h["Authorization"] = f"Bearer {key}"
        h["api-key"] = key  # Azure OpenAI uses this header name
    return h


def _endpoint(cfg: dict) -> str:
    base = cfg.get("base_url", "").rstrip("/")
    style = cfg.get("api_style", "openai")
    if style == "anthropic":
        return f"{base}/v1/messages"
    if style == "ollama":
        return f"{base}/api/chat"
    return f"{base}/chat/completions"


def ollama_num_ctx(cfg: dict) -> int:
    """Context window to ask Ollama for.

    Ollama does not use the model's full window: it defaults to 2K-4K tokens
    and silently cuts anything longer from the front of the prompt, so the
    task, the rules and the earlier steps vanish while the app thinks it sent
    them. Size it to the app's own context setting instead (about 3
    characters per token for terminal output) plus room for the reply.
    "ollama_num_ctx" in the config overrides it.
    """
    fixed = int(cfg.get("ollama_num_ctx", 0) or 0)
    if fixed > 0:
        return fixed
    chars = int(cfg.get("context_chars", 96000)) + len(cfg.get("system_prompt", ""))
    tokens = chars // 3 + max(4096, int(cfg.get("max_tokens", 2048)))
    return max(8192, -(-tokens // 2048) * 2048)


def _payload(cfg: dict, messages: list, stream: bool, tools: list = None) -> dict:
    """Request body. messages are already in this provider's wire format;
    tools, when given, are sent natively."""
    style = cfg.get("api_style", "openai")
    system = cfg.get("system_prompt", "").strip()

    if style == "anthropic":
        body = {
            "model": cfg.get("model", ""),
            "max_tokens": int(cfg.get("max_tokens", 2048)),
            "temperature": float(cfg.get("temperature", 0.7)),
            "messages": messages,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": t.key, "description": _desc(t),
                              "input_schema": _schema(t)} for t in tools]
        return body

    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    fn_tools = [{"type": "function",
                 "function": {"name": t.key, "description": _desc(t),
                              "parameters": _schema(t)}}
                for t in tools or []]
    if style == "ollama":
        body = {
            "model": cfg.get("model", ""),
            "messages": msgs,
            "stream": stream,
            "options": {
                "temperature": float(cfg.get("temperature", 0.7)),
                # Ollama otherwise generates without limit, so a model stuck
                # in a loop never ends. Generous, since thinking counts too.
                "num_predict": max(8192, 4 * int(cfg.get("max_tokens", 2048))),
                "num_ctx": ollama_num_ctx(cfg),
            },
        }
        if fn_tools:
            body["tools"] = fn_tools
        return body
    body = {
        "model": cfg.get("model", ""),
        "messages": msgs,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 2048)),
        "stream": stream,
    }
    if fn_tools:
        body["tools"] = fn_tools
    return body


def _error_text(resp) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err)[:400]
        return str(err)[:400]
    except ValueError:
        return (resp.text or "")[:400]


# ------------------------------------------------------------ tool schemas

DESC_CHARS = 1000


def _desc(tool) -> str:
    d = tool.description or f"{tool.name} (from MCP server {tool.server})"
    return d if len(d) <= DESC_CHARS else d[:DESC_CHARS - 1] + "…"


def _schema(tool) -> dict:
    schema = dict(tool.schema or {})
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _type_of(spec: dict) -> str:
    if not isinstance(spec, dict):
        return "any"
    if "enum" in spec:
        return " | ".join(json.dumps(v) for v in spec["enum"][:12])
    t = spec.get("type")
    if isinstance(t, list):
        t = " | ".join(str(x) for x in t)
    if t == "array":
        return f"array of {_type_of(spec.get('items', {}))}"
    if t == "object" and spec.get("properties"):
        inner = ", ".join(f"{k}: {_type_of(v)}" for k, v in
                          list(spec["properties"].items())[:8])
        return "{" + inner + "}"
    if not t:
        for key in ("anyOf", "oneOf"):
            if key in spec:
                return " | ".join(_type_of(s) for s in spec[key][:4])
    return t or "any"


def text_tool_prompt(tools: list) -> str:
    """The calling convention plus the tool list, for the system prompt."""
    lines = [
        "## Tools",
        "You can call the tools below. They come from MCP servers connected to "
        "this app and run on my computer, separately from the terminal. Use a "
        "tool when it is the most direct way to get what you need; otherwise "
        "answer, or give a terminal command, as usual.",
        "",
        "To call a tool, write at most a short sentence, then exactly one "
        "block like this and nothing after it:",
        "```" + TOOL_FENCE,
        '{"name": "<tool name>", "arguments": {"<parameter>": <value>}}',
        "```",
        "The block must hold valid JSON. Then stop: the app runs the tool and "
        "sends you the result in the next message. One tool call per reply. "
        "Never write a ```" + TOOL_FENCE + " block as an example or for any "
        "other purpose, and never invent a tool's result.",
        "",
        "Available tools:",
    ]
    for t in tools:
        lines.append(f"\n### {t.key}")
        desc = " ".join(_desc(t).split())
        if desc:
            lines.append(desc)
        props = (t.schema or {}).get("properties", {}) or {}
        required = set((t.schema or {}).get("required", []) or [])
        if not props:
            lines.append("Parameters: none")
            continue
        lines.append("Parameters:")
        for name, spec in props.items():
            mark = " (required)" if name in required else ""
            pdesc = ""
            if isinstance(spec, dict) and spec.get("description"):
                pdesc = " — " + " ".join(str(spec["description"]).split())[:200]
            lines.append(f"- {name}: {_type_of(spec)}{mark}{pdesc}")
    return "\n".join(lines)


def parse_text_tool_call(answer: str):
    """Find the first tool call a model wrote as text.

    Returns (before, call) where before is the reply up to the block and call
    is {"id", "name", "arguments", "error"}, or (answer, None) when there is
    no finished block. error is set when the block is there but unusable,
    so the model can be told what was wrong.
    """
    m = TOOL_BLOCK.search(answer or "")
    if not m:
        return answer, None
    raw = (m.group("a") if m.group("a") is not None else m.group("b") or "").strip()
    # A broken call keeps a placeholder name: providers reject empty ones
    # in the history that is sent back.
    call = {"id": new_call_id(), "name": "invalid_tool_call", "arguments": {},
            "error": ""}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        call["error"] = f"The tool block was not valid JSON ({exc})."
        call["raw"] = raw
        return answer[:m.start()].rstrip(), call
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        call["error"] = ('The tool block must be an object like {"name": '
                         '"…", "arguments": {…}}.')
        call["raw"] = raw
        return answer[:m.start()].rstrip(), call
    args = data.get("arguments", data.get("parameters", {}))
    if isinstance(args, str):           # some models double-encode
        try:
            args = json.loads(args)
        except ValueError:
            pass
    call["name"] = data["name"]
    if isinstance(args, dict):
        call["arguments"] = args
    else:
        call["error"] = '"arguments" must be a JSON object.'
    return answer[:m.start()].rstrip(), call


def strip_tool_blocks(text: str) -> str:
    return TOOL_BLOCK.sub("", text or "").strip()


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:20]


def _clean_id(cid: str) -> str:
    # Anthropic requires ^[a-zA-Z0-9_-]+$; ids may come from another provider.
    return re.sub(r"[^A-Za-z0-9_-]", "_", cid or "") or new_call_id()


# ---------------------------------------------------- history → wire format

def _call_block(call: dict) -> str:
    body = json.dumps({"name": call.get("name", ""),
                       "arguments": call.get("arguments") or {}},
                      ensure_ascii=False)
    return f"```{TOOL_FENCE}\n{body}\n```"


def _result_text(msg: dict) -> str:
    head = f"Result of tool `{msg.get('name', '?')}`"
    if msg.get("is_error"):
        head += " (error)"
    return f"{head}:\n```\n{msg.get('content', '') or '(empty)'}\n```"


def wire_messages(style: str, messages: list, native: bool) -> list:
    """Neutral history → the messages list a provider expects.

    native=False writes tool calls and results as plain text, the same way
    text mode asks the model to write them, so history made in either mode
    can be sent in either mode (and to a model that has no tools at all).
    """
    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        calls = m.get("tool_calls") or []

        if role == "assistant" and calls:
            if not native:
                blocks = "\n\n".join(_call_block(c) for c in calls)
                out.append({"role": "assistant",
                            "content": f"{content}\n\n{blocks}".strip()})
            elif style == "anthropic":
                blocks = [{"type": "text", "text": content}] if content.strip() else []
                blocks += [{"type": "tool_use", "id": _clean_id(c.get("id")),
                            "name": c.get("name", ""),
                            "input": c.get("arguments") or {}} for c in calls]
                out.append({"role": "assistant", "content": blocks})
            elif style == "ollama":
                out.append({"role": "assistant", "content": content,
                            "tool_calls": [{"function": {
                                "name": c.get("name", ""),
                                "arguments": c.get("arguments") or {}}}
                                for c in calls]})
            else:
                out.append({"role": "assistant", "content": content or None,
                            "tool_calls": [{
                                "id": _clean_id(c.get("id")), "type": "function",
                                "function": {"name": c.get("name", ""),
                                             "arguments": json.dumps(
                                                 c.get("arguments") or {},
                                                 ensure_ascii=False)}}
                                for c in calls]})
            continue

        if role == "tool":
            if not native:
                text = _result_text(m)
                # All results of one round go back as one user message.
                if out and out[-1].get("_results"):
                    out[-1]["content"] += "\n\n" + text
                else:
                    out.append({"role": "user", "content": text, "_results": True})
            elif style == "anthropic":
                block = {"type": "tool_result",
                         "tool_use_id": _clean_id(m.get("tool_call_id")),
                         "content": content or "(empty)"}
                if m.get("is_error"):
                    block["is_error"] = True
                out.append({"role": "user", "content": [block]})
            elif style == "ollama":
                out.append({"role": "tool", "content": content,
                            "tool_name": m.get("name", "")})
            else:
                out.append({"role": "tool",
                            "tool_call_id": _clean_id(m.get("tool_call_id")),
                            "content": content or "(empty)"})
            continue

        out.append({"role": role, "content": content})

    for m in out:
        m.pop("_results", None)
    if style == "anthropic":
        out = _merge_same_role(out)
    return out


def _merge_same_role(msgs: list) -> list:
    """Anthropic wants turns to alternate; join neighbours of one role."""
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            prev = merged[-1]
            a, b = prev["content"], m["content"]
            if isinstance(a, str) and isinstance(b, str):
                prev["content"] = f"{a}\n\n{b}"
            else:
                prev["content"] = _blocks(a) + _blocks(b)
        else:
            merged.append(dict(m))
    return merged


def _blocks(content):
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": content}] if content else []


def is_tools_refusal(status: int, text: str) -> bool:
    """True when an error means "this server/model does not do native tools".

    vLLM: '"auto" tool choice requires --enable-auto-tool-choice …'
    Ollama: '… does not support tools'. DeepSeek reasoner: '… does not
    support Function Calling'. Others reject the tools field outright.
    """
    if status not in (400, 404, 422, 500, 501):
        return False
    t = (text or "").lower()
    return any(k in t for k in ("tool", "function call", "function_call",
                                "functions"))


# ---------------------------------------------------------------- worker

class ChatWorker(QThread):
    """Runs one request. Emits text as it arrives.

    After finished_ok, tool_calls holds any native tool calls the model made
    and mode_used says how tools were offered ("native", "text" or "off").
    """

    chunk = Signal(str)
    thinking = Signal(str)   # reasoning text, from models that stream it
    finished_ok = Signal()
    failed = Signal(str)
    fell_back = Signal(str)  # auto mode switched to text; carries the reason

    def __init__(self, cfg: dict, messages: list, parent=None,
                 tools: list = None, tool_mode: str = "off"):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.messages = messages
        self.tools = list(tools or [])
        self.tool_mode = tool_mode if self.tools else "off"
        self.mode_used = "off"
        self.tool_calls = []
        self._acc = {}
        self._stop = False
        self._resp = None

    def cancel(self):
        """Stop reading. Closing the response wakes a read that is waiting
        for the next line, so the thread ends promptly."""
        self._stop = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self):
        if not self.cfg.get("model"):
            self.failed.emit("No model set. Open Settings and choose a provider and model.")
            return
        if not self.cfg.get("base_url"):
            self.failed.emit("No base URL set. Open Settings to configure the provider.")
            return
        try:
            self._run_modes()
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            if self._stop:          # cancelled: the closed stream raised
                return
            if isinstance(exc, requests.exceptions.ConnectionError):
                self.failed.emit(
                    f"Could not reach {self.cfg.get('base_url')}. Check the base "
                    "URL and your network, or start the local server if you are "
                    "using one.")
            elif isinstance(exc, requests.exceptions.Timeout):
                self.failed.emit("The request timed out.")
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _run_modes(self):
        if self.tool_mode in ("native", "auto"):
            err = self._request("native")
            if err is None:
                return
            status, text = err
            if self.tool_mode == "auto" and is_tools_refusal(status, text):
                self.fell_back.emit(text)
                self._acc, self.tool_calls = {}, []
            else:
                hint = ""
                if is_tools_refusal(status, text):
                    hint = ("\n\nThe server did not accept tool calls. On vLLM, "
                            "start it with --enable-auto-tool-choice "
                            "--tool-call-parser <parser>, or set Tool calls to "
                            "Auto or Text in Settings → MCP.")
                self.failed.emit(f"HTTP {status}: {text}{hint}")
                return
        err = self._request("text" if self.tool_mode != "off" else "off")
        if err is not None:
            self.failed.emit(f"HTTP {err[0]}: {err[1]}")

    def _request(self, mode: str):
        """One request. Returns None when done, or (status, text) on an HTTP
        error so the caller can decide whether to retry another way."""
        self.mode_used = mode
        style = self.cfg.get("api_style", "openai")
        cfg = dict(self.cfg)
        if mode == "text":
            base = cfg.get("system_prompt", "").strip()
            extra = text_tool_prompt(self.tools)
            cfg["system_prompt"] = f"{base}\n\n{extra}" if base else extra
        msgs = wire_messages(style, self.messages, native=(mode == "native"))
        tools = self.tools if mode == "native" else None

        stream = bool(cfg.get("stream", True))
        resp = requests.post(
            _endpoint(cfg),
            headers=_headers(cfg),
            json=_payload(cfg, msgs, stream, tools),
            stream=stream,
            timeout=TIMEOUT,
        )
        self._resp = resp
        if resp.status_code >= 400:
            return resp.status_code, _error_text(resp)

        if not stream:
            obj = resp.json()
            thought = _extract_thinking(cfg, obj)
            if thought:
                self.thinking.emit(thought)
            self.chunk.emit(_extract_full(cfg, obj))
            self.tool_calls = _extract_tool_calls(cfg, obj)
            self.finished_ok.emit()
            return None

        # Streams are UTF-8. Without a charset in the header requests falls
        # back to ISO-8859-1 for text/* (garbling non-ASCII) or to bytes.
        if (resp.encoding or "").lower() in ("", "iso-8859-1"):
            resp.encoding = "utf-8"
        for raw in resp.iter_lines(decode_unicode=True):
            if self._stop:
                resp.close()
                return None
            if not raw:
                continue
            line = raw.strip()

            if style == "ollama":
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                text, thought = _ollama_delta(obj)
                self._take_ollama_tools(obj)
                if thought:
                    self.thinking.emit(thought)
                if text:
                    self.chunk.emit(text)
                continue

            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if style == "anthropic":
                text, thought = _anthropic_delta(obj)
                self._take_anthropic_tools(obj)
            else:
                text, thought = _openai_delta(obj)
                self._take_openai_tools(obj)
            if thought:
                self.thinking.emit(thought)
            if text:
                self.chunk.emit(text)

        self.tool_calls = self._finish_calls()
        self.finished_ok.emit()
        return None

    # -- streamed tool calls ------------------------------------------------

    def _take_openai_tools(self, obj):
        try:
            parts = obj["choices"][0]["delta"].get("tool_calls") or []
        except (KeyError, IndexError, TypeError, AttributeError):
            return
        for p in parts:
            i = p.get("index", len(self._acc))
            slot = self._acc.setdefault(i, {"id": "", "name": "", "args": ""})
            if p.get("id"):
                slot["id"] = p["id"]
            fn = p.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            args = fn.get("arguments")
            if isinstance(args, dict):       # a few servers send an object
                slot["args"] = json.dumps(args)
            elif args:
                slot["args"] += args

    def _take_anthropic_tools(self, obj):
        kind = obj.get("type")
        if kind == "content_block_start":
            block = obj.get("content_block", {})
            if block.get("type") == "tool_use":
                self._acc[obj.get("index", len(self._acc))] = {
                    "id": block.get("id", ""), "name": block.get("name", ""),
                    "args": ""}
        elif kind == "content_block_delta":
            delta = obj.get("delta", {})
            slot = self._acc.get(obj.get("index"))
            if slot is not None and delta.get("type") == "input_json_delta":
                slot["args"] += delta.get("partial_json", "")

    def _take_ollama_tools(self, obj):
        for c in (obj.get("message") or {}).get("tool_calls") or []:
            fn = c.get("function") or {}
            args = fn.get("arguments", {})
            self._acc[len(self._acc)] = {
                "id": c.get("id", ""), "name": fn.get("name", ""),
                "args": args if isinstance(args, str) else json.dumps(args)}

    def _finish_calls(self) -> list:
        calls = []
        for _, slot in sorted(self._acc.items()):
            if not slot["name"]:
                continue
            calls.append(_make_call(slot["id"], slot["name"], slot["args"]))
        return calls


def _make_call(cid, name, args) -> dict:
    call = {"id": cid or new_call_id(), "name": name, "arguments": {}, "error": ""}
    if isinstance(args, dict):
        call["arguments"] = args
        return call
    raw = (args or "").strip()
    if not raw:
        return call
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        call["error"] = f"The tool arguments were not valid JSON ({exc})."
        call["raw"] = raw
        return call
    if isinstance(parsed, dict):
        call["arguments"] = parsed
    else:
        call["error"] = "The tool arguments must be a JSON object."
    return call


# ---------------------------------------------------------------- parsers

# Each parser returns (answer_text, thinking_text). Reasoning models stream
# their thinking on a separate field; ignoring it left the reply blank for
# minutes while the model was busy thinking.

def _openai_delta(obj):
    try:
        delta = obj["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return "", ""
    # reasoning_content: DeepSeek, vLLM, LM Studio. reasoning: OpenRouter,
    # Groq, Ollama's OpenAI endpoint.
    thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if not isinstance(thought, str):
        thought = ""
    return delta.get("content") or "", thought


def _anthropic_delta(obj):
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta", {})
        if delta.get("type") == "thinking_delta":
            return "", delta.get("thinking", "") or ""
        return delta.get("text", "") or "", ""
    return "", ""


def _ollama_delta(obj):
    msg = obj.get("message", {}) or {}
    return msg.get("content", "") or "", msg.get("thinking", "") or ""


def _extract_thinking(cfg: dict, obj) -> str:
    style = cfg.get("api_style", "openai")
    try:
        if style == "anthropic":
            return "".join(b.get("thinking", "") for b in obj.get("content", [])
                           if b.get("type") == "thinking")
        if style == "ollama":
            return obj.get("message", {}).get("thinking", "") or ""
        msg = obj["choices"][0]["message"]
        return msg.get("reasoning_content") or msg.get("reasoning") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def _extract_full(cfg: dict, obj) -> str:
    style = cfg.get("api_style", "openai")
    try:
        if style == "anthropic":
            return "".join(b.get("text", "") for b in obj.get("content", [])
                           if b.get("type", "text") == "text")
        if style == "ollama":
            return obj.get("message", {}).get("content", "")
        return obj["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _extract_tool_calls(cfg: dict, obj) -> list:
    style = cfg.get("api_style", "openai")
    try:
        if style == "anthropic":
            return [_make_call(b.get("id"), b.get("name", ""), b.get("input") or {})
                    for b in obj.get("content", []) if b.get("type") == "tool_use"]
        if style == "ollama":
            calls = obj.get("message", {}).get("tool_calls") or []
        else:
            calls = obj["choices"][0]["message"].get("tool_calls") or []
        return [_make_call(c.get("id"), (c.get("function") or {}).get("name", ""),
                           (c.get("function") or {}).get("arguments", {}))
                for c in calls]
    except (KeyError, IndexError, TypeError, AttributeError):
        return []


# ---------------------------------------------------------------- test

def test_connection(cfg: dict):
    """One tiny non-streaming call. Returns (ok, message)."""
    if not cfg.get("base_url"):
        return False, "Set a base URL first."
    if not cfg.get("model"):
        return False, "Set a model name first."
    probe = dict(cfg)
    probe["max_tokens"] = 16
    try:
        resp = requests.post(
            _endpoint(probe),
            headers=_headers(probe),
            json=_payload(probe, [{"role": "user", "content": "Reply with: ok"}], False),
            timeout=(8, 30),
        )
    except requests.exceptions.ConnectionError:
        return False, "Could not connect. Check the base URL."
    except requests.exceptions.Timeout:
        return False, "Timed out."
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {_error_text(resp)}"
    reply = _extract_full(probe, resp.json()).strip()
    return True, f"Connected. Model replied: {reply[:60] or '(empty)'}"
