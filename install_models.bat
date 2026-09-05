@echo off
REM ============================================================================
REM install_models.bat - download all Ollama models needed by LocAlis
REM Small models (router+embedder) are REQUIRED; ensemble models optional.
REM Run once. Requires: Ollama installed.
REM ============================================================================

chcp 65001 >nul

where ollama >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ollama not found. Install from https://ollama.com first.
    pause
    exit /b 1
)

echo === REQUIRED models - router and knowledge base ===
ollama pull qwen2.5:3b-instruct
ollama pull bge-m3:latest

echo.
echo === OPTIONAL ensemble models - for orchestrated/team modes ===
echo Pulling glm-4.7-flash ...
ollama pull glm-4.7-flash
echo Pulling nemotron-3.5-lightning ...
ollama pull nemotron-3.5-lightning:30b-a3b-q4_K_M

echo.
echo === Done. Check: ollama list ===
ollama list
pause
