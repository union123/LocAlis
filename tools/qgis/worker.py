"""
Worker для QGIS-плагина.

ВАЖНО: этот файл исполняется НЕ основным Python платформы, а интерпретатором,
встроенным в QGIS (там доступен модуль qgis.core). Обмен с ядром — через
JSON на stdin/stdout, поэтому импортировать код платформы здесь НЕЛЬЗЯ.

Протокол:
  stdin  : {"action": "...", "args": {...}}
  stdout : {"ok": bool, "data": {...}, "summary": "...", "error": null|str}
Вся диагностика пишется в stderr, чтобы не портить JSON.
"""

import json
import os
import sys
import traceback

# Строка-маркер: ядро вырезает по ней JSON из потока, где QGIS печатает свой шум.
MARKER = "__AGENT_PLATFORM_RESULT__"


def _init_qgis():
    """Инициализация headless-QGIS. Возвращает (QgsApplication, модуль core)."""
    from qgis.core import QgsApplication

    prefix = os.environ.get("QGIS_PREFIX_PATH")
    QgsApplication.setPrefixPath(prefix or "", True)
    app = QgsApplication([], False)
    app.initQgis()
    return app


def _layer_or_fail(path, layer_name=None):
    from qgis.core import QgsVectorLayer

    if not os.path.exists(path):
        raise FileNotFoundError("Файл слоя не найден: %s" % path)
    uri = path if not layer_name else "%s|layername=%s" % (path, layer_name)
    layer = QgsVectorLayer(uri, layer_name or os.path.basename(path), "ogr")
    if not layer.isValid():
        raise ValueError("QGIS не смог открыть слой: %s" % uri)
    return layer


# --------------------------------------------------------------------------
# Действия
# --------------------------------------------------------------------------


def act_layer_info(args):
    layer = _layer_or_fail(args["path"], args.get("layer_name"))
    crs = layer.crs()
    ext = layer.extent()
    fields = [{"name": f.name(), "type": f.typeName()} for f in layer.fields()]
    return {
        "ok": True,
        "data": {
            "name": layer.name(),
            "feature_count": layer.featureCount(),
            "geometry_type": layer.geometryType().name if hasattr(layer.geometryType(), "name") else str(layer.geometryType()),
            "crs": crs.authid(),
            "crs_description": crs.description(),
            "extent": [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()],
            "fields": fields,
        },
        "summary": "Слой '%s': %d объектов, CRS %s, полей %d"
        % (layer.name(), layer.featureCount(), crs.authid(), len(fields)),
    }


def act_read_attributes(args):
    layer = _layer_or_fail(args["path"], args.get("layer_name"))
    limit = int(args.get("limit", 50))
    columns = args.get("columns")
    expression = args.get("filter")

    from qgis.core import QgsFeatureRequest

    request = QgsFeatureRequest()
    if expression:
        request.setFilterExpression(expression)
    rows = []
    for i, feat in enumerate(layer.getFeatures(request)):
        if i >= limit:
            break
        rec = {}
        for field in layer.fields():
            fname = field.name()
            if columns and fname not in columns:
                continue
            value = feat[fname]
            # QVariant NULL -> None; всё нестандартное -> str
            if value is None or (hasattr(value, "isNull") and value.isNull()):
                rec[fname] = None
            elif isinstance(value, (int, float, bool, str)):
                rec[fname] = value
            else:
                rec[fname] = str(value)
        rows.append(rec)
    return {
        "ok": True,
        "data": {"rows": rows, "returned": len(rows), "total": layer.featureCount()},
        "summary": "Прочитано %d строк атрибутов из %d (слой '%s')"
        % (len(rows), layer.featureCount(), layer.name()),
    }


def act_export_geojson(args):
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsProject,
        QgsVectorFileWriter,
    )

    layer = _layer_or_fail(args["path"], args.get("layer_name"))
    out_path = args["output_path"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GeoJSON"
    options.fileEncoding = "UTF-8"
    transform_context = QgsProject.instance().transformContext()
    target_crs = args.get("target_crs")
    if target_crs:
        crs = QgsCoordinateReferenceSystem(target_crs)
        if not crs.isValid():
            raise ValueError("Некорректная целевая CRS: %s" % target_crs)
        # ct обязателен: присваивать None нельзя (ошибка типа в PyQGIS),
        # поэтому строим полноценное преобразование источник -> цель.
        options.ct = QgsCoordinateTransform(layer.crs(), crs, transform_context)

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, out_path, transform_context, options
    )
    code = result[0]
    if code != QgsVectorFileWriter.NoError:
        raise RuntimeError("Ошибка экспорта (код %s): %s" % (code, result[1]))
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    return {
        "ok": True,
        "data": {"output_path": out_path, "bytes": size, "features": layer.featureCount()},
        "summary": "Экспортировано %d объектов в GeoJSON: %s (%d байт)"
        % (layer.featureCount(), out_path, size),
    }


