"""Tools from an OpenAPI server, such as mcpo.

mcpo (github.com/open-webui/mcpo) puts MCP servers behind a REST API for
Open WebUI: each tool becomes POST /<server>/<tool> with a JSON body, and
/<server>/openapi.json describes them. Reading that spec gives the same tool
list the MCP server has, so the servers already running behind mcpo can be
used here without exposing MCP itself.

Configured as a server entry with "type": "openapi":

    "mcpo": {"type": "openapi", "url": "http://10.46.252.18:8000",
             "headers": {"Authorization": "Bearer ${MCPO_API_KEY}"}}

The url may be one server's base (…:8000/searxng), its spec
(…/searxng/openapi.json), or the root of an mcpo started with a config
file, in which case every server it lists is picked up.
"""

import json
import os
import re

import requests

FETCH_TIMEOUT = (10, 30)
METHODS = ("post", "get", "put", "patch", "delete")
# mcpo's root spec lists its servers as links in the description.
ROOT_LINK = re.compile(r"\]\(/([^)/\s]+)/docs\)")


def is_openapi(spec: dict) -> bool:
    return str(spec.get("type") or spec.get("transport") or "").lower() in (
        "openapi", "mcpo")


def _expand(value):
    """${VAR}, $VAR (and %VAR% on Windows) from the environment, so a key
    can stay out of the config file."""
    return os.path.expandvars(value) if isinstance(value, str) else value


def _headers(spec: dict) -> dict:
    h = {str(k): str(_expand(v)) for k, v in (spec.get("headers") or {}).items()}
    if spec.get("api_key"):
        h.setdefault("Authorization", f"Bearer {_expand(spec['api_key'])}")
    return h


def _base(url: str) -> str:
    url = _expand(url).rstrip("/")
    for tail in ("/openapi.json", "/docs"):
        if url.endswith(tail):
            url = url[:-len(tail)]
    return url


def _get_spec(base: str, headers: dict) -> dict:
    resp = requests.get(f"{base}/openapi.json", headers=headers, timeout=FETCH_TIMEOUT)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"{resp.status_code} from {base}/openapi.json — check "
                           "the API key in this server's \"headers\".")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} from {base}/openapi.json")
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"{base}/openapi.json is not JSON — is this an "
                           "OpenAPI server?") from None


def load_tools(server: str, spec: dict) -> list:
    """Every operation the server (or each server under an mcpo root) offers."""
    from mcp_client import TOOL_NAME_MAX, ToolInfo, tool_key

    headers = _headers(spec)
    base = _base(spec["url"])
    doc = _get_spec(base, headers)
    sources = [(base, doc, "")]
    if not doc.get("paths"):
        subs = ROOT_LINK.findall((doc.get("info") or {}).get("description", ""))
        if not subs:
            raise RuntimeError(f"{base}/openapi.json lists no operations.")
        sources = []
        for sub in dict.fromkeys(subs):
            sources.append((f"{base}/{sub}", _get_spec(f"{base}/{sub}", headers),
                            sub))

    tools, seen = [], set()
    for sub_base, sub_doc, prefix in sources:
        root = _server_url(sub_base, sub_doc)
        for path, ops in (sub_doc.get("paths") or {}).items():
            for method in METHODS:
                op = (ops or {}).get(method)
                if not isinstance(op, dict):
                    continue
                name = path.strip("/").replace("/", "_") or op.get("operationId", "call")
                if prefix:
                    name = f"{prefix}_{name}"
                key = tool_key(server, name)
                while key in seen:
                    key = key[:TOOL_NAME_MAX - 3] + f"_{len(seen) % 100}"
                seen.add(key)
                schema, query = _input_schema(op, sub_doc)
                desc = (op.get("description") or op.get("summary") or "").strip()
                tools.append(ToolInfo(
                    key=key, server=server, name=name, description=desc,
                    schema=schema, read_only=(method == "get"),
                    endpoint={"method": method.upper(), "url": root + path,
                              "query": query, "headers": headers}))
    return tools


