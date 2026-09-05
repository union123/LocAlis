"""
Инструмент веб-поиска и извлечения текста страниц.

Два действия:
  - search_web     — метапоиск через ddgs (DuckDuckGo и другие бэкенды),
                      без ключей API и без облачных сервисов.
  - get_page_text  — скачать страницу по URL и вытащить из неё основной
                      читаемый текст. Понимает HTML (без меню/рекламы/подвала)
                      и PDF (текстовый слой через pypdf — та же локальная
                      библиотека, что уже используется в files_tool для
                      локальных .pdf). Справочные материалы вроде
                      международной хроностратиграфической шкалы часто лежат
                      только в PDF — раньше это действие такие ссылки просто
                      отклоняло с ошибкой "Страница не HTML".

Извлечение текста из HTML реализовано своим лёгким алгоритмом на lxml, а не через
trafilatura: у trafilatura огромное дерево транзитивных зависимостей
(babel — тысячи файлов локализации, dateparser, tzdata и т.д.), что сильно
раздувает окружение ради одной опциональной функции (даты публикации).
Алгоритм — вариант общеизвестной идеи "текстовая плотность минус плотность
ссылок": для каждого блочного элемента считаем текст без потомков-блоков,
штрафуем за долю текста внутри <a>, выбираем контейнер с максимальным
суммарным счётом. Это не даёт идеальный результат на любом сайте, но
уверенно отсекает навигацию/рекламу/подвал на типичных статьях и новостях.
"""

from __future__ import annotations

import re
import time
from io import BytesIO
from typing import Any

import httpx
from lxml import html as lxml_html

from core.base_tool import BaseTool, ToolAction
from core.schemas import ToolStatus

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException
    _DDGS_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001 — фиксируем причину для health()
    DDGS = None  # type: ignore[assignment,misc]
    DDGSException = Exception  # type: ignore[assignment,misc]
    _DDGS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form",
               "noscript", "iframe", "svg", "button", "select", "option")
_BLOCK_TAGS = ("p", "article", "section", "div", "main", "td", "li")


def _clean_tree(tree: Any) -> None:
    for tag in _STRIP_TAGS:
        for el in tree.iter(tag):
            el.drop_tree()


def _link_density(el: Any) -> float:
    text_len = len(el.text_content() or "")
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.text_content() or "") for a in el.iter("a"))
    return min(1.0, link_len / text_len)


def _extract_main_text(html_text: str, base_url: str = "") -> tuple[str, str]:
    """Вернуть (заголовок, основной_текст) из HTML. Своя лёгкая эвристика без trafilatura.

    Принимает уже декодированную СТРОКУ (не байты): lxml.html.fromstring умеет сам
    угадывать кодировку по байтам, но делает это по <meta charset> в самом документе,
    а если его нет — молча съезжает на latin-1 и превращает кириллицу в кашу.
    Декодирование поручаем httpx (response.text): он использует заголовок
    Content-Type и charset-детектор — надёжнее самодельного угадывания по байтам.
    """
    try:
        tree = lxml_html.fromstring(html_text)
    except Exception:
        return "", ""
    if base_url:
        try:
            tree.make_links_absolute(base_url)
        except Exception:
            pass

    title = ""
    title_el = tree.find(".//title")
    if title_el is not None and title_el.text:
        title = title_el.text.strip()
    if not title:
        og = tree.xpath("//meta[@property='og:title']/@content")
        if og:
            title = og[0].strip()

    _clean_tree(tree)

    best_el = None
    best_score = 0.0
    for tag in _BLOCK_TAGS:
        for el in tree.iter(tag):
            text = (el.text_content() or "").strip()
            if len(text) < 200:
                continue
            density = _link_density(el)
            score = len(text) * (1.0 - density)
            if score > best_score:
                best_score = score
                best_el = el

    if best_el is not None:
        text = best_el.text_content() or ""
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) >= 200:
            return title, text

    # Фолбэк: во всём документе не нашлось явно лучшего блока —
    # берём текст body целиком (лучше урезанный текст, чем пустой ответ).
    body = tree.find(".//body")
    text = (body.text_content() if body is not None else tree.text_content()) or ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return title, text


