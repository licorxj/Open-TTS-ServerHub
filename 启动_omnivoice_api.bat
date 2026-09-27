@echo off
chcp 65001 >nul
title OmniVoice API Server by licor

:: ===========================================
:: OmniVoice TTS API Server 启动脚本 (Windows) by licor  若需要帮助，请联系微信：727909969
:: ===========================================

echo.
echo ==========================================
echo    OmniVoice API Server 启动器 by licor  若需要帮助，请联系微信：727909969
echo   更多开源AI整合包:https://www.kdocs.cn/l/ch0DFzxFCWhA
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

:: 检查 Python
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请确保 Python 已安装并添加到 PATH
    pause
    exit /b 1
)

:: 检测torch

echo 检查 PyTorch

"%PYTHON_EXECUTABLE%" -c "import torch; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] PyTorch 未安装或者不匹配,请安装适合你的系统的版本
    pause
    exit /b 1
)
echo [OK] PyTorch 正常


:: 默认参数
set HOST=0.0.0.0
set PORT=8853
set DEVICE=
set MODEL=models\omnivoice
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
echo  计算设备: %DEVICE% (auto=自动检测)
echo  模型路径: %MODEL%
echo  工作线程: %MAX_WORKERS%
echo ==========================================
echo.
echo  API 文档地址: http://localhost:%PORT%/docs
echo.

:: 构建命令
set "CMD=%PYTHON_EXECUTABLE% omnivoice\omni_api_server.py --host %HOST% --port %PORT% --model %MODEL% --max-workers %MAX_WORKERS%"
if not "%DEVICE%"=="" (
    set "CMD=%CMD% --device %DEVICE%"
)

echo [信息] 启动 API 服务器...
echo.

%CMD%

:: 如果服务器异常退出
echo.
echo [错误] 服务器已停止
pause
