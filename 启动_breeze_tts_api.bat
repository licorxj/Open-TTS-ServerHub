@echo off
chcp 65001 >nul
title Breeze-TTS-2 API Server by licor

:: ===========================================
:: Breeze-TTS-2 TTS API Server 启动脚本 (Windows) by licor  若需要帮助，请联系微信：727909969
:: ===========================================

echo.
echo ==========================================
echo    Breeze-TTS-2 API Server 启动器 by licor  若需要帮助，请联系微信：727909969
echo    更多开源AI整合包:https://www.kdocs.cn/l/ch0DFzxFCWhA
echo ==========================================
echo.

:: 清除 conda/virtualenv 环境变量，防止污染
SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

REM Breeze-TTS-2 运行在独立的 breezeenv 中，需要 transformers 4.57.3 与 qwen-tts 0.1.1
REM 与共享 py312env 的 transformers 5.3.0 不兼容。若该环境不存在，请先创建：
REM   py312env\python.exe -m venv --system-site-packages breezeenv
REM   breezeenv\Scripts\python.exe -m pip install qwen-tts==0.1.1
SET PYTHON_PATH=%cd%\breezeenv\Scripts
SET PYTHON_EXECUTABLE=%PYTHON_PATH%\python.exe

if not exist "%PYTHON_EXECUTABLE%" (
    echo [错误] 未找到 breezeenv 虚拟环境：%PYTHON_EXECUTABLE%
    echo   请先创建 breezeenv 并安装依赖（见上方注释）。
    pause
    exit /b 1
)

SET GRADIO_TEMP_DIR=%cd%\tmp\
SET PYTHONHOME=
SET PYTHONPATH=
SET TORCH_HOME=%cd%\cache
SET HF_ENDPOINT=https://hf-mirror.com
SET HF_HOME=%CD%\hf_download
SET TQDM_DISABLE=1
SET HF_HUB_DISABLE_PROGRESS_BARS=1
SET HF_DATASETS_DISABLE_PROGRESS_BARS=1
SET PATH=%cd%\py312env%;%cd%\py312env\Scripts;%cd%\py312env\ffmpeg;%PYTHON_PATH%;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

:: 检查 Python
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请确认 breezeenv 已正确创建
    pause
    exit /b 1
)

:: 检测 torch / CUDA
echo 检查 PyTorch / CUDA
"%PYTHON_EXECUTABLE%" -c "import torch; assert torch.cuda.is_available(), 'no cuda'; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] 未检测到可用的 CUDA GPU。Breeze-TTS-2 需要 NVIDIA CUDA 设备。
    pause
    exit /b 1
)
echo [OK] CUDA 可用

:: 默认参数
set HOST=0.0.0.0
set PORT=8860
set DEVICE=cuda:0
set MODEL=models\Breeze-TTS-2
set MAX_WORKERS=2

:: 解析命令行参数
:parse_args
if "%~1"=="" goto :start_server
if "%~1"=="--host" (
    set HOST=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--port" (
    set PORT=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--device" (
    set DEVICE=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--model" (
    set MODEL=%~2
    shift
    shift
    goto :parse_args
)
if "%~1"=="--max-workers" (
    set MAX_WORKERS=%~2
    shift
    shift
    goto :parse_args
)
shift
goto :parse_args

:start_server
echo.
echo ==========================================
echo    启动参数
echo ==========================================
echo  服务地址: %HOST%
echo  服务端口: %PORT%
echo  计算设备: %DEVICE%
echo  模型路径: %MODEL%
echo  工作线程: %MAX_WORKERS%
echo ==========================================
echo.
echo  API 文档地址: http://localhost:%PORT%/docs
echo.

set "CMD=%PYTHON_EXECUTABLE% breeze_tts_api.py --host %HOST% --port %PORT% --device %DEVICE% --model %MODEL% --max-workers %MAX_WORKERS%"

echo [信息] 启动 API 服务器...
echo.

%CMD%

echo.
echo [错误] 服务器已停止
pause