def act_update_attributes(args):
    """Запись значений атрибутов. Требует writable-слой (shp/gpkg)."""
    layer = _layer_or_fail(args["path"], args.get("layer_name"))
    updates = args["updates"]  # [{"fid": 1, "values": {"field": value}}]
    field_index = {f.name(): i for i, f in enumerate(layer.fields())}

    unknown = sorted({k for u in updates for k in u.get("values", {})} - set(field_index))
    if unknown:
        raise ValueError("Нет таких полей в слое: %s. Доступны: %s"
                         % (unknown, sorted(field_index)))

    if not layer.startEditing():
        raise RuntimeError("Слой не поддерживает редактирование: %s" % args["path"])
    changed = 0
    for upd in updates:
        fid = int(upd["fid"])
        for fname, value in upd.get("values", {}).items():
            if not layer.changeAttributeValue(fid, field_index[fname], value):
                layer.rollBack()
                raise RuntimeError("Не удалось изменить %s у объекта fid=%s" % (fname, fid))
            changed += 1
    if not layer.commitChanges():
        errors = "; ".join(layer.commitErrors())
        layer.rollBack()
        raise RuntimeError("Ошибка сохранения изменений: %s" % errors)
    return {
        "ok": True,
        "data": {"changed_values": changed, "features": len(updates)},
        "summary": "Обновлено %d значений в %d объектах" % (changed, len(updates)),
    }


def act_list_layers(args):
    """Список слоёв в контейнере (GeoPackage) или в папке."""
    from qgis.core import QgsProviderRegistry

    path = args["path"]
    if not os.path.exists(path):
        raise FileNotFoundError("Путь не найден: %s" % path)
    layers = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml")):
                layers.append({"name": name, "path": os.path.join(path, name)})
    else:
        meta = QgsProviderRegistry.instance().providerMetadata("ogr")
        for sub in meta.querySublayers(path):
            layers.append({
                "name": sub.name(),
                "path": path,
                "feature_count": sub.featureCount(),
                "geometry_type": str(sub.wkbType()),
            })
    return {
        "ok": True,
        "data": {"layers": layers, "count": len(layers)},
        "summary": "Найдено слоёв: %d" % len(layers),
    }


# --------------------------------------------------------------------------
# Универсальный доступ к алгоритмам обработки QGIS
# --------------------------------------------------------------------------

# БЕЛЫЙ СПИСОК, а не полный доступ. Причина: processing.run() умеет удалять
# файлы, запускать GDAL-команды и писать куда угодно. Модель, ошибившись
# в имени алгоритма или параметрах, может испортить данные пользователя.
# Здесь только чтение/создание новых слоёв — ни одного разрушающего действия.
ALLOWED_ALGORITHMS = {
    # геометрия
    "native:buffer": "Буферные зоны вокруг объектов",
    "native:centroids": "Центроиды объектов",
    "native:clip": "Обрезать слой по границе другого слоя",
    "native:intersection": "Пересечение двух слоёв",
    "native:union": "Объединение двух слоёв",
    "native:difference": "Разность слоёв",
    "native:dissolve": "Слить объекты по значению поля",
    "native:convexhull": "Выпуклая оболочка",
    "native:simplifygeometries": "Упростить геометрию",
    "native:reprojectlayer": "Перепроецировать слой в другую CRS",
    "native:fixgeometries": "Исправить некорректную геометрию",
    # выборка и соединения
    "native:extractbyattribute": "Выбрать объекты по значению атрибута",
    "native:extractbylocation": "Выбрать объекты по расположению",
    "native:joinattributestable": "Присоединить таблицу по общему полю",
    "native:joinbylocation": "Присоединить атрибуты по расположению",
    # расчёты и статистика
    "native:fieldcalculator": "Вычислить новое поле выражением",
    "qgis:statisticsbycategories": "Статистика по категориям",
    "native:zonalstatisticsfb": "Зональная статистика (растр по полигонам)",
    "qgis:basicstatisticsforfields": "Базовая статистика по полю",
    "native:countpointsinpolygon": "Подсчёт точек в полигонах",
    # прочее
    "native:addautoincrementalfield": "Добавить поле-счётчик",
    "native:explodelines": "Разбить линии на отрезки",
    "native:pointsalonglines": "Точки вдоль линий",
}


