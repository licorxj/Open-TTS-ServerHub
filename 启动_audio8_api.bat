@echo off
chcp 65001 >nul 2>&1
echo ============================================
echo   Audio8_TTS API Server 启动器 by licor  若需要帮助，请联系微信：727909969
echo   更多开源AI整合包:https://www.kdocs.cn/l/ch0DFzxFCWhA
echo ============================================
echo.

:: 清除 conda/virtualenv 环境变量，防止污染
SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

:: 关闭实时任务面板清屏刷屏
SET TTS_DASHBOARD=0

set GRADIO_TEMP_DIR=%cd%\tmp\
SET PYTHON_PATH=%cd%\py312env\
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

:: 清除 PATH 中的系统 Python/conda 路径，只保留 Windows 系统路径和项目环境
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

:: 检查 Python
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先运行“虚拟环境.bat”
    pause
    exit /b 1
)

echo 启动 Audio8_TTS API Server...
echo 端口 / 设备 / 并发等参数见 config\audio8_server.yaml
echo 模型: models/Audio8 (不存在将自动下载到此统一目录)
echo.

REM 启动参数统一由 config/audio8_server.yaml 提供（不传任何命令行参数）
"%PYTHON_EXECUTABLE%" audio8_api_server.py

pause
