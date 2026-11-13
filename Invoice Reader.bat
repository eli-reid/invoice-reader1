@echo off
setlocal
rem Starts Invoice Reader without a console window.
cd /d "%~dp0" || exit /b 1

if exist ".venv\Scripts\pythonw.exe" goto run

echo First run: setting up Python environment...
set "PYTHON_EXE="

for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%I"

if not defined PYTHON_EXE (
    for /f "delims=" %%I in ('where.exe python 2^>nul') do (
        if not defined PYTHON_EXE (
            "%%I" -c "import sys" >nul 2>&1
            if not errorlevel 1 set "PYTHON_EXE=%%I"
        )
    )
)

if not defined PYTHON_EXE (
    for %%K in ("HKCU\Software\Python\PythonCore" "HKLM\Software\Python\PythonCore" "HKLM\Software\WOW6432Node\Python\PythonCore") do (
        for /f "tokens=3,*" %%A in ('reg query %%K /s /v ExecutablePath 2^>nul ^| findstr /i "ExecutablePath"') do (
            if not defined PYTHON_EXE if exist "%%B" (
                "%%B" -c "import sys" >nul 2>&1
                if not errorlevel 1 set "PYTHON_EXE=%%B"
            )
        )
    )
)

if not defined PYTHON_EXE (
    echo Could not find an installed Python 3 interpreter.
    echo Install Python 3, then run this file again.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -m venv .venv
if errorlevel 1 (
    echo Failed to create the Python environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install the application requirements.
    pause
    exit /b 1
)

:run
start "" ".venv\Scripts\pythonw.exe" main.py %*
