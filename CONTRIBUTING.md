# Contributing to LocAlis

Thanks for your interest in the project! Any contribution is welcome —
bug reports, documentation fixes, new tools, code changes.

## Quick start

```bash
git clone https://github.com/union123/LocAlis.git localis
cd localis
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip

# minimal models (router + embedder):
ollama pull qwen2.5:3b-instruct
ollama pull bge-m3

# config for your hardware:
copy config\models.example.yaml config\models.yaml   # Linux: cp
# edit paths/model names

# sanity checks:
.venv/Scripts/python.exe main.py --status
.venv/Scripts/python.exe -m pytest tests/ -q
```

## Reporting a bug

Open an Issue using the **Bug report** template. Most helpful:
- output of `main.py --status`
- the task ID from the panel (8 characters, e.g. `e2a9998d`) —
  `main.py --show <id>` prints the full execution chain
- console/log output at the moment of the failure

## Proposing a feature

Open an Issue with the **Feature request** template: what user problem it
solves, and the minimal version that would satisfy you.

## Ground rules

- One PR = one logical topic.
- Tests: `python -m pytest tests/ -q` — green before submitting a PR.
- Style: PEP8 without fanaticism, type hints in public signatures.
- New tools go into `tools/<name>/` with a `manifest.yaml` — the registry
  picks them up automatically.
- Never commit `data/`, `config/models.yaml`, `secrets.json` (see `.gitignore`).

## Hardware note

The project was developed on an RTX 3070 8GB + 42GB RAM machine. On other
setups, edit `config/models.yaml` (see `models.example.yaml`). If your machine
is different, an installation-log issue would genuinely help the project.
