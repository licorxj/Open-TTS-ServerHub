@echo off
chcp 936 >nul 2>&1
echo ============================================
echo   LcTTS 管家 · 开发模式（免 build）
echo     后端 :5199  +  前端 Vite :5180（热更新）
echo ============================================
echo.

set "ROOT=%cd%"
set "PY=%ROOT%\py312env\python.exe"
set "PORT=5199"

:: 注意：开发模式【不能】清空 PATH —— 需要同时用到 py312env 的 python 和系统的 node/npm
:: 这里只把 py312env 前置，保留原有 PATH
set "PATH=%ROOT%\py312env;%ROOT%\py312env\Scripts;%PATH%"

:: ---------------- 1. 环境检查 ----------------
if not exist "%PY%" (
    echo [错误] 未找到 py312env\python.exe
    pause
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js
    echo   开发模式需要 Node 18+ 才能运行 Vite。
    echo   安装：https://nodejs.org/  （或用 nvm / winget install OpenJS.NodeJS.LTS）
    echo   装完重开本窗口即可。
    echo.
    echo   若只需使用面板（不改前端代码），请改用：
    echo     启动_TTS管家.bat            或    启动TTS管家桌面端.bat
    echo   它们直接用已构建的 tts_hub\static\，运行时不需要 Node。
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('node --version') do set NODEV=%%v
echo [OK] Python:
"%PY%" --version
echo [OK] Node: %NODEV%
echo.

:: ---------------- 2. 首次运行自动装前端依赖 ----------------
if not exist "%ROOT%\frontend\node_modules" (
    echo 首次运行，正在安装前端依赖（约 1-2 分钟）...
    pushd "%ROOT%\frontend"
    call npm install --no-audit --no-fund
    if errorlevel 1 (
        echo [错误] npm install 失败
        popd
        pause
        exit /b 1
    )
    popd
    echo [OK] 前端依赖已就绪
    echo.
)

:: ---------------- 3. 拉起后端管家 ----------------
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [跳过] 检测到 :%PORT% 已有服务在监听，假定管家已在运行。
) else (
    echo 正在启动后端管家 :%PORT% ...
    start "LcTTS管家-后端 :%PORT%" cmd /k "cd /d %ROOT% & "%PY%" tts_hub_server.py --port %PORT%"
    echo 等待后端就绪...
    timeout /t 5 /nobreak >nul
)
echo.

:: ---------------- 4. 拉起 Vite 开发服务器 ----------------
echo 正在启动前端 Vite :5180 ...
start "LcTTS管家-前端 :5180" cmd /k "cd /d %ROOT%\frontend & npm run dev"
timeout /t 5 /nobreak >nul

:: ---------------- 5. 打开浏览器 ----------------
start "" "http://127.0.0.1:5180/console"

echo ============================================
echo   开发模式已就绪
echo     面板   http://127.0.0.1:5180/console
echo     接口   http://127.0.0.1:%PORT%/docs
echo.
echo   改动 frontend\src 下任意文件 → 浏览器自动热更新，无需 npm run build
echo   若 :5180 被占用，Vite 会自动顺延端口，请以「LcTTS管家-前端」窗口
echo   实际输出的 Local 地址为准（/api 代理目标固定为 %PORT%）。
echo.
echo   关闭：直接关掉那两个命令行窗口即可
echo ============================================
echo.
echo 提示：发布前请执行  cd frontend ^&^& npm run build  更新 tts_hub\static\
pause
