"""TELEGRAM_API_BASE / TELEGRAM_API_FILES_DIR: self-hosted Bot API server support (#1812).

Hermetic: no request leaves the machine; a loopback httpx transport stands in
for the Bot API server, and the files dir is a tmp_path copy. Mirrors the
TELEGRAM_PROXY test module's loader.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1] / "skills" / "telegram"
_PACKAGE = "telegram_api_base_test"
_TOKEN = "123456:SECRET-BOT-TOKEN"
_BASE = "http://127.0.0.1:8081"


def _load():
    for name in [key for key in sys.modules if key == _PACKAGE or key.startswith(f"{_PACKAGE}.")]:
        sys.modules.pop(name, None)
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE}.plugin", _ROOT / "plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    assert spec.loader is not None
    spec.loader.exec_module(plugin)
    return sys.modules[f"{_PACKAGE}.lib.telegram_api"], sys.modules[f"{_PACKAGE}.scripts.telegram_settings"]


api_mod, settings_mod = _load()


def _client(**kwargs):
    return api_mod.TelegramClient(_TOKEN, **kwargs)


# --- validator ---------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("", "https://api.telegram.org"),
    (None, "https://api.telegram.org"),
    ("http://127.0.0.1:8081", "http://127.0.0.1:8081"),
    ("http://127.0.0.1:8081/", "http://127.0.0.1:8081"),
    ("https://tapi.lan", "https://tapi.lan"),
])
def test_api_base_valid(value, expected):
    assert settings_mod.validate_telegram_api_base(value) == expected


@pytest.mark.parametrize("value", [
    "127.0.0.1:8081",              # no scheme
    "ftp://127.0.0.1",             # bad scheme
    "http://user:pass@127.0.0.1",  # credentials
    "http://127.0.0.1/bot",        # path
    "http://127.0.0.1:8081/?q=1",  # query
    "http://127.0.0.1:8081/#f",    # fragment
    "http://127.0.0.1:99999",      # port range
])
def test_api_base_invalid(value):
    with pytest.raises(ValueError):
        settings_mod.validate_telegram_api_base(value)


def test_files_dir_validator():
    assert settings_mod.validate_telegram_files_dir("") is None
    assert settings_mod.validate_telegram_files_dir(None) is None
    assert settings_mod.validate_telegram_files_dir("/srv/tba") == "/srv/tba"
    assert settings_mod.validate_telegram_files_dir("/srv/tba/") == "/srv/tba"
    with pytest.raises(ValueError):
        settings_mod.validate_telegram_files_dir("relative/dir")


# --- endpoints ---------------------------------------------------------------

def test_default_endpoints_unchanged():
    c = _client()
    assert c.api_base == f"https://api.telegram.org/bot{_TOKEN}"
    assert c.file_base == f"https://api.telegram.org/file/bot{_TOKEN}"
    assert c.files_dir is None


def test_custom_endpoints():
    c = _client(api_base_url=_BASE, files_dir="/srv/tba")
    assert c.api_base == f"{_BASE}/bot{_TOKEN}"
    assert c.file_base == f"{_BASE}/file/bot{_TOKEN}"
    assert c.files_dir == "/srv/tba"


# --- live call routing + disk fallback ----------------------------------------

def _fake_server(responses):
    """Loopback transport: records requests, replies from `responses`."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return responses.get(request.url.path.rsplit("/", 1)[-1], httpx.Response(404, text="nf"))

    transport = httpx.MockTransport(handler)
    return transport, calls


def test_call_hits_custom_base():
    transport, calls = _fake_server({"getMe": httpx.Response(200, json={"ok": True, "result": {}})})
    c = _client(api_base_url=_BASE)

    async def run():
        async with httpx.AsyncClient(transport=transport) as _:
            # patch the client factory the same way call() uses it
            orig = api_mod.httpx.AsyncClient
            api_mod.httpx.AsyncClient = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
            try:
                payload = await c.call("getMe")
            finally:
                api_mod.httpx.AsyncClient = orig
            return payload

    payload = asyncio.run(run())
    assert payload["ok"] is True
    assert calls and calls[0].startswith(_BASE)


def test_download_falls_back_to_disk(tmp_path):
    # HTTP says 404 (local server behavior, tdlib#26); the file exists on disk.
    store = tmp_path / "store"
    (store / "documents").mkdir(parents=True)
    payload = b"file-bytes-from-disk"
    (store / "documents" / "file_1.bin").write_bytes(payload)
    transport, calls = _fake_server({"file_1.bin": httpx.Response(404, text="not found")})
    c = _client(api_base_url=_BASE, files_dir=str(store))

    async def run():
        orig = api_mod.httpx.AsyncClient
        api_mod.httpx.AsyncClient = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
        try:
            return await c._download_bytes("documents/file_1.bin")
        finally:
            api_mod.httpx.AsyncClient = orig

    assert asyncio.run(run()) == payload


def test_download_without_files_dir_raises(tmp_path):
    transport, _ = _fake_server({"file_1.bin": httpx.Response(404, text="not found")})
    c = _client(api_base_url=_BASE)  # no files_dir

    async def run():
        orig = api_mod.httpx.AsyncClient
        api_mod.httpx.AsyncClient = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
        try:
            return await c._download_bytes("documents/file_1.bin")
        finally:
            api_mod.httpx.AsyncClient = orig

    with pytest.raises(RuntimeError):
        asyncio.run(run())


def test_download_traversal_stays_in_store(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"nope")
    store = tmp_path / "store"
    store.mkdir()
    transport, _ = _fake_server({"file_1.bin": httpx.Response(404, text="nf")})
    c = _client(api_base_url=_BASE, files_dir=str(store))

    async def run():
        orig = api_mod.httpx.AsyncClient
        api_mod.httpx.AsyncClient = lambda **kw: orig(transport=transport, **{k: v for k, v in kw.items() if k != "transport"})
        try:
            return await c._download_bytes("../secret.txt")
        finally:
            api_mod.httpx.AsyncClient = orig

    with pytest.raises(RuntimeError):
        asyncio.run(run())
