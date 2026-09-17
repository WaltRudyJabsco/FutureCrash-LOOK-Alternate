"""The only Ollama client in the system.

LO, Future Crash, reflection, benchmarks and `lk models` all talk to Ollama
through this module, and all of them pick the host from the same place.
"""
from __future__ import annotations

import concurrent.futures
import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import store
from ..log import log
from ..paths import roots
from ..settings import secret, settings

LOCAL = "http://127.0.0.1:11434"


class OllamaError(RuntimeError):
    pass


def normalize_url(raw: str) -> str:
    text = str(raw or "").strip().rstrip("/")
    if not text:
        return LOCAL
    if "://" not in text:
        text = "http://" + text
    host_part = text.split("://", 1)[1]
    if ":" not in host_part.split("/", 1)[0] and text.startswith("http://"):
        text += ":11434"
    return text


class Ollama:
    def __init__(self, base: Optional[str] = None, timeout: float = 600.0):
        self.base = normalize_url(base or current_host()[1])
        self.timeout = timeout
        self._caps: Dict[str, Optional[List[str]]] = {}

    # ---- plumbing --------------------------------------------------------
    def _request(self, path: str, payload: Optional[dict] = None, timeout: Optional[float] = None, method: Optional[str] = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = json.loads(exc.read().decode() or "{}").get("error", "")
            except (ValueError, OSError):
                pass
            raise OllamaError(body or f"HTTP {exc.code} from {self.base}{path}") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise OllamaError(f"cannot reach Ollama at {self.base}: {reason}") from None

    def _json(self, path: str, payload: Optional[dict] = None, timeout: float = 10.0) -> dict:
        with self._request(path, payload, timeout) as resp:
            return json.load(resp)

    # ---- inspection ------------------------------------------------------
    def alive(self, timeout: float = 1.5) -> bool:
        try:
            self._json("/api/version", timeout=timeout)
            return True
        except OllamaError:
            return False

    def version(self) -> str:
        return str(self._json("/api/version", timeout=3).get("version", "?"))

    def models(self) -> List[dict]:
        return list(self._json("/api/tags", timeout=6).get("models") or [])

    def model_names(self) -> List[str]:
        return [str(m.get("name") or m.get("model")) for m in self.models() if m.get("name") or m.get("model")]

    def running(self) -> List[dict]:
        try:
            return list(self._json("/api/ps", timeout=4).get("models") or [])
        except OllamaError:
            return []

    def capabilities(self, model: str) -> Optional[List[str]]:
        """None means the server is too old to say; treat as 'tools probably work'."""
        if model not in self._caps:
            try:
                info = self._json("/api/show", {"model": model}, timeout=8)
                caps = info.get("capabilities")
                self._caps[model] = list(caps) if isinstance(caps, list) else None
            except OllamaError:
                self._caps[model] = None
        return self._caps[model]

    def supports(self, model: str, capability: str) -> bool:
        caps = self.capabilities(model)
        if caps is None:
            return capability == "tools"
        return capability in caps

    # ---- inference -------------------------------------------------------
    def chat_stream(self, model: str, messages: List[dict], tools: Optional[List[dict]] = None,
                    think: Optional[bool] = None, options: Optional[dict] = None,
                    keep_alive: Optional[str] = None) -> Iterator[dict]:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        if options:
            payload["options"] = options
        if keep_alive:
            payload["keep_alive"] = keep_alive
        resp = self._request("/api/chat", payload)
        try:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(str(event["error"]))
                yield event
        finally:
            resp.close()

    def chat_raw(self, model: str, messages: List[dict], tools: Optional[List[dict]] = None,
                 options: Optional[dict] = None, think: Optional[bool] = False, timeout: float = 300.0) -> dict:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think
        return self._json("/api/chat", payload, timeout=timeout)

    def chat(self, model: str, messages: List[dict], format: Optional[Any] = None, think: Optional[bool] = False,
             options: Optional[dict] = None, timeout: float = 180.0) -> str:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if format is not None:
            payload["format"] = format
        if think is not None:
            payload["think"] = think
        if options:
            payload["options"] = options
        data = self._json("/api/chat", payload, timeout=timeout)
        return str((data.get("message") or {}).get("content") or "")


# ---- hosts --------------------------------------------------------------

def load_hosts() -> dict:
    data = store.read_json(roots.hosts, None)
    if not isinstance(data, dict):
        data = {"default": "local", "hosts": {}}
    data.setdefault("default", "local")
    data.setdefault("hosts", {})
    return data


def save_hosts(data: dict) -> None:
    store.write_json(roots.hosts, data)


def resolve_host(name: str) -> Tuple[str, Optional[str]]:
    if name in {"local", "localhost", ""}:
        return "local", LOCAL
    hosts = load_hosts()["hosts"]
    if name in hosts:
        return name, normalize_url(hosts[name].get("url", ""))
    for key, value in hosts.items():
        if key.casefold().startswith(name.casefold()):
            return key, normalize_url(value.get("url", ""))
    return name, None


def current_host() -> Tuple[str, str]:
    import os
    if os.environ.get("OLLAMA_HOST"):
        return "env", normalize_url(os.environ["OLLAMA_HOST"])
    name, url = resolve_host(load_hosts().get("default", "local"))
    return (name, url) if url else ("local", LOCAL)


def tailscale_peers() -> List[dict]:
    exe = shutil.which("tailscale")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=3)
        status = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    peers = status.get("Peer") or {}
    out = []
    for node in (peers.values() if isinstance(peers, dict) else []):
        if not isinstance(node, dict) or node.get("Online") is False:
            continue
        name = str(node.get("HostName") or str(node.get("DNSName") or "").split(".")[0]).strip()
        for ip in (node.get("TailscaleIPs") or [])[:1]:
            if name:
                out.append({"name": name, "url": f"http://{ip}:11434"})
    return out


def discover_hosts() -> List[dict]:
    candidates = tailscale_peers()
    found = []
    if not candidates:
        return found
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(candidates))) as pool:
        futures = {pool.submit(Ollama(c["url"]).alive, 0.6): c for c in candidates}
        for fut, cand in futures.items():
            try:
                if fut.result():
                    found.append(cand)
            except Exception as exc:  # boundary: one bad peer must not break discovery
                log.warn("ollama.discover", f"{cand['url']}: {exc}")
    return sorted(found, key=lambda c: c["name"].casefold())


def ensure_local_server(client: Ollama, wait: float = 8.0) -> bool:
    if client.alive():
        return True
    if not client.base.startswith(("http://127.0.0.1", "http://localhost")):
        return False
    exe = shutil.which("ollama")
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        log.warn("ollama.serve", str(exc))
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if client.alive(0.5):
            return True
        time.sleep(0.25)
    return False


def active_model() -> str:
    return settings.get("ai.model")


def web_search(query: str, max_results: int = 5) -> dict:
    key = secret("OLLAMA_API_KEY")
    if not key:
        raise OllamaError("web search needs an Ollama API key: lk web key")
    req = urllib.request.Request(
        "https://ollama.com/api/web_search",
        data=json.dumps({"query": query, "max_results": max_results}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaError(f"web search failed: {exc}") from None
