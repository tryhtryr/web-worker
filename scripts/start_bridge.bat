@echo off
chcp 65001 >nul
title web-deepseek bridge

cd /d "%~dp0..\bridge"

echo ============================================
echo   web-deepseek-bridge
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 python，请先安装 Python 3.9+ 并加入 PATH
  pause
  exit /b 1
)

python -c "import requests, websocket" >nul 2>&1
if errorlevel 1 (
  echo [提示] 正在安装依赖...
  python -m pip install -r "%~dp0..\requirements.txt"
  echo.
)

echo 启动中... 首次运行会打开浏览器，请手动登录 DeepSeek
echo 登录完成后本服务即可使用（登录态会保存，以后不用再登）
echo.
echo 按 Ctrl+C 停止
echo.

python bridge.py

echo.
echo 服务已退出
pause
