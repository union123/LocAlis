"""
Регрессионные тесты для core/tool_parsing.py.

Контекст: обнаружено 20.08.2026 при разборе падения задачи 87eef3701975,
шаг s10 (proposer_a / gemma4:26b) — пустой ответ, ok=0, автопроверка
отклонила результат как "инструменты были доступны, но ни один не вызван".
Латентность шага — ~43 минуты при пустом content, что соответствует
внешне документированному поведению Gemma4 в Ollama: во время стриминга
JSON вызова инструмента может попадать в поле "thinking"/"reasoning",
а "content" остаётся пустым (note.com/ezark_company, апрель 2026).
extract_tool_calls() до фикса проверял только tool_calls и content —
такой "утёкший" в размышления вызов инструмента был не виден парсеру.
"""

from __future__ import annotations

from core.tool_parsing import extract_tool_calls, parse_text_tool_calls

KNOWN = {"datafiles__save_geojson", "files__read_csv"}


def test_extract_tool_calls_native_field():
    message = {
        "content": "",
        "tool_calls": [
            {"function": {"name": "datafiles__save_geojson", "arguments": {"path": "out.geojson"}}}
        ],
    }
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1
    assert calls[0].tool == "datafiles"
    assert calls[0].action == "save_geojson"


def test_extract_tool_calls_json_in_content():
    message = {"content": '{"name": "files__read_csv", "arguments": {"path": "samples.csv"}}'}
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1
    assert calls[0].tool == "files"
    assert calls[0].action == "read_csv"


def test_extract_tool_calls_falls_back_to_thinking_field_when_content_empty():
    """
    Регрессия на баг Gemma4/Ollama: content пуст, но вызов инструмента
    присутствует в поле "thinking" — раньше это давало [] и пустой ответ.
    """
    message = {
        "content": "",
        "thinking": (
            "Мне нужно сохранить результат. "
            '{"name": "datafiles__save_geojson", "arguments": {"path": "points_enriched.geojson"}}'
        ),
    }
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1
    assert calls[0].tool == "datafiles"
    assert calls[0].action == "save_geojson"


def test_extract_tool_calls_falls_back_to_reasoning_field_when_content_empty():
    message = {
        "content": "",
        "reasoning": '<tool_call>{"name": "files__read_csv", "arguments": {"path": "boreholes.csv"}}</tool_call>',
    }
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1
    assert calls[0].tool == "files"
    assert calls[0].action == "read_csv"


def test_extract_tool_calls_does_not_fall_back_when_content_present_but_no_call():
    """
    Если content непустой, но реального вызова там нет (модель просто
    ответила текстом, как qwen3.6:35b-a3b на шаге s8 задачи 87eef3701975) —
    fallback в thinking/reasoning НЕ должен подхватывать случайные
    вызовы оттуда: пустой ответ в этом случае — корректное поведение
    (автопроверка сама решит, что делать с догадкой без вызова инструмента).
    """
    message = {
        "content": "Судя по данным, возраст пробы соответствует диапазону K2gm.",
        "thinking": '{"name": "files__read_csv", "arguments": {"path": "samples.csv"}}',
    }
    calls = extract_tool_calls(message, KNOWN)
    assert calls == []


def test_extract_tool_calls_empty_message_returns_empty_list():
    assert extract_tool_calls({}, KNOWN) == []
    assert extract_tool_calls({"content": ""}, KNOWN) == []


def test_parse_text_tool_calls_respects_limit():
    text = " ".join(
        f'{{"name": "files__read_csv", "arguments": {{"path": "f{i}.csv"}}}}' for i in range(10)
    )
    calls = parse_text_tool_calls(text, KNOWN, limit=4)
    assert len(calls) == 4
