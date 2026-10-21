@echo off
setlocal
for /f "usebackq delims=" %%V in ("%~dp0version.txt") do set "WART_VERSION=%%V"
start "Warthog Wallet" "%~dp0wart-dapp-%WART_VERSION%-windows-x64.exe"
