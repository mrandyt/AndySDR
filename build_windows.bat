@echo off
REM ======================================================================
REM  Build AndySDR.exe on Windows 10/11
REM
REM  1. Install Miniforge (free):  https://github.com/conda-forge/miniforge
REM  2. Open "Miniforge Prompt", cd to this folder, run:  build_windows.bat
REM  3. Result: dist\AndySDR\AndySDR.exe   (copy the whole dist\AndySDR folder)
REM ======================================================================
setlocal
cd /d "%~dp0"

where conda >nul 2>nul
if errorlevel 1 (
  echo Please run this from the "Miniforge Prompt" ^(install Miniforge first^).
  pause
  exit /b 1
)

echo === Creating build environment "andysdr" ...
call conda create -y -n andysdr -c conda-forge python=3.11 numpy scipy soapysdr pip
if errorlevel 1 goto :fail
call conda activate andysdr

echo === Adding SDR driver modules (any that are not available are skipped) ...
for %%M in (soapysdr-module-rtlsdr soapysdr-module-airspy soapysdr-module-airspyhf soapysdr-module-hackrf soapysdr-module-remote soapysdr-module-lms7 soapysdr-module-plutosdr) do (
  call conda install -y -c conda-forge %%M || echo     skipped %%M
)

echo === Installing window + packager ...
python -m pip install --upgrade pywebview pyinstaller
if errorlevel 1 goto :fail

echo === Building AndySDR.exe ...
pyinstaller --noconfirm AndySDR.spec
if errorlevel 1 goto :fail

mkdir "dist\AndySDR\decoders" 2>nul
copy /y "decoders_README.txt" "dist\AndySDR\decoders\README.txt" >nul
copy /y "README.md" "dist\AndySDR\README.md" >nul
xcopy /y /i "samples" "dist\AndySDR\samples" >nul

echo.
echo === Done!  Run dist\AndySDR\AndySDR.exe
echo     SDRplay RSP users: install "SDRplay API 3.x" from sdrplay.com first.
echo     RTL-SDR / Nooelec users: install the WinUSB driver once with Zadig.
pause
exit /b 0

:fail
echo Build failed - see the messages above.
pause
exit /b 1
