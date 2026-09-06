@echo off
REM ============================================================================
REM gaia_ablation.bat - GAIA L1 x2: Lead, then Team-3R (same 42 tasks)
REM Double-click. Docker NOT needed. VPN recommended (websearch-heavy).
REM Total: ~11 h (Lead ~4 h + Team ~7 h)
REM ============================================================================

chcp 65001 >nul

cd /d C:\path\to\localis

echo.
echo ============================================================
echo  PHASE 1/2: GAIA Level-1 LEAD (42 tasks, ~4 h)
echo ============================================================
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe scripts\localis_gaia.py --level 1 --mode lead --timeout 1200
echo [INFO] Lead phase finished, exit code %errorlevel%

echo.
echo ============================================================
echo  PHASE 2/2: GAIA Level-1 TEAM-3R (42 tasks, ~7 h)
echo ============================================================
.venv\Scripts\python.exe scripts\localis_gaia.py --level 1 --mode team --timeout 2400
echo [INFO] Team phase finished, exit code %errorlevel%

echo.
echo ============================================================
echo  DONE. Compare:
echo    data\gaia_runs\*-lead\gaia_results.json
echo    data\gaia_runs\*-team\gaia_results.json
echo ============================================================
pause