def _server_url(base: str, doc: dict) -> str:
    # mcpo writes a relative "servers" entry (/searxng) that is already part
    # of base; only an absolute one says the API lives somewhere else.
    for s in doc.get("servers") or []:
        url = str(s.get("url", ""))
        if url.startswith(("http://", "https://")):
            return url.rstrip("/")
    return base


def _input_schema(op: dict, doc: dict):
    """(JSON schema of the arguments, sent_as_query)."""
    body = (((op.get("requestBody") or {}).get("content") or {})
            .get("application/json") or {}).get("schema")
    if body:
        schema = _resolve(body, doc)
        if schema.get("type") == "object" or "properties" in schema:
            schema.setdefault("type", "object")
            return schema, False
        # A body that is not an object: wrap it so the model passes {"body": …}
        return {"type": "object", "properties": {"body": schema},
                "required": ["body"]}, False
    props, required = {}, []
    for p in op.get("parameters") or []:
        p = _resolve(p, doc)
        if p.get("in") not in ("query", "path"):
            continue
        s = _resolve(p.get("schema") or {}, doc)
        if p.get("description"):
            s["description"] = p["description"]
        props[p["name"]] = s
        if p.get("required"):
            required.append(p["name"])
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema, True


def _resolve(node, doc, depth=0):
    """Inline $refs and drop noise (titles, empty descriptions)."""
    if depth > 12:
        return {}
    if isinstance(node, list):
        return [_resolve(x, doc, depth + 1) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = doc
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        merged = dict(target)
        merged.update({k: v for k, v in node.items() if k != "$ref"})
        return _resolve(merged, doc, depth + 1)
    out = {}
    for k, v in node.items():
        if k == "title" or (k == "description" and not v):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _resolve(pv, doc, depth + 1) for pk, pv in v.items()}
        else:
            out[k] = _resolve(v, doc, depth + 1)
    return out


def call(tool, arguments: dict, timeout: float) -> tuple:
    """Run one operation. Returns (text, is_error). Never raises."""
    ep = tool.endpoint
    args = arguments or {}
    if "body" in args and set((tool.schema.get("properties") or {})) == {"body"}:
        payload = args["body"]
    else:
        payload = args
    kw = {"headers": ep["headers"], "timeout": (10, timeout)}
    if ep["query"]:
        kw["params"] = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                        for k, v in args.items()}
    elif tool.schema.get("properties"):
        kw["json"] = payload
    try:
        url = ep["url"]
        for k, v in args.items():          # path parameters, if any
            url = url.replace("{" + k + "}", requests.utils.quote(str(v), safe=""))
        resp = requests.request(ep["method"], url, **kw)
    except requests.exceptions.Timeout:
        return f"The tool did not answer within {timeout:.0f} s.", True
    except requests.exceptions.ConnectionError:
        return f"Could not reach {ep['url']}.", True
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", True

    if resp.status_code >= 400:
        return _error(resp), True
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip(), False
    if isinstance(data, str):
        return data, False
    return json.dumps(data, indent=1, ensure_ascii=False), False


def _error(resp) -> str:
    code = resp.status_code
    try:
        detail = resp.json().get("detail", resp.json())
    except (ValueError, AttributeError):
        detail = (resp.text or "").strip()[:600]
    if code in (401, 403):
        return (f"HTTP {code}: {detail if isinstance(detail, str) else 'unauthorized'}"
                " — the API key in this server's \"headers\" is missing or wrong.")
    if code == 422 and isinstance(detail, list):
        lines = []
        for d in detail[:10]:
            loc = ".".join(str(x) for x in d.get("loc", []) if x != "body")
            lines.append(f"{loc or 'body'}: {d.get('msg', d)}")
        return "Invalid arguments:\n" + "\n".join(lines)
    if isinstance(detail, dict):
        detail = detail.get("error") or detail.get("message") or json.dumps(detail)
    return f"HTTP {code}: {detail}"[:1500]
