"""
Тесты для решения по автопроверке шага в workflows/main_graph.py.

Раньше этого файла не было вообще. Реальный прогон (задача 5d3928f13036)
показал: шаги 7-8 (write_excel/write_geojson) провалились по существу
(инструменты не вызваны или упали с ошибкой), core.step_check.check_step
корректно проставил suspicious=True, но run_step() после исчерпания попыток
всё равно возвращал шаг как ok=True/status="done" — сборщик итога принимал
догадку модели за факт и синтезировал неверное объяснение пользователю
("отсутствие инструментов для записи GeoJSON", хотя инструмент есть в коде).

Полный run_step() слишком завязан на Proposer/Router/ThreadPoolExecutor/
Blackboard, чтобы тестировать его целиком без тяжёлого мокирования. Сама
мутация ok/error после исчерпания попыток вынесена в чистую функцию
_apply_autocheck_verdict — её тестируем в изоляции, без вызова run_step.
"""
from __future__ import annotations

from core.schemas import StepResult
from workflows.main_graph import _apply_autocheck_verdict


def test_apply_autocheck_verdict_marks_suspicious_step_as_failed():
    candidate = StepResult(
        step_id="s8", title="write_geojson", assignee="datafiles",
        model="qwen3.6:35b-a3b", answer="Инструмента для записи GeoJSON нет.",
        ok=True, suspicious=True,
        warnings=["инструменты datafiles были доступны, но ни один не вызван"],
    )
    result = _apply_autocheck_verdict(candidate, max_attempts=2)
    assert result.ok is False
    assert "2 попыток" in result.error
    assert "ни один не вызван" in result.error


def test_apply_autocheck_verdict_joins_multiple_warnings():
    candidate = StepResult(
        step_id="s7", title="write_excel", ok=True, suspicious=True,
        warnings=["JSONDecodeError на позиции 262", "оба вызова провалились"],
    )
    result = _apply_autocheck_verdict(candidate, max_attempts=2)
    assert result.ok is False
    assert "JSONDecodeError на позиции 262" in result.error
    assert "оба вызова провалились" in result.error


def test_apply_autocheck_verdict_returns_same_object():
    """Мутирует и возвращает тот же объект (StepResult не frozen) —
    вызывающий код (run_step) переприсваивает candidate = ...(candidate, ...)
    и полагается на то, что это тот же экземпляр, а не копия."""
    candidate = StepResult(step_id="s1", ok=True, suspicious=True, warnings=["w"])
    result = _apply_autocheck_verdict(candidate, max_attempts=3)
    assert result is candidate
