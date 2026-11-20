@echo off
chcp 65001 >nul
cd /d "%~dp0"
git pull --rebase origin main
if errorlevel 1 ( echo   x 拉取失败 & pause & exit /b 1 )
git add -A
git commit -m "update: %date% %time%"
if errorlevel 1 ( echo   （没有新改动） )
git push origin main
if errorlevel 1 ( echo   x 推送失败 & pause & exit /b 1 )
echo.
echo 已推送，约 1 分钟后自动跑一轮并部署
pause
