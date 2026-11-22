@echo off
chcp 65001
setlocal EnableDelayedExpansion
title LcTTS 管家 · 统一管理器（守护进程）

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYW=%ROOT%\py312env\pythonw.exe"
set "MGTPORT=5290"
set "HUBPORT=5190"
set "LOGDP=%ROOT%\logs\manager"
set "MGTOUT=%LOGDP%\manager.out.log"
set "MGTERR=%LOGDP%\manager.err.log"
set "PANEL=http://127.0.0.1:%HUBPORT%/ui"

echo ============================================
echo   LcTTS 管家 · 统一管理器（守护进程）
echo     后端 + 前端统一看护 · 端口守护 · 控制 API
echo ============================================
echo.

if not exist "%PYW%" (
    echo [错误] 未找到 py312env\pythonw.exe
    echo        请确认在项目根目录下运行，且 py312env 环境完整。
    pause
    exit /b 1
)
if not exist "%LOGDP%" mkdir "%LOGDP%" >nul 2>&1

:: ---------------------------------------------------------------------------
:: 1. 是否已在运行
:: ---------------------------------------------------------------------------
set "RUNNING=0"
netstat -ano | findstr ":%MGTPORT%" | findstr "LISTENING" >nul 2>&1
if !errorlevel!==0 set "RUNNING=1"

if "!RUNNING!"=="1" (
    echo [跳过] 检测到 :%MGTPORT% 已在监听，管理器已处于运行状态。
) else (
    echo 正在后台启动管理器 :%MGTPORT% ...
    powershell -NoProfile -Command ^
      "Start-Process -FilePath '%PYW%' -ArgumentList '%ROOT%\tts_hub\manager_daemon.py' -WorkingDirectory '%ROOT%' -WindowStyle Hidden -RedirectStandardOutput '%MGTOUT%' -RedirectStandardError '%MGTERR%'"

    echo 等待管理器就绪 ...
    powershell -NoProfile -Command ^
      "$u='http://127.0.0.1:%MGTPORT%/health'; for($i=0;$i -lt 80;$i++){ try{ $r=Invoke-RestMethod $u -TimeoutSec 2; if($r.status -eq 'ok'){ exit 0 } }catch{}; Start-Sleep -Milliseconds 500 }; exit 1"
    if !errorlevel! neq 0 (
        echo.
        echo [错误] 管理器启动超时。请查看日志：
        echo        %MGTERR%
        echo        %MGTOUT%
        echo ---- manager.err.log ----
        type "%MGTERR%" 2>nul
        echo ---- manager.out.log ----
        type "%MGTOUT%" 2>nul
        pause
        exit /b 1
    )
    echo [OK] 管理器已就绪
)
echo.

:: ---------------------------------------------------------------------------
:: 2. 打开面板（hub /ui）
::    管理器的控制能力已内置进面板的「后端管理」标签页，不再单独弹窗，
::    避免「统一管理器页 + 调度平台页」两个页面并存、逻辑混乱。
::    （管理器控制页仍保留在 http://127.0.0.1:%MGTPORT%/ ，仅在面板不可用时手动救急）
:: ---------------------------------------------------------------------------

:: Chrome 应用窗口打开 hub 面板（无地址栏）
for %%P in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if not defined CHROME if exist %%P set "CHROME=%%~P"
)
if defined CHROME (
    powershell -NoProfile -ExecutionPolicy Bypass -File "tts_hub\open_panel.ps1" -Browser "%CHROME%" -Url "%PANEL%" -Mode app
) else (
    echo 未找到 Chrome，改用默认浏览器打开面板：%PANEL%
    start "" "%PANEL%"
)

echo.
echo ============================================
echo   LcTTS 统一管理器已在后台运行
echo     面板       %PANEL%
echo.
echo   在面板左侧「后端管理」标签页即可启停 / 重启后端。
echo   关闭：双击 关闭_TTS管家_管理器.bat（会先停止后端再退出管理器）
echo ============================================
timeout /t 6 /nobreak >nul
endlocal
