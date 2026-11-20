@echo off
chcp 65001 >nul
setlocal EnableExtensions
title Toc Memex Inspector
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"
set "NO_OPEN=%~2"
set "URL=http://127.0.0.1:%PORT%/"

echo.
echo   Toc Memex Inspector
echo   ================================
echo   网页地址: %URL%
echo   ================================
echo.

if not exist "data\tocmemex.db" (
    echo   [错误] 找不到数据库 data\tocmemex.db
    echo   请先初始化并导入文档，或把本脚本放到项目根目录运行。
    echo.
    pause
    exit /b 1
)

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    where uv >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON=uv run python"
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo   [错误] 未找到 Python，请先安装依赖。
            echo.
            pause
            exit /b 1
        )
        set "PYTHON=python"
    )
)

if /i "%NO_OPEN%"=="--no-open" goto :run
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 80; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', %PORT%); $c.Close(); Start-Process '%URL%'; break } catch { Start-Sleep -Milliseconds 250 } }"

:run
echo   正在启动，按 Ctrl+C 停止服务。
echo.
%PYTHON% -m app.inspector --db "data\tocmemex.db" --port %PORT%

echo.
echo   服务已停止。
pause