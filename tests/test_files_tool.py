"""
Тесты поиска расположения файлов.

Закрепляют поведение, добавленное после жалобы 05.08 «моделям трудновато
находить расположения файлов»: на этом компьютере папки Desktop нет,
рабочий стол перенаправлен в OneDrive, и модель, подставляя стандартный
путь, получала «не найдено» и выдумывала ответ.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.fs_paths import known_folders, normalize
from core.schemas import ToolCallRequest
from tools.files.files_tool import FilesTool


@pytest.fixture()
def tool() -> FilesTool:
    return FilesTool(config={"max_chars": 5000, "max_entries": 50,
                             "blocked_extensions": [".exe"], "allowed_roots": []})


def test_known_folders_resolve_to_existing_dirs():
    """Все возвращённые папки должны существовать: это факты, не догадки."""
    folders = known_folders()
    assert "home" in folders
    for name, path in folders.items():
        assert path.is_dir(), f"{name} -> {path} не существует"


def test_normalize_expands_user_and_vars(tmp_path: Path):
    assert normalize('"' + str(tmp_path) + '"') == str(tmp_path)
    assert normalize("~") == str(Path(known_folders()["home"]))


def test_normalize_redirects_missing_desktop():
    """
    Ключевой случай: модель строит C:/Users/<имя>/Desktop. Если такой папки
    нет, а реальный рабочий стол известен, путь должен быть переписан.
    """
    folders = known_folders()
    desktop = folders.get("desktop")
    if desktop is None:
        pytest.skip("рабочий стол не определён на этой машине")
    guessed = str(Path(folders["home"]) / "Desktop" / "файл.txt")
    result = normalize(guessed)
    if not (Path(folders["home"]) / "Desktop").is_dir():
        assert result == str(desktop / "файл.txt")


def test_normalize_accepts_human_folder_name():
    """«Рабочий стол/файл.txt» без диска — валидный ввод от модели."""
    folders = known_folders()
    if "desktop" not in folders:
        pytest.skip("рабочий стол не определён")
    assert normalize("Рабочий стол/файл.txt") == str(folders["desktop"] / "файл.txt")


def test_known_folders_action_lists_real_paths(tool: FilesTool):
    result = tool.run(ToolCallRequest(tool="files", action="known_folders", args={}))
    assert result.ok
    assert result.data["folders"]["home"]


def test_find_file_returns_full_paths(tool: FilesTool, tmp_path: Path):
    target = tmp_path / "вложенная" / "искомый_отчёт.txt"
    target.parent.mkdir(parents=True)
    target.write_text("данные", encoding="utf-8")
    result = tool.run(ToolCallRequest(
        tool="files", action="find_file",
        args={"name": "искомый_отчёт", "search_root": str(tmp_path)}))
    assert result.ok
    assert [h["path"] for h in result.data["hits"]] == [str(target)]


def test_find_file_supports_glob(tool: FilesTool, tmp_path: Path):
    (tmp_path / "a.shp").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    result = tool.run(ToolCallRequest(
        tool="files", action="find_file",
        args={"name": "*.shp", "search_root": str(tmp_path)}))
    assert [h["name"] for h in result.data["hits"]] == ["a.shp"]


def test_find_file_deduplicates_nested_roots(tool: FilesTool, tmp_path: Path):
    """Один файл не должен выдаваться несколько раз из вложенных корней."""
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "уникальный.txt").write_text("x", encoding="utf-8")
    result = tool.run(ToolCallRequest(
        tool="files", action="find_file",
        args={"name": "уникальный", "search_root": str(tmp_path)}))
    paths = [h["path"] for h in result.data["hits"]]
    assert len(paths) == len(set(paths))


def test_missing_file_error_suggests_nested_path(tool: FilesTool, tmp_path: Path):
    """
    Ошибка обязана содержать готовый полный путь: без этого модель повторяет
    неверный путь или выдумывает содержимое.
    """
    (tmp_path / "папка").mkdir()
    real = tmp_path / "папка" / "лекция.txt"
    real.write_text("текст", encoding="utf-8")
    result = tool.run(ToolCallRequest(
        tool="files", action="read_text", args={"path": str(tmp_path / "лекция.txt")}))
    assert not result.ok
    assert str(real) in result.error


def test_missing_dir_error_lists_known_folders(tool: FilesTool):
    result = tool.run(ToolCallRequest(
        tool="files", action="read_text",
        args={"path": "C:/совершенно-несуществующая-папка-xyz/файл.txt"}))
    assert not result.ok
    assert "find_file" in result.error or "home" in result.error


def test_find_file_reports_searched_roots_when_empty(tool: FilesTool, tmp_path: Path):
    result = tool.run(ToolCallRequest(
        tool="files", action="find_file",
        args={"name": "нетакогофайла12345", "search_root": str(tmp_path)}))
    assert result.ok
    assert result.data["hits"] == []
    assert str(tmp_path) in result.summary
