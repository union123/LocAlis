# Contributing to LocAlis

Спасибо за интерес к проекту! Любой вклад приветствуется — баг-репорты,
уточнения документации, новые инструменты, фиксы.

## Быстрый старт

```bash
git clone https://github.com/union123/LocAlis.git localis
cd localis
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip

# минимальные модели (роутер + эмбеддер):
ollama pull qwen2.5:3b-instruct
ollama pull bge-m3

# конфиг под своё железо:
copy config\models.example.yaml config\models.yaml   # Linux: cp
# отредактируйте пути/имена моделей

# проверить, что всё живо:
.venv/Scripts/python.exe main.py --status
.venv/Scripts/python.exe -m pytest tests/ -q
```

## Как сообщить о баге

Откройте Issue с шаблоном **Bug report**. Очень помогают:
- вывод `main.py --status`
- ID задачи из панели (8-символьный, например `e2a9998d`) — по нему в
  `main.py --show <id>` видна вся цепочка
- что в консоли/логах в момент сбоя

## Как предложить фичу

Issue с шаблоном **Feature request**: какая задача пользователя решается,
какой минимальный вариант устроил бы.

## Правила

- Один PR — одна логическая тема.
- Тесты: `python -m pytest tests/ -q` — зелёные перед PR.
- Стиль: PEP8 без фанатизма, типы в сигнатурах публичных функций.
- Новые инструменты кладите в `tools/<имя>/` с `manifest.yaml` —
  registry подхватит их автоматически.
- Не коммитьте `data/`, `config/models.yaml`, `secrets.json` (см. `.gitignore`).

## Замечание по железу

Проект разрабатывался на RTX 3070 8GB + 42GB RAM. На других конфигурациях
правьте `config/models.yaml` (см. `models.example.yaml`). Если ваша машина
отличается — issue с логом установки очень поможет проекту.
