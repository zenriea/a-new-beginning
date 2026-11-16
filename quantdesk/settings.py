"""Operator settings store.

Reads: anyone. Writes: ONLY ``operator_update`` - called exclusively by the authenticated
operator API (quantdesk/api/app.py). The agent package never imports ``operator_update``;
tests/test_risk.py enforces that by scanning the agent sources.
"""
from __future__ import annotations

import copy

from . import clock, db
from .config import DEFAULT_SETTINGS, SETTING_BOUNDS, load_config


def _merged(key: str) -> dict:
    base = copy.deepcopy(DEFAULT_SETTINGS[key])
    row = db.q1("SELECT value FROM settings WHERE key=?", (key,))
    if row:
        stored = db.loads(row["value"], {})
        if isinstance(base, dict) and isinstance(stored, dict):
            _deep_update(base, stored)
        else:
            base = stored
    return base


def _deep_update(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def get(key: str) -> dict:
    return _merged(key)


def all_settings() -> dict:
    return {k: _merged(k) for k in DEFAULT_SETTINGS}


class SettingsError(ValueError):
    pass


def _bound(value, bound):
    cfg = load_config()
    return getattr(cfg, bound) if isinstance(bound, str) else bound


def validate(key: str, value: dict) -> dict:
    if key not in DEFAULT_SETTINGS:
        raise SettingsError(f"unknown settings group {key!r}")
    current = _merged(key)
    if not isinstance(value, dict):
        raise SettingsError("settings value must be an object")
    for sub, v in value.items():
        if isinstance(DEFAULT_SETTINGS[key], dict) and sub not in DEFAULT_SETTINGS[key]:
            raise SettingsError(f"unknown setting {key}.{sub}")
        if isinstance(DEFAULT_SETTINGS[key].get(sub), bool) and not isinstance(v, bool):
            raise SettingsError(f"{key}.{sub} must be a boolean")
        bounds = SETTING_BOUNDS.get((key, sub))
        if bounds is not None:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise SettingsError(f"{key}.{sub} must be a number")
            lo, hi = _bound(v, bounds[0]), _bound(v, bounds[1])
            if not (lo <= v <= hi):
                raise SettingsError(f"{key}.{sub}={v} outside allowed range [{lo}, {hi}]")
    merged = copy.deepcopy(current)
    _deep_update(merged, value)
    return merged


def operator_update(key: str, value: dict, actor: str = "operator") -> dict:
    """Persist an operator edit with an audit record. Never called by agent code."""
    merged = validate(key, value)
    ts = clock.now()
    with db.tx():
        db.ex("INSERT INTO settings(key, value, updated_at, updated_by) VALUES(?,?,?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
              "updated_by=excluded.updated_by", (key, db.dumps(merged), ts, actor))
        db.ex("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
              (ts, actor, f"settings.{key}", db.dumps(value)))
    return merged
