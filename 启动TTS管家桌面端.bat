@echo off
chcp 936 >nul 2>&1
setlocal EnableDelayedExpansion
title LcTTS 管家 · 桌面端启动器

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PYW=%ROOT%\py312env\pythonw.exe"
set "PORT=5199"
set "LOGDIR=%ROOT%\logs\tts_hub"
set "HUBLOG=%LOGDIR%\hub.log"
set "HUBERR=%LOGDIR%\hub.err.log"
set "URL=http://127.0.0.1:%PORT%/ui"

echo ============================================
echo   LcTTS 管家 · 桌面端
echo     后台常驻（无控制台窗口）+ 面板窗口
echo ============================================
echo.

if not exist "%PYW%" (
    echo [错误] 未找到 py312env\pythonw.exe
    echo        请确认在项目根目录下运行，且 py312env 环境完整。
    pause
    exit /b 1
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

:: ---------------------------------------------------------------------------
:: 1. 是否已在运行
:: ---------------------------------------------------------------------------
set "RUNNING=0"
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if !errorlevel!==0 set "RUNNING=1"

if "!RUNNING!"=="1" (
    echo [跳过] 检测到 :%PORT% 已在监听，管家已处于运行状态。
) else (
    echo 正在后台启动管家 :%PORT% ...
    powershell -NoProfile -Command ^
      "Start-Process -FilePath '%PYW%' -ArgumentList 'tts_hub_server.py','--port','%PORT%' -WorkingDirectory '%ROOT%' -WindowStyle Hidden -RedirectStandardOutput '%HUBLOG%' -RedirectStandardError '%HUBERR%'"

    echo 等待管家就绪 ...
    powershell -NoProfile -Command ^
      "$u='http://127.0.0.1:%PORT%/health'; for($i=0;$i -lt 80;$i++){ try{ $r=Invoke-RestMethod $u -TimeoutSec 2; if($r.status -eq 'ok'){ exit 0 } }catch{}; Start-Sleep -Milliseconds 500 }; exit 1"
    if !errorlevel! neq 0 (
        echo.
        echo [错误] 管家启动超时。请查看日志：
        echo        %HUBERR%
        echo        %HUBLOG%
        pause
        exit /b 1
    )
    echo [OK] 管家已就绪
)
echo.

:: ---------------------------------------------------------------------------
:: 2. 自检：面板产物是否可访问
:: ---------------------------------------------------------------------------
where curl >nul 2>&1
if not errorlevel 1 (
    curl.exe -s -o "%TEMP%\lctts_ui.html" "%URL%"
    for %%A in ("%TEMP%\lctts_ui.html") do set "UISIZE=%%~zA"
    if !UISIZE! LSS 400 (
        echo [警告] 面板响应异常（!UISIZE! 字节）。
        echo        请先构建前端：cd frontend ^&^& npm run build
        echo        然后重新打开 %URL%
    ) else (
        echo [OK] 面板可访问（!UISIZE! 字节）
    )
)

:: ---------------------------------------------------------------------------
:: 3. 打开面板
::
:: 实测：Chrome 的 --app 模式正常（无地址栏、无标签页，窗口标题即页面标题）；
:: 而 Edge 的 --app 模式只会取回 index.html、不加载后续 JS/CSS，表现为白屏，
:: 因此 Edge 改用 --new-window 普通打开以保证可用。
:: 想让 Edge 也去掉地址栏：在 Edge 里打开面板后，菜单 → 应用 → 安装此站点为应用。
:: ---------------------------------------------------------------------------
set "APP_BROWSER="
set "PLAIN_BROWSER="

for %%P in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if not defined APP_BROWSER if exist %%P set "APP_BROWSER=%%~P"
)

for %%P in (
    "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
    "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
) do (
    if not defined PLAIN_BROWSER if exist %%P set "PLAIN_BROWSER=%%~P"
)

if defined APP_BROWSER goto :open_app
if defined PLAIN_BROWSER goto :open_plain

echo 未找到 Chrome/Edge，改用默认浏览器打开 %URL%
start "" "%URL%"
goto :opened

:open_app
echo 打开面板（应用模式，无地址栏）：%URL%
start "" "%APP_BROWSER%" --app="%URL%" --window-size=1920,1080 --window-position=0,0
goto :opened

:open_plain
echo 打开面板（新窗口）：%URL%
start "" "%PLAIN_BROWSER%" --new-window "%URL%" --window-size=1920,1080 --window-position=0,0
goto :opened

:opened

echo.
echo ============================================
echo   LcTTS 管家已在后台运行
echo     面板   %URL%
echo     文档   http://127.0.0.1:%PORT%/docs
echo     日志   %HUBLOG%
echo.
echo   面板窗口初始尺寸 1920x1080；若屏幕分辨率更小，浏览器会自动缩到屏幕可用区域。
echo   关闭管家：双击 关闭TTS管家桌面端.bat
echo   （直接在任务管理器强杀会留下引擎子进程继续占用显存）
echo ============================================
timeout /t 6 /nobreak >nul
endlocal
