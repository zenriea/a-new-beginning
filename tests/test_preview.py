import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk import preview


def test_preview_console_handles_non_ascii_windows_paths(monkeypatch):
    import io
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stderr", stream)
    preview._utf8_console()
    print("Varsayılan Proje", flush=True)
    assert output.getvalue().decode("utf-8").splitlines() == ["Varsayılan Proje"]


def test_preview_environment_overrides_live_settings_and_credentials(tmp_path, monkeypatch):
    parent = {"PATH": "keep", "QD_DB_PATH": "production.db", "QD_ENV_FILE": "production.env",
              "QD_MARKET_DATA": "live", "QD_LLM_PROVIDER": "gemini", "QD_LLM_API_KEY": "secret",
              "QD_LLM_BASE_URL": "https://provider.invalid", "QD_SESSION_SECRET": "live-secret",
              "ALPACA_API_KEY_ID": "live-key", "ANTHROPIC_API_KEY": "secret", "PORT": "9999"}
    env = preview.preview_environment(tmp_path, 8127, parent)
    assert parent["QD_DB_PATH"] == "production.db"  # input isn't mutated
    assert env["PATH"] == "keep" and "PORT" not in env
    assert "secret" not in str(env) and "live-key" not in str(env)
    monkeypatch.setattr(os, "environ", env)
    from quantdesk.config import load_config
    cfg = load_config(reload=True)
    assert cfg.db_path == tmp_path / "quantdesk.db"
    assert cfg.market_data_mode == "synthetic" and cfg.demo_llm and not cfg.alpaca_configured
    assert cfg.bind_host == "127.0.0.1" and cfg.bind_port == 8127
    assert not cfg.admin_password and not cfg.cookie_secure


def test_preview_refuses_to_adopt_an_unmarked_database(tmp_path):
    db = tmp_path / "quantdesk.db"
    db.write_bytes(b"do not touch this account")
    with pytest.raises(RuntimeError, match="without a preview marker"):
        preview.check_preview_directory(tmp_path)
    assert db.read_bytes() == b"do not touch this account"
    assert not (tmp_path / ".quantdesk-preview").exists()


def test_preview_directory_creation_is_idempotent(tmp_path):
    path = tmp_path / "separate-demo"
    preview.check_preview_directory(path)
    preview.check_preview_directory(path)
    assert (path / ".quantdesk-preview").read_text(encoding="utf-8") == preview.MARKER
    (path / ".quantdesk-preview").write_text("wrong version", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unrecognized preview marker"):
        preview.check_preview_directory(path)


def test_preview_initializes_once_and_preserves_operator_edits(env, monkeypatch):
    from quantdesk import db, settings
    monkeypatch.setattr(os, "environ", preview.preview_environment(env.data_dir / "preview", 8127))
    from quantdesk.config import load_config
    load_config(reload=True)
    seeds = []
    monkeypatch.setattr(preview, "_seed_history", lambda: seeds.append(True))
    preview.prepare_database()
    assert settings.get("universe") == preview.SYMBOLS
    assert settings.get("schedule")["quote_poll_sec"] == 5
    settings.operator_update("agent", {"paused": True})
    preview.prepare_database()
    assert seeds == [True] and settings.get("agent")["paused"]
    assert db.get_meta("local_preview_seeded") == "1"


def test_preview_rejects_live_configuration_before_writing(env, monkeypatch):
    from quantdesk.config import load_config
    load_config(reload=True, market_data_mode="live")
    with pytest.raises(RuntimeError, match="safety configuration"):
        preview.prepare_database()
    assert not (env.data_dir / ".quantdesk-preview").exists()


def test_preview_stops_only_its_worker(monkeypatch):
    import signal
    calls = []
    worker = SimpleNamespace(poll=lambda: None, send_signal=lambda sig: calls.append(sig),
                             wait=lambda timeout: calls.append(timeout))
    preview.stop_worker(worker)
    assert calls == [signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM, 35]


def test_preview_shutdown_falls_back_without_a_windows_console():
    calls = []

    def no_console(sig):
        raise OSError("no console")

    worker = SimpleNamespace(poll=lambda: None, send_signal=no_console,
                             terminate=lambda: calls.append("terminate"),
                             wait=lambda timeout: calls.append(timeout))
    preview.stop_worker(worker)
    assert calls == ["terminate", 35]


def test_busy_preview_port_does_not_initialize_or_touch_a_database(tmp_path, monkeypatch):
    import socket
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(preview, "PREVIEW_DIR", tmp_path / "must-not-be-created")
    monkeypatch.setattr(preview, "_configure_history", lambda: None)
    monkeypatch.setattr("sys.argv", ["preview", "--no-browser", "--port", str(port)])
    try:
        with pytest.raises(SystemExit) as error:
            preview.main()
        assert error.value.code == 1
        assert not preview.PREVIEW_DIR.exists()
    finally:
        busy.close()


def test_windows_launchers_use_quoted_project_relative_python():
    root = Path(__file__).resolve().parents[1]
    for name in ("Start Local Preview.cmd", "Run Tests.cmd"):
        text = (root / name).read_text(encoding="utf-8")
        assert 'cd /d "%~dp0"' in text
        assert '".venv\\Scripts\\python.exe"' in text and "PYTHONUTF8=1" in text
    tests = (root / "Run Tests.cmd").read_text()
    assert "QD_DB_PATH=" in tests and "QD_LLM_PROVIDER=none" in tests
