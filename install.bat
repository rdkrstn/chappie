@echo off
title Chappie - Install
cd /d "%~dp0"

echo.
echo  ========================================
echo   CHAPPIE - Installing Dependencies
echo  ========================================
echo.

py -3.12 -m pip install -e ".[dev]"

echo.
echo  Done!
pause
