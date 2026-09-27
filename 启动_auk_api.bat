@echo off
chcp 65001 >nul 2>&1
echo ============================================
echo   AuK 语音生成/编辑 API Server 启动器 by licor  若需要帮助，请联系微信：727909969
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

echo 启动 AuK 语音生成/编辑 API Server...
echo 端口 / 设备 / 并发等参数见 config\auk_server.yaml
echo 模型: models/AuK, models/AuK-Flash, models/Qwen2.5-Omni-3B (不存在将经魔搭自动下载到该统一目录)
echo.

:: 启动参数统一由 config/auk_server.yaml 提供（不传任何命令行参数）
:: 显存受限（如 16GB 显卡）默认只构建 Base 变体；显存充足（如 24GB 以上）可改为多变体
::   AuK (Base), AuK-Flash 或直接设置环境变量 AUK_VARIANTS（逗号分隔）后再启动。
if not defined AUK_VARIANTS set AUK_VARIANTS=AuK (Base)
"%PYTHON_EXECUTABLE%" auk_api_server.py

pause
