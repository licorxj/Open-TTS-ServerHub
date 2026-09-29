@echo off
chcp 936 >nul 2>&1
title LcTTS 管家 · 关闭

set "PORT=5199"
set "API=http://127.0.0.1:%PORT%/api/hub/shutdown"
set "HEALTH=http://127.0.0.1:%PORT%/health"

echo ============================================
echo   LcTTS 管家 · 关闭
echo ============================================
echo.

:: 说明：本脚本全程用 curl.exe 发请求，不把中文塞进 powershell 参数。
:: Windows 的 cmd 按系统 ANSI 代码页解析 .bat 并把参数编码后传给子进程，
:: 含中文的 powershell -Command 极易被编坏而静默失败；curl 参数全 ASCII 最稳。

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [提示] 未检测到 :%PORT% 上的管家进程，无需关闭。
    timeout /t 3 /nobreak >nul
    exit /b 0
)

echo 正在优雅关闭管家（会先卸载全部引擎、释放显存）...

where curl >nul 2>&1
if errorlevel 1 (
    echo [提示] 未找到 curl.exe（Windows 10 1803+ 自带），改用 PowerShell 发送请求...
    powershell -NoProfile -Command "try { Invoke-RestMethod -Uri '%API%' -Method Post -TimeoutSec 120 | Out-Null; exit 0 } catch { exit 1 }"
) else (
    curl.exe -sS -X POST --max-time 120 "%API%" >nul
)

if errorlevel 1 (
    echo.
    echo [警告] 未能通过管家接口完成关闭。
    echo        请勿直接在任务管理器强杀 pythonw.exe —— 那会留下 TTS 引擎子进程
    echo        继续占用显存；如需强制处理，请一并结束对应的 python.exe 引擎进程。
    pause
    exit /b 1
)

echo 等待管家进程退出 ...
powershell -NoProfile -Command "for ($i=0; $i -lt 60; $i++) { try { Invoke-RestMethod '%HEALTH%' -TimeoutSec 2 | Out-Null } catch { exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1"

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [OK] LcTTS 管家已关闭：端口 %PORT% 已释放，显存已归还。
) else (
    echo.
    echo [警告] :%PORT% 仍在监听，请稍候片刻或手动检查。
)

timeout /t 4 /nobreak >nul
