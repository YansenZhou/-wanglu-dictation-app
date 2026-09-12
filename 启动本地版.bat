@echo off
cd /d "%~dp0"
py -3 --version >nul 2>nul
if not errorlevel 1 goto use_py
python --version >nul 2>nul
if not errorlevel 1 goto use_python
echo Python 3 was not found.
echo Install Python 3 or use the GitHub Pages version.
pause
goto end

:use_py
py -3 local_server.py
if errorlevel 1 goto failed
goto end

:use_python
python local_server.py
if errorlevel 1 goto failed
goto end

:failed
echo.
echo The local server failed to start. The error is shown above.
pause

:end
