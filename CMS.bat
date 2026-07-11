@echo off
REM Atlas launcher - runs the CLI via the repo's .venv, falling back to the
REM python on PATH (AVG blocks the unsigned CMS.exe, so we run from source).
REM Double-click for the app (UI), or: CMS.bat query "..."   CMS.bat align "..."  etc.
setlocal
set "CMS_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%CMS_PY%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Atlas setup incomplete: no .venv at "%~dp0.venv" and no python on PATH.
        echo Recreate it with:  py -3.11 -m venv .venv ^&^& .venv\Scripts\pip install -e .[dev,anthropic]
        exit /b 9009
    )
    set "CMS_PY=python"
)
"%CMS_PY%" -m cms.cli %*
set "RC=%ERRORLEVEL%"
REM Pause only on double-click (no args) so the window stays readable on errors;
REM never pause when invoked with arguments (automation must not hang).
if "%~1"=="" if not "%RC%"=="0" pause
exit /b %RC%
