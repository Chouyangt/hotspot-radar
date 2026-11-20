@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   热点雷达 · 抓取深圳与世界热点
echo ================================
python radar.py sync
echo.
echo 正在打开热榜...
start "" "%~dp0out\index.html"
