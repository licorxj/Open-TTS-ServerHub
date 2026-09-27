@echo off
chcp 65001 >nul
echo ============================================================
echo   dots.tts WebUI
echo ============================================================
echo.

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
SET CU_PATH=%PYTHON_PATH%\Lib\site-packages\torch\lib
SET cuda_PATH=%PYTHON_PATH%\Library
SET CUDA_HOME=%PYTHON_PATH%\Library
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%PATH%

echo [1/3] Checking Python...
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)
echo [OK] Python OK

echo.
echo [2/3] Checking PyTorch...
"%PYTHON_EXECUTABLE%" -c "import torch; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyTorch not installed
    pause
    exit /b 1
)
echo [OK] PyTorch OK

echo.
echo [3/3] Starting dots.tts WebUI on http://localhost:7861
echo.

cd /d "%~dp0dots"
"%PYTHON_EXECUTABLE%" apps/gradio/app.py --host 0.0.0.0 --port 7861 --model-name-or-path "../models/dot" --optimize

pause
