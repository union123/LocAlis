# LocAlis

> **A local-first multi-agent platform: three models debate the plan, execute it with tools, and verify each other — all on your laptop, no cloud.**

![local](https://img.shields.io/badge/100%25-local-no_cloud_data_sent-success) ![multi-model](https://img.shields.io/badge/multi--model-3%2B%20proposers-blue) ![tools](https://img.shields.io/badge/tools-11%2B%20built--in-orange) ![license](https://img.shields.io/badge/license-MIT-blue)

[README in Russian](README.ru.md)

```
Task → Router → Secretary (context)
            ↓
   ┌── proposer_a ──┐
   ├── proposer_b ──┤  → Arbiter → consensus plan
   └── proposer_c ──┘
            ↓
     Lead Executor (tool-calling: files, QGIS, browser, container…)
            ↓
   Cross-review (against disk facts) → Adversarial review
            ↓
   [auto-fix] → Result + full journal in SQLite
```

**LocAlis** turns several local LLMs (Ollama + llama.cpp) into a working team:
models propose plans independently, an arbiter merges them into a consensus,
an executor works through 11+ built-in tools (files, CSV/Excel, QGIS, browser,
container shell), and the result passes cross-review plus an adversarial
check before it reaches you. Everything runs on consumer hardware: an ensemble
of 30B models fits in 8 GB VRAM + 32 GB RAM thanks to the VRAM arbiter
(heavy models offload their experts to RAM).

Competitive with cloud agents on public benchmarks (Terminal-Bench, GAIA —
see the table below), while being free and private.

## Features

- **Routing**: simple tasks are solved by one model immediately, complex ones go to the ensemble
- **Team mode**: N rounds of plan debate → arbiter consensus → execution → cross-review → auto-fix
- **Adversarial review**: a separate model tries to refute the result before it is released
- **11+ tools**: files, CSV/Excel (column-order preserving), QGIS, charts, headless browser (Playwright), desktop automation, hybrid knowledge base (FTS5 + embeddings), container shell
- **VRAM arbiter**: 30B+ models get an exclusive GPU slot while others are unloaded — the ensemble fits in 8 GB VRAM
- **Live telemetry**: tokens/sec and latency of every model call, stored in SQLite

## Benchmarks

| Benchmark | Lead | Team-3R |
|---|---|---|
| Terminal-Bench core (20-task subset) | 2/20 (10%) | **4/20 (20%)** |
| GAIA validation L1 (42 tasks, text-only) | 13/42 (31%) | 2-model run, see footnote |

Both rows come from a single overnight ablation (Sep 6) on one adapter version
(v6.3) under equal conditions. Team wins were manually verified:
`heterogeneous-dates` solved with the byte-exact ground truth 11.428571
(the meeting caught an off-by-one), `fix-permissions` passed 1/1 in-container
exec tests. Niche split: Team excels at local file/shell work; Lead wins on
web research (faster, fewer timeout exposures). **Footnote**: the Ornith
llama-server was down during that run, so Team ran on two models out of
three — a full 3-model ablation is still pending. GAIA Lead ran on the old
answer-extraction (with the new strict marker we project 50%+); the GAIA Team
run is not directly comparable. LocAlis-Bench v1 (12 tasks) is the internal
regression set.

Scripts: `scripts/localis_gaia.py`, `scripts/localis_bench.py`,
`scripts/tb20_subset.sh`. Terminal-Bench adapter:
`scripts/localis_tb_agent.py` (requires
[terminal-bench](https://github.com/laude-institute/terminal-bench) and Docker).

## Installation

Requirements: Windows (tested) or Linux, Python 3.10+, [Ollama](https://ollama.com);
Docker for the container tool.

### Models

**Required** (small, needed to start any task):

```bash
ollama pull qwen2.5:3b-instruct   # router + secretary (~2 GB)
ollama pull bge-m3:latest          # knowledge-base embeddings (~1.2 GB)
```

**Optional** (the ensemble for orchestrated/team modes — pick for your
hardware, full analogs in `config/models.example.yaml`):

```bash
ollama pull glm-4.7-flash          # proposer/executor
ollama pull nemotron-3.5-lightning:30b-a3b-q4_K_M
```

**35B+ models run through llama.cpp (`llama-server`), not Ollama.**
Example - the default `proposer_b` (Ornith-1.5-35B-A3B MoE, ~20 GB GGUF Q4_K_M):

```bash
llama-server --model C:/path/to/Ornith-1.5-35B-Q4_K_M.gguf ^
  --alias ornith15 -ngl 99 --cpu-moe -c 32768 --flash-attn on --port 8081
```

- `--alias` must match the `model:` field in `config/models.yaml`; the model connects via `provider: llamacpp` with `base_url: http://127.0.0.1:8081`
- `--cpu-moe` keeps the MoE experts in RAM (~11 t/s on a Ryzen + RTX 3070 setup); drop it if your GPU fits the whole model
- thinking models need `no_think: true` in the model's `extra:` section - otherwise they burn the whole token budget on reasoning and return an empty answer
- one llama-server per port; do not run two models on the same port at once
- start the server before orchestrated/team tasks; the welcome screen checks its health along with the Ollama models

Minimal setup: just the two required models — the platform works in
simple mode and with the knowledge base. The VRAM arbiter decides who
enters the GPU and when.

Windows users: `install_models.bat` in the repo root downloads everything
in one double-click.

### Setup

**Repo layout** (so you know where things live):

```
LocAlis/
├── main.py                  # entry point (panel + CLI)
├── requirements.txt         # python dependencies (in the ROOT)
├── install_models.bat       # downloads all required models (ROOT)
├── config/
│   ├── models.example.yaml  # copy to models.yaml and edit
│   └── settings.yaml
├── agents/  core/  tools/  workflows/   # platform code
├── scripts/                 # benchmark utilities only
├── ui/                      # control panel
└── tests/
```

**Option A — ZIP (no git needed):**

1. On the repo page click **Code → Download ZIP**, unpack anywhere
2. Open a terminal **inside the unpacked folder** (the one containing
   `main.py` and `requirements.txt`)
3. Continue from Step 0 below — everything else is the same

*(To get updates later you will need to re-download the ZIP; with git,
updates are one `git pull` — that is the only difference.)*

**Option B — git clone:**

**Step 0. Python 3.10–3.12 is required** (3.13+ will fail — faiss-cpu and
parts of langchain have no wheels yet). Check: `python --version`.

```bash
git clone https://github.com/union123/LocAlis.git localis
cd localis
python -m venv .venv

# Windows (cmd):
.venv\Scripts\activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# configure:
copy config\models.example.yaml config\models.yaml   # Linux/macOS: cp ...
# edit paths/model names for your hardware

# launch the panel (from the activated venv):
python main.py --ui
# → http://127.0.0.1:8080
```

Troubleshooting: if `pip install` fails on Python 3.13+ — install
[Python 3.11](https://www.python.org/downloads/) and recreate the venv
with `py -3.11 -m venv .venv`. If activation is blocked in PowerShell, run:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and try again.

Cloud provider keys are optional — the platform works fully without them:
`python main.py --set-key OPENROUTER_API_KEY=sk-or-v1-...` or an environment
variable. Keys are stored in `data/secrets.json` (never committed).

## Usage

- **Panel** (`main.py --ui`) — task input, live tool journal, tokens/sec for every model call, meeting and review status. Interface is in Russian; an English switch is planned.
- **CLI**: `python main.py --mode orchestrated --task "your task"`
- **Team mode**: a checkbox in the panel or `--meta '{"meeting_rounds":3}'`
- **Benchmarks**: `python scripts/localis_bench.py --mode lead` / `python scripts/localis_gaia.py --level 1 --mode team`

## Architecture (brief)

```
Router → Secretary → [Team meeting] → Lead executor (tools)
     → Cross-review (disk facts) → Adversarial review → [auto-fix]
```

Key components: `core/llm_gateway.py` (unified provider calls + VRAM arbiter +
telemetry), `core/blackboard.py` (SQLite decision journal), `tools/`
(manifest-based plugins), `workflows/strategies.py` (execution modes).

## Status

Research preview. Developed on the author's machine (RTX 3070 8GB VRAM,
40 GB RAM, Windows); installing on other configurations requires editing
`config/models.yaml` for your hardware. Issues welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).
