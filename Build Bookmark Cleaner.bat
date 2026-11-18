@echo off
title Building Bookmark Cleaner
cd /d "%~dp0"
echo.
echo  Building Bookmark Cleaner.exe - this takes a minute or two...
echo.

set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul || goto nopython

%PY% -m pip install --upgrade --quiet pyinstaller || goto failed
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Bookmark Cleaner" ^
    --icon "bookmark_cleaner.ico" ^
    --add-data "bookmark_cleaner.ico;." ^
    "bookmark_cleaner_gui.pyw" || goto failed

copy /y "dist\Bookmark Cleaner.exe" "Bookmark Cleaner.exe" >nul
rmdir /s /q build >nul 2>nul
rmdir /s /q dist >nul 2>nul
del /q "Bookmark Cleaner.spec" >nul 2>nul

echo.
echo  Done!  "Bookmark Cleaner.exe" is in this folder.
echo  Double-click it to run, and share that one file with friends.
echo.
explorer /select,"%~dp0Bookmark Cleaner.exe"
pause
exit /b 0

:nopython
echo  Python isn't installed (it's only needed once, to build the program).
echo.
echo  1. Get it from https://www.python.org/downloads/
echo  2. In the installer, tick "Add python.exe to PATH", then click Install Now.
echo  3. Double-click this file again.
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1

:failed
echo.
echo  Something went wrong while building. Scroll up to see the error.
pause
exit /b 1
