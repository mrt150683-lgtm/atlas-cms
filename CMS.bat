@echo off
REM Atlas launcher - probes each runtime before trusting it. A copied or moved
REM venv can leave python.exe present but unusable; existence is not health.
REM Double-click for the app (UI), or: CMS.bat query "..."   CMS.bat align "..."  etc.
setlocal
set "CMS_PY="
set "CMS_PY_ARGS="
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_BROKEN="
if defined CMS_PYTHON call :probe "%CMS_PYTHON%" ""
if not defined CMS_PY if exist "%VENV_PY%" call :probe "%VENV_PY%" ""
if not defined CMS_PY if exist "%VENV_PY%" set "VENV_BROKEN=1"
if not defined CMS_PY where py >nul 2>nul && call :probe "py" "-3.11"
if not defined CMS_PY where python >nul 2>nul && call :probe "python" ""
if not defined CMS_PY (
    echo Atlas runtime health check failed.
    if defined VENV_BROKEN echo The existing .venv Python is present but cannot import Atlas.
    echo Repair with:  py -3.11 -m venv .venv
    echo Then run:     .venv\Scripts\pip install -e .[dev,anthropic]
    echo Or set CMS_PYTHON to the full path of a healthy Python executable.
    exit /b 9009
)
"%CMS_PY%" %CMS_PY_ARGS% -m cms.cli %*
set "RC=%ERRORLEVEL%"
REM Pause only on double-click (no args) so the window stays readable on errors;
REM never pause when invoked with arguments (automation must not hang).
if "%~1"=="" if not "%RC%"=="0" pause
exit /b %RC%

:probe
"%~1" %~2 -c "import cms.cli" >nul 2>nul
if not errorlevel 1 (
    set "CMS_PY=%~1"
    set "CMS_PY_ARGS=%~2"
)
exit /b 0