def _extract_pdf_text(content: bytes, limit: int) -> tuple[str, bool]:
    """Текст из PDF-байтов, полученных по HTTP. Требует pypdf (уже локальная
    зависимость — используется в tools/files/files_tool.py для тех же целей,
    новых пакетов/облачных сервисов не добавляем).

    Страницы помечаются номерами, как в files_tool._read_pdf — источники вроде
    международной хроностратиграфической шкалы часто состоят из одной большой
    таблицы на нескольких страницах, и явные разделители помогают модели не
    перепутать колонки при последующем разборе.

    Останавливаем разбор страниц, как только текста уже накоплено на limit
    символов сверх — PDF из веба (особенно многостраничные) не должны заставлять
    сервер вытаскивать текст из сотен страниц ради первых 8000 символов ответа.
    """
    from pypdf import PdfReader  # type: ignore  # ImportError пробрасываем наверх намеренно

    reader = PdfReader(BytesIO(content))
    parts: list[str] = []
    empty = 0
    total_len = 0
    truncated_pages = False
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — битая страница не должна ронять чтение
            text = ""
        if text:
            parts.append(f"=== Страница {number} ===\n{text}")
            total_len += len(text)
        else:
            empty += 1
        if total_len >= limit:
            truncated_pages = number < len(reader.pages)
            break
    if empty:
        parts.append(f"[страниц без извлекаемого текста: {empty} — возможно, это сканы]")
    return "\n\n".join(parts), truncated_pages


