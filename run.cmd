@echo off
rem Windows launcher. Double-click it, or: run.cmd "<url>"   /   run.cmd replay captures\<dir>
rem First run downloads uv, Python, dependencies and Chromium into this folder (needs internet once).
setlocal
cd /d "%~dp0"
set "UV_INSTALL_DIR=%~dp0.uv"
set "UV_NO_MODIFY_PATH=1"
set "UV_PYTHON_INSTALL_DIR=%~dp0.uv\python"
set "UV_CACHE_DIR=%~dp0.uv\cache"
set "UV_PYTHON_PREFERENCE=only-managed"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.browsers"
set "PYTHONUTF8=1"
set "PATH=%UV_INSTALL_DIR%;%PATH%"

if not exist ".venv\" echo First run: downloading Python, packages and Chromium into this folder (about a minute)...
where uv >nul 2>nul || powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" || goto :fail
uv run --frozen --quiet playwright install chromium --no-shell || goto :fail

if not "%~1"=="" (
  uv run --frozen --quiet web-cloner %*
  exit /b
)
rem Double-clicked: ask for the URL and keep the window open at the end.
set /p "URL=Paste the website URL and press Enter: "
uv run --frozen --quiet web-cloner "%URL%"
pause
exit /b

:fail
echo.
echo Setup failed - see the messages above. Internet access is needed on the first run.
pause
exit /b 1
