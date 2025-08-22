@echo off
setlocal

REM Create output/build folders (ignore errors if they already exist)
mkdir C:\_pyi\out 2>nul
mkdir C:\_pyi\build 2>nul

REM Run from the folder this .bat lives in so llm_toast_ui.py is found
pushd "%~dp0"

pyinstaller llm_toast_ui.py --name ClipLLMTray --onefile --windowed --noupx --clean --noconfirm ^
  --distpath C:\_pyi\out --workpath C:\_pyi\build ^
  --hidden-import win32timezone --collect-submodules keyring.backends --collect-data keyring ^
  --log-level DEBUG

set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
