@echo off
title Chappie - Run Tests
cd /d "%~dp0"

echo.
echo  ========================================
echo   CHAPPIE - Running Tests
echo  ========================================
echo.

py -3.12 -m pytest tests/ -v

echo.
echo  ========================================
echo   Generating Visual Report...
echo  ========================================
echo.

py -3.12 tests/report.py

echo.
echo  Done! Opening report...
start "" "docs\test-report.html"

pause
