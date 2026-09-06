@echo off
REM ============================================================================
REM check_ornith.bat - health-check Ornith llama-server :8081 before a team
REM phase. If down: try to start it from the known GGUF path, wait up to 3 min.
REM Exit codes: 0 = healthy, 1 = still down after retries.
REM ============================================================================

REM --- 1. Health probe ---
curl -s --max-time 5 http://127.0.0.1:8081/health | findstr /i "ok" >nul 2>&1
if not errorlevel 1 goto :healthy

echo [WARN] Ornith :8081 is DOWN. Trying to start llama-server...

REM --- 2. Start if GGUF exists (goto-style, no parens in echo) ---
if exist "C:\path\to\models\ornith15\Ornith-1.5-35B-A3B-Q4_K_M.gguf" goto :start_server
if exist "C:\path\to\models\ornith15\Ornith-1.5-35B-A3B-UD-Q3_K_XL.gguf" goto :start_server_xl

echo [ERROR] Ornith GGUF not found in C:\path\to\models\ornith15\
exit /b 1

:start_server_xl
set GGUF=C:\path\to\models\ornith15\Ornith-1.5-35B-A3B-UD-Q3_K_XL.gguf
goto :do_start

:start_server
set GGUF=C:\path\to\models\ornith15\Ornith-1.5-35B-A3B-Q4_K_M.gguf

:do_start
set LLAMA_DIR=C:\path\to\Ollama\lib\ollama
if not exist "%LLAMA_DIR%\llama-server.exe" set LLAMA_DIR=C:\path\to\llama.cpp
start "ornith15-llama-server" /MIN "%LLAMA_DIR%\llama-server.exe" --model "%GGUF%" --alias ornith15 -ngl 99 --cpu-moe -c 32768 --flash-attn on --port 8081

REM --- 3. Wait for health (goto-style retry loop, 18 x 10s = 3 min) ---
set /a tries=0
:wait_loop
timeout /t 10 /nobreak >nul
curl -s --max-time 5 http://127.0.0.1:8081/health | findstr /i "ok" >nul 2>&1
if not errorlevel 1 goto :healthy
set /a tries+=1
if %tries% lss 18 goto :wait_loop

echo [ERROR] Ornith :8081 still DOWN after 3 min. Team phase would run 2-model - ABORTING.
exit /b 1

:healthy
echo [INFO] Ornith :8081 healthy.
exit /b 0
