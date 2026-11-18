@echo off
setlocal
title GMaps MaxRes - installer
cd /d "%~dp0"

set "PY_VER=3.13.5"
set "APP_NAME=GMaps MaxRes"
set "APPDIR=%LOCALAPPDATA%\GMapsMaxRes"
set "VENV=%APPDIR%\venv"
set "PYTXT=%TEMP%\gmaps_maxres_python_path.txt"

echo ============================================================
echo   %APP_NAME% - installer
echo   This takes a few minutes. Please keep this window open.
echo ============================================================
echo.

if not exist "%~dp0gmaps_maxres.py" goto :not_extracted
if not exist "%~dp0gmaps_maxres_gui.py" goto :not_extracted

rem ---------------------------------------------------------------
rem  Step 1: find a usable Python (3.8+ with tkinter), else install
rem ---------------------------------------------------------------
echo [1/4] Looking for Python...
set "PYEXE="
call :check_python py -3
if not defined PYEXE call :check_python python
if not defined PYEXE call :check_python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PYEXE goto :have_python

echo       Python not found - downloading Python %PY_VER% ...
set "PY_ARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "PY_ARCH=arm64"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-%PY_ARCH%.exe"
set "PY_SETUP=%TEMP%\python-%PY_VER%-setup.exe"
call :download "%PY_URL%" "%PY_SETUP%"
if errorlevel 1 goto :fail_download

echo       Installing Python (for this user only, no admin needed) ...
"%PY_SETUP%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 InstallLauncherAllUsers=0 Include_tcltk=1 Include_test=0 Include_doc=0 TargetDir="%LOCALAPPDATA%\Programs\Python\Python313"
if errorlevel 1 goto :fail_python
del "%PY_SETUP%" >nul 2>&1

call :check_python "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE goto :fail_python

:have_python
echo       Using: %PYEXE%
echo.

rem ---------------------------------------------------------------
rem  Step 2: private environment with Pillow + PyInstaller
rem ---------------------------------------------------------------
echo [2/4] Installing required libraries...
if not exist "%APPDIR%" mkdir "%APPDIR%"
if exist "%VENV%\Scripts\python.exe" goto :have_venv
"%PYEXE%" -m venv "%VENV%"
if errorlevel 1 goto :fail_libs
:have_venv
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade "pillow>=10.0" pyinstaller
if errorlevel 1 goto :fail_libs
echo.

rem ---------------------------------------------------------------
rem  Step 3: build the .exe
rem ---------------------------------------------------------------
echo [3/4] Building the program (this can take a minute)...
"%VENV%\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "%APP_NAME%" ^
    --paths "%~dp0." ^
    --distpath "%APPDIR%\dist" ^
    --workpath "%APPDIR%\build" ^
    --specpath "%APPDIR%\build" ^
    "%~dp0gmaps_maxres_gui.py"
if errorlevel 1 goto :fail_build
set "EXE=%APPDIR%\%APP_NAME%.exe"
copy /y "%APPDIR%\dist\%APP_NAME%.exe" "%EXE%" >nul
if errorlevel 1 goto :fail_copy
copy /y "%EXE%" "%~dp0%APP_NAME%.exe" >nul
echo.

rem ---------------------------------------------------------------
rem  Step 4: shortcuts on Desktop and in Start menu
rem ---------------------------------------------------------------
echo [4/4] Creating Desktop and Start menu shortcuts...
set "GMX_EXE=%EXE%"
set "GMX_DIR=%APPDIR%"
set "GMX_NAME=%APP_NAME%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "foreach ($f in @('Desktop','Programs')) {" ^
  "  $s = $w.CreateShortcut([Environment]::GetFolderPath($f) + '\' + $env:GMX_NAME + '.lnk');" ^
  "  $s.TargetPath = $env:GMX_EXE; $s.WorkingDirectory = $env:GMX_DIR; $s.Save() }"
if errorlevel 1 echo       (Could not create shortcuts - you can still run "%EXE%")
echo.

echo ============================================================
echo   Done!
echo   Start "%APP_NAME%" from the Desktop icon or the Start menu.
echo   A copy of the program is also in this folder.
echo ============================================================
echo.
start "" "%EXE%"
pause
exit /b 0


rem ===============================================================
rem  helpers
rem ===============================================================

:check_python
rem Usage: call :check_python <command...>   Sets PYEXE on success.
del "%PYTXT%" >nul 2>&1
%* -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
%* -c "import sys; open(sys.argv[1], 'w').write(sys.executable)" "%PYTXT%" >nul 2>&1
if not exist "%PYTXT%" exit /b 1
set /p PYEXE=<"%PYTXT%"
del "%PYTXT%" >nul 2>&1
exit /b 0

:download
rem Usage: call :download <url> <file>
curl.exe -L -f -s -S -o "%~2" "%~1" >nul 2>&1
if not errorlevel 1 exit /b 0
set "GMX_URL=%~1"
set "GMX_OUT=%~2"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
  "$ProgressPreference = 'SilentlyContinue';" ^
  "Invoke-WebRequest -UseBasicParsing -Uri $env:GMX_URL -OutFile $env:GMX_OUT"
exit /b %errorlevel%


rem ===============================================================
rem  errors
rem ===============================================================

:not_extracted
echo ERROR: The program files were not found next to this installer.
echo If you opened this from a ZIP file, first right-click the ZIP,
echo choose "Extract All...", then run install.bat from the extracted folder.
goto :end_fail

:fail_download
echo ERROR: Could not download Python. Check your internet connection and try again.
goto :end_fail

:fail_python
echo ERROR: Python could not be installed automatically.
echo Please install Python from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then run this installer again.
goto :end_fail

:fail_libs
echo ERROR: Could not install the required libraries.
echo Check your internet connection and try again.
goto :end_fail

:fail_build
echo ERROR: Building the program failed. See the messages above.
goto :end_fail

:fail_copy
echo ERROR: Could not copy the program. If "%APP_NAME%" is currently open,
echo close it and run this installer again.
goto :end_fail

:end_fail
echo.
pause
exit /b 1
