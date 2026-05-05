@echo off
chcp 65001 >nul
echo.
echo === LANA - Fix git and push to GitHub ===
echo.

cd /d "%~dp0"

echo [1/4] Removing broken .git folder...
powershell -Command "if (Test-Path '.git') { Remove-Item -Path '.git' -Recurse -Force }"
echo Done.

echo.
echo [2/4] Git init...
git init
git branch -M main

echo.
echo [3/4] Commit all files...
git add .
git commit -m "Initial commit: LANA Flask Web App"

echo.
echo [4/4] Push to GitHub...
git remote add origin https://github.com/nicepolo/lana-monitor.git
git push -u origin main

echo.
echo === Done! ===
echo If push asks for password, use a GitHub Personal Access Token.
pause
