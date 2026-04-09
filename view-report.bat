@echo off
title Chappie - Report Viewer
cd /d "%~dp0\docs"

echo.
echo  Opening report at http://localhost:8000/test-report.html
echo  Press Ctrl+C to stop
echo.

start "" "http://localhost:8000/test-report.html"
py -3.12 -m http.server 8000
