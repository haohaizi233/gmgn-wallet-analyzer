@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT=%~dp0.."
pushd "%ROOT%"

set "PY=python"
if exist "D:\APP\python 3.13.2\python.exe" set "PY=D:\APP\python 3.13.2\python.exe"

echo Using: %PY%
"%PY%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

"%PY%" -m PyInstaller --noconfirm --clean gmgn_wallet.spec
if errorlevel 1 goto :fail

"%PY%" scripts\copy_portable_files.py
if errorlevel 1 goto :fail

echo.
echo Build OK: %ROOT%\dist\GMGNWallet\GMGNWallet.exe
popd
exit /b 0

:fail
echo Build failed.
popd
exit /b 1
