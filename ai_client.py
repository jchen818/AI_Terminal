"""Talks to the model. Three wire formats: OpenAI-compatible, Anthropic, Ollama."""

import json

import requests
from PySide6.QtCore import QThread, Signal

TIMEOUT = (10, 300)  # connect, read


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


def _payload(cfg: dict, messages: list, stream: bool) -> dict:
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
        return body

    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    if style == "ollama":
        return {
            "model": cfg.get("model", ""),
            "messages": msgs,
            "stream": stream,
            "options": {
                "temperature": float(cfg.get("temperature", 0.7)),
                # Ollama otherwise generates without limit, so a model stuck
                # in a loop never ends. Generous, since thinking counts too.
                "num_predict": max(8192, 4 * int(cfg.get("max_tokens", 2048))),
            },
        }
    return {
        "model": cfg.get("model", ""),
        "messages": msgs,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 2048)),
        "stream": stream,
    }


def _error_text(resp) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err)[:400]
        return str(err)[:400]
    except ValueError:
        return (resp.text or "")[:400]


# ---------------------------------------------------------------- worker

class ChatWorker(QThread):
    """Runs one request. Emits text as it arrives."""

    chunk = Signal(str)
    thinking = Signal(str)   # reasoning text, from models that stream it
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, cfg: dict, messages: list, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.messages = messages
        self._stop = False

    def cancel(self):
        self._stop = True

    def run(self):
        if not self.cfg.get("model"):
            self.failed.emit("No model set. Open Settings and choose a provider and model.")
            return
        if not self.cfg.get("base_url"):
            self.failed.emit("No base URL set. Open Settings to configure the provider.")
            return
        try:
            self._request()
        except requests.exceptions.ConnectionError:
            self.failed.emit(
                f"Could not reach {self.cfg.get('base_url')}. Check the base URL "
                "and your network, or start the local server if you are using one.")
        except requests.exceptions.Timeout:
            self.failed.emit("The request timed out.")
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _request(self):
        stream = bool(self.cfg.get("stream", True))
        resp = requests.post(
            _endpoint(self.cfg),
            headers=_headers(self.cfg),
            json=_payload(self.cfg, self.messages, stream),
            stream=stream,
            timeout=TIMEOUT,
        )
        if resp.status_code >= 400:
            self.failed.emit(f"HTTP {resp.status_code}: {_error_text(resp)}")
            return

        if not stream:
            obj = resp.json()
            thought = _extract_thinking(self.cfg, obj)
            if thought:
                self.thinking.emit(thought)
            self.chunk.emit(_extract_full(self.cfg, obj))
            self.finished_ok.emit()
            return

        style = self.cfg.get("api_style", "openai")
        for raw in resp.iter_lines(decode_unicode=True):
            if self._stop:
                resp.close()
                return
            if not raw:
                continue
            line = raw.strip()

            if style == "ollama":
                text, thought = _ollama_delta(line)
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
            text, thought = (_anthropic_delta(obj) if style == "anthropic"
                             else _openai_delta(obj))
            if thought:
                self.thinking.emit(thought)
            if text:
                self.chunk.emit(text)

        self.finished_ok.emit()


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


def _ollama_delta(line: str):
    try:
        obj = json.loads(line)
    except ValueError:
        return "", ""
    msg = obj.get("message", {})
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
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


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