def _ensure_processing_path():
    """Добавить папку плагинов QGIS в sys.path — там лежит модуль processing."""
    import glob

    if any(os.path.basename(p) == "plugins" for p in sys.path):
        return
    roots = []
    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        roots.append(os.path.join(prefix, "python", "plugins"))
    # Резерв: вычисляем от самого интерпретатора QGIS
    exe_root = os.path.dirname(os.path.dirname(sys.executable))
    roots += glob.glob(os.path.join(exe_root, "apps", "qgis*", "python", "plugins"))
    roots += glob.glob(os.path.join(exe_root, "..", "apps", "qgis*", "python", "plugins"))
    for candidate in roots:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)
            return
    raise RuntimeError(
        "Не найдена папка плагинов QGIS с модулем processing. "
        "Проверьте переменную QGIS_PREFIX_PATH.")


def act_list_algorithms(args):
    """Список разрешённых алгоритмов. Модель не должна их угадывать."""
    query = str(args.get("query") or "").strip().lower()
    items = []
    for name, description in sorted(ALLOWED_ALGORITHMS.items()):
        if query and query not in name.lower() and query not in description.lower():
            continue
        items.append({"id": name, "description": description})
    listing = "; ".join("%s — %s" % (i["id"], i["description"]) for i in items)
    return {
        "ok": True,
        "data": {"algorithms": items, "total": len(items)},
        "summary": ("Разрешённых алгоритмов: %d. %s" % (len(items), listing))
        if items else "По запросу «%s» алгоритмов не найдено" % query,
    }


def act_run_processing(args):
    """
    Запустить алгоритм обработки QGIS из белого списка.

    Параметры передаются как есть: у каждого алгоритма они свои, и подменять
    их «умной» логикой опаснее, чем вернуть модели понятную ошибку QGIS.
    """
    # Модуль processing лежит НЕ в python/, а в apps/qgis*/python/plugins.
    # В headless-режиме этот путь не подхватывается сам (проверено:
    # ModuleNotFoundError: No module named 'processing').
    _ensure_processing_path()
    import processing
    from processing.core.Processing import Processing
    from qgis.analysis import QgsNativeAlgorithms
    from qgis.core import QgsApplication

    algorithm = str(args.get("algorithm") or "").strip()
    if algorithm not in ALLOWED_ALGORITHMS:
        return {
            "ok": False, "data": {}, "summary": "",
            "error": ("Алгоритм '%s' не разрешён. Вызовите list_algorithms, "
                      "чтобы увидеть доступные (%d шт.)."
                      % (algorithm, len(ALLOWED_ALGORITHMS))),
        }
    params = args.get("parameters")
    if not isinstance(params, dict) or not params:
        return {"ok": False, "data": {}, "summary": "",
                "error": "Не переданы parameters — словарь параметров алгоритма"}

    # Провайдер native-алгоритмов подключается вручную: в headless-режиме
    # он не регистрируется сам (проверено — иначе 'algorithm not found')
    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
    Processing.initialize()

    output = params.get("OUTPUT")
    result = processing.run(algorithm, params)

    produced = result.get("OUTPUT") if isinstance(result, dict) else None
    info = {}
    if isinstance(produced, str) and os.path.exists(produced):
        info["output_path"] = produced
        info["size_bytes"] = os.path.getsize(produced)
        try:
            layer = _layer_or_fail(produced)
            info["features"] = layer.featureCount()
            info["crs"] = layer.crs().authid()
        except Exception:
            pass
    elif output:
        info["output_path"] = str(output)

    plain = {k: (v if isinstance(v, (int, float, str, bool, type(None))) else str(v))
             for k, v in (result or {}).items()}
    summary = "Алгоритм %s выполнен." % algorithm
    if info.get("output_path"):
        summary += " Результат: %s" % info["output_path"]
    if "features" in info:
        summary += " (объектов: %d, CRS: %s)" % (info["features"], info.get("crs", "?"))
    return {"ok": True, "data": {"result": plain, **info}, "summary": summary}


ACTIONS = {
    "layer_info": act_layer_info,
    "read_attributes": act_read_attributes,
    "export_geojson": act_export_geojson,
    "update_attributes": act_update_attributes,
    "list_layers": act_list_layers,
    "run_processing": act_run_processing,
    "list_algorithms": act_list_algorithms,
}


def main():
    payload = sys.stdin.read()
    app = None
    try:
        request = json.loads(payload)
        action = request.get("action")
        args = request.get("args") or {}
        if action not in ACTIONS:
            result = {"ok": False, "error": "Неизвестное действие: %s" % action,
                      "data": {}, "summary": ""}
        else:
            app = _init_qgis()
            result = ACTIONS[action](args)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        result = {"ok": False, "data": {}, "summary": "",
                  "error": "%s: %s" % (type(exc).__name__, exc)}
    finally:
        if app is not None:
            try:
                app.exitQgis()
            except Exception:
                pass

    sys.stdout.write("\n" + MARKER + json.dumps(result, ensure_ascii=False) + MARKER + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
