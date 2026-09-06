# -*- coding: utf-8 -*-
"""Мок-тесты runtime-верификатора (без реальных npm/браузера)."""
import sys
from pathlib import Path
from core.runtime_verify import RuntimeVerifier


def test_build_skipped_without_package_json():
    tmp = Path(tempfile.mkdtemp())
    rv = RuntimeVerifier(tmp, browser_tool=None, build_tool_factory=None)
    r = rv.check_build()
    assert r["skipped"] and r["reason"] == "нет package.json"


def test_build_fail_reported():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "package.json").write_text("{}", encoding="utf-8")
    (tmp / "angular.json").write_text("{}", encoding="utf-8")

    class FakeBuild:
        def execute(self, action, args):
            return {"ok": False, "error": "TS2349: not callable"}

    rv = RuntimeVerifier(tmp, build_tool_factory=lambda: FakeBuild())
    r = rv.check_build()
    assert not r["skipped"] and r["ok"] is False
    assert "TS2349" in r["summary"]


def test_full_check_ok_when_all_skipped():
    tmp = Path(tempfile.mkdtemp())
    rv = RuntimeVerifier(tmp)
    r = rv.full_check(dev_port=None)
    assert r["ok"] is True  # всё skipped -> не провалено


def test_serve_failure_fails_verdict():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "package.json").write_text("{}", encoding="utf-8")

    class FakeBrowser:
        def execute(self, action, args):
            return {"ok": False, "error": "timeout"}

    rv = RuntimeVerifier(tmp, browser_tool=FakeBrowser(),
                         build_tool_factory=None)
    rv.set_server_callbacks(lambda port: None, lambda: None)
    r = rv.check_serve_and_open(port=4999, wait_sec=2)
    assert not r["skipped"] and r["ok"] is False


def test_serve_ok_with_clean_console():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "package.json").write_text("{}", encoding="utf-8")

    class FakeBrowser:
        def execute(self, action, args):
            if action == "navigate":
                return {"ok": True}
            if action == "console":
                return {"data": {"messages": []}}
            if action == "content":
                return {"data": {"text": "Дашборд Программа Progress"}}
            return {"ok": False}

    started = {}
    rv = RuntimeVerifier(tmp, browser_tool=FakeBrowser(),
                         build_tool_factory=None)
    rv.set_server_callbacks(
        lambda port: started.setdefault("port", port),
        lambda: started.update(stopped=True))
    r = rv.check_serve_and_open(port=4200)
    assert r["ok"] is True and r["spa_content_detected"]
    assert started.get("port") == 4200 and started.get("stopped") is True


import tempfile  # noqa: E402  (внизу чтобы не мешать мокам сверху — ок для теста)
