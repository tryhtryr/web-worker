@echo off
chcp 65001 >nul
title 安装 web-worker 技能到 DSH

set SRC=%~dp0..\skill
set DST=%USERPROFILE%\.dsh\skills\web-worker

echo ============================================
echo   安装 web-worker 技能
echo ============================================
echo 源:   %SRC%
echo 目标: %DST%
echo.

if not exist "%SRC%\SKILL.md" (
  echo [错误] 找不到 %SRC%\SKILL.md
  pause
  exit /b 1
)

if not exist "%USERPROFILE%\.dsh\skills" (
  echo [提示] 创建技能目录
  mkdir "%USERPROFILE%\.dsh\skills" 2>nul
)

if exist "%DST%" (
  echo [提示] 目标已存在，先备份
  move "%DST%" "%DST%.bak-%RANDOM%" >nul
)

xcopy /E /I /Q "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo [错误] 复制失败
  pause
  exit /b 1
)

echo [OK] 已安装到 %DST%
echo.
echo 内容：
dir /b "%DST%"
echo.
echo 请重启 DeepSeek Harness，技能列表里会出现 web-worker
echo.
echo 使用前记得先启动桥接：scripts\start_bridge.bat
echo.
pause
