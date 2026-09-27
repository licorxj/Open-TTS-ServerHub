@echo off
chcp 65001 >nul
echo ============================================
echo   IndexTTS-2.5 API Server (Port 8858)
echo ============================================
echo.

SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

set GRADIO_TEMP_DIR=%cd%\tmp\

SET PYTHON_PATH=%cd%\py312env\
SET PYTHON_EXECUTABLE=%PYTHON_PATH%python.exe
SET PYTHONW_EXECUTABLE=%PYTHON_PATH%Scripts\pythonw.exe
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
SET PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)

echo Checking PyTorch...
"%PYTHON_EXECUTABLE%" -c "import torch; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyTorch not installed
    pause
    exit /b 1)
echo [OK] PyTorch OK

echo.
echo ==========================================
echo    Server Config (from config/index25_server.yaml)
echo ==========================================
echo  Config 文件: config\index25_server.yaml
echo  Model: IndexTTS-2.5
echo  （host / port / device / workers / model 等见配置文件）
echo ==========================================
echo.
echo  API Docs: http://localhost:8858/docs
echo.

REM 启动参数统一由 config/index25_server.yaml 提供（不传任何命令行参数）
"%PYTHON_EXECUTABLE%" index_tts25\index_api_server.py

pause
