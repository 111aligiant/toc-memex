@echo off
rem cmd parses this file with the console code page active when it opens it.
rem On a Chinese console (936) that can split UTF-8 lines into bogus commands.
rem Re-launch once under 65001 before any Chinese text is parsed.
if defined TOCMEMEX_UTF8 goto :utf8_ready
set "TOCMEMEX_UTF8=1"
chcp 65001 >nul
"%ComSpec%" /c ""%~f0" %*"
exit /b %errorlevel%
:utf8_ready
chcp 65001 >nul
setlocal EnableExtensions
title Toc Memex
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8791"
set "NO_OPEN=%~2"
set "URL=http://127.0.0.1:%PORT%/"

echo.
echo   Toc Memex — 档案库
echo   ================================
echo   网页地址: %URL%
echo   ================================
echo.

if not exist "web\dist\index.html" (
    echo   [提示] web\dist 不存在，先构建前端：cd web ^&^& npm install ^&^& npm run build
    echo.
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

call :ensure_frontend
if errorlevel 1 (
    pause
    exit /b 1
)

call :check_port
if errorlevel 1 (
    pause
    exit /b 1
)

if not exist "data\tocmemex.db" (
    echo   首次启动，正在初始化空档案库...
    %PYTHON% -m app.cli init
    if errorlevel 1 (
        echo   [错误] 初始化失败。
        echo.
        pause
        exit /b 1
    )
)

if exist "data\tocmemex.db" (
    %PYTHON% scripts\backup.py --data-dir data --keep 3
    if errorlevel 1 echo   [提示] 备份失败，继续启动。
)

if /i "%NO_OPEN%"=="--no-open" goto :run
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 120; $i++) { try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', %PORT%); $c.Close(); Start-Process '%URL%'; break } catch { Start-Sleep -Milliseconds 250 } }"

:run
echo   正在启动，按 Ctrl+C 停止服务。
echo.
%PYTHON% -m app.web_api --db "data\tocmemex.db" --port %PORT% --web-dir "web\dist"

echo.
echo   服务已停止。
pause
exit /b 0

rem ---------------------------------------------------------------------------
rem Rebuild web\dist when the checked-out frontend sources are newer than the
rem last build. Merging a worktree only updates sources and those are ignored,
rem so without this step the launcher keeps serving an old build forever.
rem ---------------------------------------------------------------------------
:ensure_frontend
if not exist "web\dist\index.html" set "NEED_FRONTEND_BUILD=1"
if not defined NEED_FRONTEND_BUILD (
    powershell -NoProfile -Command "$d=(Get-Item -LiteralPath 'web\dist\index.html').LastWriteTimeUtc; $s=@(Get-ChildItem -LiteralPath 'web\src' -Recurse -File -ErrorAction SilentlyContinue) + @(Get-Item -Path 'web\index.html','web\package.json','web\vite.config.*' -ErrorAction SilentlyContinue); if (@($s | Where-Object { $_.LastWriteTimeUtc -gt $d }).Count -gt 0) { exit 1 } else { exit 0 }"
    if errorlevel 1 set "NEED_FRONTEND_BUILD=1"
)
if not defined NEED_FRONTEND_BUILD (
    echo   前端已是当前源码的构建结果。
    echo.
    exit /b 0
)
where npm >nul 2>nul
if errorlevel 1 (
    echo   [警告] 未找到 npm，无法重新构建前端，将继续使用现有 web\dist。
    echo          刚合并的前端改动不会生效，请安装 Node.js 后重试。
    echo.
    exit /b 0
)
if not exist "web\node_modules" (
    echo   首次构建前端，正在安装依赖...
    pushd "web"
    call npm install
    if errorlevel 1 (
        popd
        echo   [错误] 前端依赖安装失败。
        echo.
        exit /b 1
    )
    popd
)
echo   检测到前端源码有更新，正在重新构建 web\dist ...
echo.
pushd "web"
call npm run build
if errorlevel 1 (
    popd
    echo.
    echo   [错误] 前端构建失败，已中止启动，避免展示旧页面。
    echo.
    exit /b 1
)
popd
echo   前端构建完成。
echo.
exit /b 0

rem ---------------------------------------------------------------------------
rem A server left over from an earlier launch keeps the port and never loads
rem the newly merged backend code. Ask before replacing it, because worktree
rem sessions may deliberately run their own server on another port.
rem ---------------------------------------------------------------------------
:check_port
set "PORT_PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING" 2^>nul') do set "PORT_PID=%%p"
if not defined PORT_PID exit /b 0
echo   [提示] 端口 %PORT% 已被进程 %PORT_PID% 占用（通常是上次未关闭的服务）。
echo          它不会加载刚合并的后端代码，继续启动只会打开旧服务。
choice /c YN /n /m "  是否停止该进程并重新启动? [Y/N] "
if errorlevel 2 (
    echo.
    echo   已取消。请手动关闭该进程，或换端口启动：start-web.bat 8793
    echo.
    exit /b 1
)
taskkill /PID %PORT_PID% /T /F >nul 2>nul
if errorlevel 1 (
    echo   [错误] 无法停止进程 %PORT_PID%，请手动关闭后重试。
    echo.
    exit /b 1
)
echo   已停止旧服务。
timeout /t 1 /nobreak >nul
exit /b 0