class WebSearchTool(BaseTool):
    """Поиск в интернете и извлечение текста страниц. Внешний HTTP, не требует контекста платформы."""

    name = "websearch"
    version = "1.0.0"
    description = "Найти страницы в интернете по запросу и прочитать основной текст конкретной страницы по ссылке"
    requires_context = False

    def health(self) -> tuple[ToolStatus, str]:
        if DDGS is None:
            return (ToolStatus.UNAVAILABLE,
                    f"Пакет 'ddgs' не установлен или не импортируется ({_DDGS_IMPORT_ERROR}). "
                    "Установите: pip install ddgs")
        try:
            httpx.Client  # noqa: B018 — просто проверяем импорт есть
        except Exception as exc:  # noqa: BLE001
            return ToolStatus.UNAVAILABLE, f"Пакет 'httpx' недоступен: {exc}"
        return ToolStatus.READY, "ddgs + httpx + lxml готовы"

    def actions(self) -> list[ToolAction]:
        max_r = int(self.config.get("max_max_results", 20))
        max_c = int(self.config.get("max_max_chars", 40000))
        return [
            ToolAction(
                "search_web",
                "Найти страницы в интернете по текстовому запросу (метапоиск: "
                "DuckDuckGo и другие бэкенды через ddgs, без ключей API). "
                "Возвращает список результатов: заголовок, ссылка, краткое описание. "
                "Используй для свежих фактов, документации, новостей — того, чего "
                "может не быть в знаниях модели.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Поисковый запрос"},
                        "max_results": {
                            "type": "integer",
                            "description": f"Сколько результатов вернуть (по умолчанию "
                                            f"{self.config.get('default_max_results', 6)}, "
                                            f"максимум {max_r})",
                        },
                        "region": {
                            "type": "string",
                            "description": "Регион поиска, например 'ru-ru' или 'us-en'. "
                                            "По умолчанию — без ограничения (worldwide).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolAction(
                "get_page_text",
                "Скачать страницу по URL и извлечь из неё основной читаемый текст. "
                "Работает и с обычными HTML-страницами (без меню, рекламы, навигации и "
                "подвала сайта), и с PDF-документами (извлекает текстовый слой). "
                "Используй, когда нужно прочитать конкретную страницу или документ, "
                "в т.ч. найденную через search_web — включая PDF-ссылки на справочники, "
                "стандарты и таблицы, которые часто встречаются в геологических и "
                "научных источниках.",
                {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Ссылка на страницу"},
                        "max_chars": {
                            "type": "integer",
                            "description": f"Максимум символов текста в ответе (по умолчанию "
                                            f"{self.config.get('default_max_chars', 8000)}, "
                                            f"максимум {max_c}). Текст обрезается, не страница.",
                        },
                    },
                    "required": ["url"],
                },
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == "search_web":
            return self._search_web(args)
        if action == "get_page_text":
            return self._get_page_text(args)
        return {"ok": False, "data": {}, "summary": "",
                "error": f"Неизвестное действие: {action}"}

    # ------------------------------------------------------------------

    def _search_web(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "data": {}, "summary": "", "error": "Пустой запрос 'query'"}
        if DDGS is None:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Пакет ddgs не готов: {_DDGS_IMPORT_ERROR}"}

        default_n = int(self.config.get("default_max_results", 6))
        max_n = int(self.config.get("max_max_results", 20))
        n = int(args.get("max_results") or default_n)
        n = max(1, min(n, max_n))
        region = args.get("region") or None

        try:
            with DDGS() as ddgs:
                kwargs: dict[str, Any] = {"max_results": n}
                if region:
                    kwargs["region"] = region
                raw_results = list(ddgs.text(query, **kwargs))
        except DDGSException as exc:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Ошибка поиска (ddgs): {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"{type(exc).__name__}: {exc}"}

        results = []
        for item in raw_results:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("href") or item.get("url", ""),
                "snippet": item.get("body", ""),
            })

        summary = f"Найдено {len(results)} результатов по запросу «{query}»"
        if results:
            top = "; ".join(r["title"] for r in results[:3] if r["title"])
            if top:
                summary += f": {top}"
        return {"ok": True, "data": {"query": query, "results": results},
                "summary": summary, "error": None}

    def _get_page_text(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        # SSRF-защита (критика ревью 25.08): модель может быть обманута
        # страницей из результатов поиска и запросить локальные сервисы.
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1",
                    "169.254.169.254") or host.endswith(".local"):
            return {"ok": False,
                    "error": (f"Запрос к внутреннему адресу '{host}' запрещён "
                              f"(защита от SSRF). Укажи публичный http(s)-URL."),
                    "data": {}, "summary": ""}
        if not url:
            return {"ok": False, "data": {}, "summary": "", "error": "Пустой аргумент 'url'"}
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url

        default_c = int(self.config.get("default_max_chars", 8000))
        max_c = int(self.config.get("max_max_chars", 40000))
        limit = int(args.get("max_chars") or default_c)
        limit = max(500, min(limit, max_c))

        timeout = float(self.config.get("fetch_timeout_sec", 15))
        ua = self.config.get("user_agent", "agent-platform-websearch/1.0")

        # Сетевые сбои (ConnectTimeout, SSL-handshake timeout и т.п.) на этой
        # машине наблюдались примерно в половине запросов подряд (задача
        # db30ca177f00, 20.08: 10 из 20 вызовов get_page_text упали с
        # httpx.RequestError при единственной попытке без повторов). Раньше
        # здесь не было retry вообще — одна неудачная попытка сразу отдавала
        # ошибку шагу, из-за чего модели не хватало исходников по части
        # минералов и она подменяла их данными "из памяти". Добавлены до
        # 2 повторов с небольшой паузой ТОЛЬКО для httpx.RequestError
        # (транспортные сбои) — HTTP-ошибки статуса (404/5xx) не повторяем,
        # там повтор обычно бессмысленен.
        max_attempts = int(self.config.get("fetch_retry_attempts", 3))
        retry_delay = float(self.config.get("fetch_retry_delay_sec", 1.0))
        resp = None
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(follow_redirects=True, timeout=timeout,
                                   headers={"User-Agent": ua}) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                return {"ok": False, "data": {"url": url}, "summary": "",
                        "error": f"HTTP {exc.response.status_code} при запросе {url}"}
            except httpx.RequestError as exc:
                if attempt < max_attempts:
                    time.sleep(retry_delay)
                    continue
                return {"ok": False, "data": {"url": url}, "summary": "",
                        "error": f"Ошибка сети после {max_attempts} попыток: "
                                 f"{type(exc).__name__}: {exc}"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "data": {"url": url}, "summary": "",
                        "error": f"{type(exc).__name__}: {exc}"}

        assert resp is not None  # достигнуто только при успешном break
        content_type = resp.headers.get("content-type", "")
        final_url = str(resp.url)
        is_pdf = ("application/pdf" in content_type
                  or "application/octet-stream" in content_type and final_url.lower().endswith(".pdf"))
        is_html = "text/html" in content_type or "application/xhtml" in content_type
        if not is_pdf and not is_html:
            return {"ok": False, "data": {"url": url, "content_type": content_type},
                    "summary": "",
                    "error": f"Страница не HTML и не PDF (Content-Type: {content_type})"}
        if is_pdf:
            pdf_bytes = resp.content
        else:
            html_text = resp.text

        if is_pdf:
            try:
                text, truncated = _extract_pdf_text(pdf_bytes, limit)
            except ImportError as exc:
                return {"ok": False, "data": {"url": final_url, "content_type": content_type},
                        "summary": "",
                        "error": f"Найден PDF, но пакет для чтения недоступен: {exc}"}
            except Exception as exc:  # noqa: BLE001 — битый/защищённый PDF не должен ронять шаг
                return {"ok": False, "data": {"url": final_url, "content_type": content_type},
                        "summary": "", "error": f"Не удалось прочитать PDF: {type(exc).__name__}: {exc}"}
            title = final_url.rsplit("/", 1)[-1] or final_url
            if len(text) > limit:
                text = text[:limit]
                truncated = True
        else:
            title, text = _extract_main_text(html_text, base_url=final_url)
            truncated = len(text) > limit
            if truncated:
                text = text[:limit]

        summary = f"Страница «{title or url}»: извлечено {len(text)} символов текста"
        if is_pdf:
            summary += " (из PDF)"
        if truncated:
            summary += " (обрезано лимитом max_chars)"
        return {
            "ok": True,
            "data": {"url": final_url, "title": title, "text": text, "truncated": truncated,
                     "source_type": "pdf" if is_pdf else "html"},
            "summary": summary,
            "error": None,
        }
