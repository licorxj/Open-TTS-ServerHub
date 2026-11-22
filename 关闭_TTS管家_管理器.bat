@echo off
chcp 65001
title LcTTS 管家 · 关闭管理器

set "MGTPORT=5290"
set "API=http://127.0.0.1:%MGTPORT%/api/manager/shutdown"
set "HEALTH=http://127.0.0.1:%MGTPORT%/health"

echo ============================================
echo   LcTTS 管家 · 关闭统一管理器
echo ============================================
echo.

:: 说明：全程用 curl.exe 发请求，不把中文塞进 powershell 参数（中文易编坏）。
netstat -ano | findstr ":%MGTPORT%" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [提示] 未检测到 :%MGTPORT% 上的管理器进程，无需关闭。
    timeout /t 3 /nobreak >nul
    exit /b 0
)

echo 正在关闭统一管理器（会先停止后端 + 前端、释放显存）...

where curl >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "try { Invoke-RestMethod -Uri '%API%' -Method Post -TimeoutSec 120 | Out-Null; exit 0 } catch { exit 1 }"
) else (
    curl.exe -sS -X POST --max-time 120 "%API%" >nul
)

if errorlevel 1 (
    echo.
    echo [警告] 未能通过管理器接口完成关闭，尝试按端口强杀管理器进程...
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%MGTPORT%" ^| findstr "LISTENING"') do (
        taskkill /F /PID %%P >nul 2>&1
    )
)

echo 等待管理器进程退出 ...
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-RestMethod '%HEALTH%' -TimeoutSec 2 | Out-Null } catch { exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1"

netstat -ano | findstr ":%MGTPORT%" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [OK] 统一管理器已关闭：端口 %MGTPORT% 已释放，后端与前端已停止。
) else (
    echo.
    echo [警告] :%MGTPORT% 仍在监听，请稍候片刻或手动检查。
)

timeout /t 4 /nobreak >nul
