@echo off
chcp 65001 >nul
title Confucius4-TTS API Server by licor

REM ===========================================
REM Confucius4-TTS API Server
REM ===========================================

echo.
echo ==========================================
echo    Confucius4-TTS API Server by licor
echo   https://www.kdocs.cn/l/ch0DFzxFCWhA
echo ==========================================
echo.

REM Clear conda/virtualenv env vars
SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

REM 关闭实时任务面板清屏刷屏（设为 1 可恢复旧版实时面板）
SET TTS_DASHBOARD=0

set GRADIO_TEMP_DIR=%cd%\tmp\
SET PYTHON_PATH=%cd%\py312env\
SET PYTHONHOME=
SET PYTHONPATH=
SET PYTHONEXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONWEXECUTABLE=%PYTHON_PATH%pythonw.exe
SET PYTHON_EXECUTABLE=%PYTHON_PATH%\python.exe
SET PYTHONW_EXECUTABLE=%PYTHON_PATH%pythonw.exe
SET PYTHON_BIN_PATH=%PYTHON_EXECUTABLE%
SET PYTHON_LIB_PATH=%PYTHON_PATH%\Lib\site-packages
SET FFMPEG_PATH=%cd%\py312env\ffmpeg
SET TORCH_HOME=%cd%\cache
SET HF_ENDPOINT=https://hf-mirror.com
SET HF_HOME=%CD%\hf_download
SET TQDM_DISABLE=1
SET HF_HUB_DISABLE_PROGRESS_BARS=1
SET HF_DATASETS_DISABLE_PROGRESS_BARS=1
SET CU_PATH=%PYTHON_PATH%\Lib\site-packages\torch\lib
SET cuda_PATH=%PYTHON_PATH%\Library
SET CUDA_HOME=%PYTHON_PATH%\Library
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

REM Check Python
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)

REM Check PyTorch
echo Checking PyTorch...
"%PYTHON_EXECUTABLE%" -c "import torch; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] PyTorch not found or mismatch
    pause
    exit /b 1
)
echo [OK] PyTorch OK

REM ===========================================================
REM 启动参数统一由 config/confucius4_server.yaml 提供
REM （host / port / device / max_workers 等不再通过命令行传入）
REM 如需临时微调，仍是“改配置文件 -> 重启服务”即可。
REM ===========================================================

:start_server
echo.
echo ==========================================
echo    Server Config (from config/confucius4_server.yaml)
echo ==========================================
echo  Config 文件: config\confucius4_server.yaml
echo  Model: Confucius4-TTS
echo  （host / port / device / workers 等见配置文件）
echo ==========================================
echo.
echo  API Docs: http://localhost:8857/docs
echo.

REM Build command —— 不传任何启动参数，全部以 config 文件为准
set "CMD=%PYTHON_EXECUTABLE% confucius4_api_server.py"

echo [INFO] Starting API Server...
echo.

%CMD%

REM If server exits
echo.
echo [ERROR] Server stopped
pause
