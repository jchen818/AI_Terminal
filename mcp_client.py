"""MCP servers: connect to them, list their tools, and call them.

The official `mcp` SDK is asyncio-based and the app is Qt, so every server
lives on one background thread with its own event loop. Each server is a
long-running task that opens the transport (a local process over stdio, or a
URL over Streamable HTTP / SSE), keeps the session open, and closes it when
told to. The chat calls tools from worker threads with call(), which hands
the request to that loop and waits for the answer.

Servers are configured in the same shape Claude Desktop uses, so existing
entries can be pasted in:

    {"mcpServers": {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/work"]},
        "remote":     {"url": "http://10.0.0.5:8000/mcp", "headers": {"Authorization": "Bearer …"}},
        "old":        {"url": "http://host:9000/sse", "type": "sse", "disabled": true},
        "mcpo":       {"url": "http://10.0.0.5:8000", "type": "openapi",
                       "headers": {"Authorization": "Bearer ${MCPO_API_KEY}"}}
    }}

"type": "openapi" reads a REST API's openapi.json instead of speaking MCP —
see openapi_tools.py; that is how servers behind mcpo are reached.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

import openapi_tools
from config import config_dir
from openapi_tools import is_openapi

try:  # The SDK needs Python 3.10+; without it the rest of the app still works.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    ClientSession = StdioServerParameters = stdio_client = None
    MCP_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

CONNECT_TIMEOUT = 60      # seconds to start a server and list its tools
TOOL_NAME_MAX = 64        # OpenAI's limit on function names


# ------------------------------------------------------------------ config

def parse_servers(text: str) -> dict:
    """Server specs from the settings editor. Raises ValueError with a reason.

    Accepts {"mcpServers": {...}} (Claude Desktop / Cursor) or the inner
    mapping on its own. An empty box means no servers.
    """
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"Not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object of servers.")
    if isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Server “{name}” must be an object.")
        if not spec.get("command") and not spec.get("url"):
            raise ValueError(f"Server “{name}” needs a \"command\" (local) "
                             "or a \"url\" (remote).")
        if is_openapi(spec) and not spec.get("url"):
            raise ValueError(f"Server “{name}”: an OpenAPI server needs a \"url\".")
        if "args" in spec and not isinstance(spec["args"], list):
            raise ValueError(f"Server “{name}”: \"args\" must be a list.")
        for key in ("env", "headers"):
            if key in spec and not isinstance(spec[key], dict):
                raise ValueError(f"Server “{name}”: \"{key}\" must be an object.")
    return data


def servers_text(servers: dict) -> str:
    return json.dumps({"mcpServers": servers or {}}, indent=2)


def tool_key(server: str, tool: str) -> str:
    """The name the model sees: server__tool, safe for every provider."""
    raw = f"{server}__{tool}"
    key = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return key[:TOOL_NAME_MAX]


# ------------------------------------------------------------------- state

@dataclass
class ToolInfo:
    key: str                  # name given to the model
    server: str
    name: str                 # name on the server
    description: str
    schema: dict
    read_only: bool = False   # server says it changes nothing
    destructive: bool = False # server says it may destroy data
    endpoint: dict = None     # set for OpenAPI tools: how to call over HTTP


@dataclass
class ServerState:
    name: str
    status: str = "off"       # off | connecting | ready | error | disabled
    error: str = ""
    tools: list = field(default_factory=list)
    session: object = None


def _attr(obj, *names, default=None):
    """Read a field under its v2 (snake_case) or v1 (camelCase) SDK name."""
    for n in names:
        if obj is not None and hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def result_text(result) -> tuple:
    """(text, is_error) from a CallToolResult, whatever its content types."""
    parts = []
    for block in _attr(result, "content", default=[]) or []:
        kind = _attr(block, "type", default="")
        if kind == "text":
            parts.append(_attr(block, "text", default=""))
        elif kind in ("image", "audio"):
            data = _attr(block, "data", default="") or ""
            mime = _attr(block, "mime_type", "mimeType", default=kind)
            parts.append(f"[{kind} {mime}, about {len(data) * 3 // 4:,} bytes — "
                         "not shown]")
        elif kind == "resource":
            res = _attr(block, "resource")
            text = _attr(res, "text")
            uri = _attr(res, "uri", default="")
            parts.append(text if text is not None else f"[binary resource {uri}]")
        elif kind == "resource_link":
            parts.append(f"[resource {_attr(block, 'uri', default='')}]")
        else:
            parts.append(str(block))
    structured = _attr(result, "structured_content", "structuredContent")
    if structured and not any(p.strip() for p in parts):
        parts.append(json.dumps(structured, indent=1, ensure_ascii=False))
    is_error = bool(_attr(result, "is_error", "isError", default=False))
    return "\n".join(parts).strip(), is_error


# ----------------------------------------------------------------- manager

class MCPManager(QObject):
    """Owns every server connection. Thread-safe to call from workers."""

    changed = Signal()   # a server connected, failed or was stopped

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._states = {}          # name -> ServerState
        self._stops = {}           # name -> asyncio.Event
        self._tasks = {}           # name -> asyncio.Task
        self._specs = {}
        self._loop = None
        self._thread = None

    # -- loop thread -------------------------------------------------------

    def _ensure_loop(self):
        if self._loop is not None:
            return
        ready = threading.Event()

        def run():
            # Proactor (the default on Windows) is the loop that can start
            # subprocesses there.
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="mcp-loop", daemon=True)
        self._thread.start()
        ready.wait(5)

    @property
    def available(self) -> bool:
        return not MCP_IMPORT_ERROR

    # -- public API (GUI thread) ------------------------------------------

    def configure(self, servers: dict, force: bool = False):
        """Connect to servers; reconnect only those whose spec changed."""
        servers = servers or {}
        self._ensure_loop()
        old = self._specs
        self._specs = {n: dict(s) for n, s in servers.items()}
        for name in list(old):
            if force or name not in servers or old[name] != servers[name]:
                self._stop_server(name)
        for name, spec in servers.items():
            if spec.get("disabled"):
                self._stop_server(name)
                with self._lock:
                    self._states[name] = ServerState(name, "disabled")
                continue
            if not self.available and not is_openapi(spec):
                self._stop_server(name)
                with self._lock:
                    self._states[name] = ServerState(
                        name, "error", "The mcp package is not usable (it needs "
                        f"Python 3.10+): {MCP_IMPORT_ERROR}")
                continue
            running = name in self._tasks and not self._tasks[name].done()
            if force or not running or old.get(name) != spec:
                self._start_server(name, spec)
        with self._lock:
            for name in list(self._states):
                if name not in servers:
                    del self._states[name]
        self.changed.emit()

    def reconnect(self):
        self.configure(self._specs, force=True)

    def shutdown(self):
        if self._loop is None:
            return
        for name in list(self._tasks):
            self._stop_server(name)
        tasks = list(self._tasks.values())

        async def drain():
            if tasks:
                await asyncio.wait(tasks, timeout=3)

        try:
            asyncio.run_coroutine_threadsafe(drain(), self._loop).result(4)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    def states(self) -> list:
        with self._lock:
            return [ServerState(s.name, s.status, s.error, list(s.tools))
                    for s in self._states.values()]

    def tools(self) -> list:
        """Every tool on every connected server."""
        with self._lock:
            return [t for s in self._states.values() if s.status == "ready"
                    for t in s.tools]

    def find(self, key: str):
        return next((t for t in self.tools() if t.key == key), None)

    def summary(self) -> str:
        states = self.states()
        if not states:
            return ""
        ready = [s for s in states if s.status == "ready"]
        n_tools = sum(len(s.tools) for s in ready)
        bad = sum(1 for s in states if s.status == "error")
        busy = sum(1 for s in states if s.status == "connecting")
        text = f"{len(ready)}/{len([s for s in states if s.status != 'disabled'])} servers · {n_tools} tools"
        if busy:
            text += f" · {busy} connecting"
        if bad:
            text += f" · {bad} failed"
        return text

    # -- calling (worker threads) ----------------------------------------

    def call(self, key: str, arguments: dict, timeout: float = 120) -> tuple:
        """Run a tool and wait. Returns (text, is_error). Never raises."""
        tool = self.find(key)
        if tool is None:
            return f"Unknown tool “{key}”, or its server is not connected.", True
        if tool.endpoint:
            return openapi_tools.call(tool, arguments, timeout)
        with self._lock:
            session = self._states[tool.server].session
        if session is None:
            return f"Server “{tool.server}” is not connected.", True

        async def go():
            return await asyncio.wait_for(
                session.call_tool(tool.name, arguments or {}), timeout)

        fut = asyncio.run_coroutine_threadsafe(go(), self._loop)
        try:
            result = fut.result(timeout + 5)
        except (asyncio.TimeoutError, TimeoutError):
            fut.cancel()
            return f"The tool did not answer within {timeout:.0f} s.", True
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}", True
        return result_text(result)

    # -- per-server tasks (loop thread) -------------------------------------

    def _start_server(self, name, spec):
        with self._lock:
            self._states[name] = ServerState(name, "connecting")
        stop = asyncio.Event()

        def launch():
            self._stops[name] = stop
            self._tasks[name] = self._loop.create_task(self._serve(name, spec, stop))

        # create the task on the loop's own thread, then wait until it exists
        done = threading.Event()
        self._loop.call_soon_threadsafe(lambda: (launch(), done.set()))
        done.wait(2)

    def _stop_server(self, name):
        stop = self._stops.pop(name, None)
        self._tasks.pop(name, None)
        if stop is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(stop.set)
        with self._lock:
            if name in self._states:
                self._states[name].session = None

    def _set(self, name, stop, **kw):
        # A replaced server's old task must not overwrite the new one's state.
        if self._stops.get(name) is not stop:
            return
        with self._lock:
            st = self._states.get(name)
            if st is None:
                return
            for k, v in kw.items():
                setattr(st, k, v)
        self.changed.emit()

    async def _serve(self, name, spec, stop):
        if is_openapi(spec):
            await self._serve_openapi(name, spec, stop)
            return
        log = None
        try:
            async with contextlib.AsyncExitStack() as stack:
                if spec.get("url"):
                    streams = await stack.enter_async_context(self._http(spec))
                else:
                    log = open(config_dir() / f"mcp-{_safe(name)}.log", "a",
                               encoding="utf-8", errors="replace")
                    stack.callback(log.close)
                    streams = await stack.enter_async_context(
                        stdio_client(self._stdio_params(spec), errlog=log))
                read, write = streams[0], streams[1]
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                tools = await asyncio.wait_for(self._list_tools(name, session),
                                               CONNECT_TIMEOUT)
                self._set(name, stop, status="ready", error="", tools=tools,
                          session=session)
                await stop.wait()
        except asyncio.CancelledError:
            pass
        except BaseException as exc:  # noqa: BLE001 - incl. ExceptionGroup
            self._set(name, stop, status="error", error=_explain(exc, name),
                      session=None, tools=[])
            return
        self._set(name, stop, status="off", session=None, tools=[])

    async def _serve_openapi(self, name, spec, stop):
        """An OpenAPI server has no session to hold open: read its spec once,
        then calls are plain HTTP requests."""
        loop = asyncio.get_running_loop()
        try:
            tools = await loop.run_in_executor(
                None, openapi_tools.load_tools, name, spec)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "Connection" in type(exc).__name__ or "Max retries" in text:
                text = f"Could not reach {spec.get('url')}."
            self._set(name, stop, status="error", error=text[:400], tools=[])
            return
        self._set(name, stop, status="ready", error="", tools=tools,
                  session="openapi")
        try:
            await stop.wait()
        except asyncio.CancelledError:
            return
        self._set(name, stop, status="off", session=None, tools=[])

    @staticmethod
    def _stdio_params(spec):
        command = spec["command"]
        # npx, uvx and friends are .cmd files on Windows; resolve them so the
        # process starts without a shell.
        command = shutil.which(command) or command
        env = None
        if spec.get("env"):
            env = dict(os.environ)
            env.update({k: str(v) for k, v in spec["env"].items()})
        kw = {"command": command, "args": [str(a) for a in spec.get("args", [])],
              "env": env}
        if spec.get("cwd"):
            kw["cwd"] = spec["cwd"]
        return StdioServerParameters(**kw)

    @staticmethod
    def _http(spec):
        url = spec["url"]
        headers = spec.get("headers") or None
        kind = (spec.get("type") or spec.get("transport") or "").lower()
        if kind == "sse" or (not kind and url.rstrip("/").endswith("/sse")):
            from mcp.client.sse import sse_client
            return sse_client(url, headers=headers)
        from mcp.client import streamable_http as sh
        if hasattr(sh, "streamable_http_client"):          # SDK 2.x
            client = sh.create_mcp_http_client(headers=headers)
            return sh.streamable_http_client(url, http_client=client)
        return sh.streamablehttp_client(url, headers=headers)  # SDK 1.x

    @staticmethod
    async def _list_tools(server, session):
        tools, cursor, seen = [], None, set()
        while True:
            if cursor:
                try:
                    from mcp.types import PaginatedRequestParams
                    page = await session.list_tools(
                        params=PaginatedRequestParams(cursor=cursor))
                except (ImportError, TypeError):
                    page = await session.list_tools(cursor=cursor)
            else:
                page = await session.list_tools()
            for t in _attr(page, "tools", default=[]) or []:
                key = tool_key(server, t.name)
                while key in seen:
                    key = key[:TOOL_NAME_MAX - 2] + f"_{len(seen) % 100}"
                seen.add(key)
                ann = _attr(t, "annotations")
                tools.append(ToolInfo(
                    key=key, server=server, name=t.name,
                    description=(_attr(t, "description", default="") or "").strip(),
                    schema=_attr(t, "input_schema", "inputSchema",
                                 default=None) or {"type": "object", "properties": {}},
                    read_only=bool(_attr(ann, "read_only_hint", "readOnlyHint",
                                         default=False)),
                    destructive=bool(_attr(ann, "destructive_hint", "destructiveHint",
                                           default=False))))
            cursor = _attr(page, "next_cursor", "nextCursor")
            if not cursor:
                return tools


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60]


def _explain(exc, name) -> str:
    """One readable line from whatever the transport raised."""
    inner = exc
    while getattr(inner, "exceptions", None):   # ExceptionGroup from anyio
        inner = inner.exceptions[0]
    if isinstance(inner, (asyncio.TimeoutError, TimeoutError)):
        return f"Did not answer within {CONNECT_TIMEOUT} s."
    if isinstance(inner, FileNotFoundError):
        return (f"Command not found: {inner.filename or inner}. Is it installed "
                "and on PATH?")
    text = str(inner).strip() or type(inner).__name__
    if "Connection closed" in text:
        text += f" — the server exited; see mcp-{_safe(name)}.log in {config_dir()}"
    return f"{type(inner).__name__}: {text}"[:400]
