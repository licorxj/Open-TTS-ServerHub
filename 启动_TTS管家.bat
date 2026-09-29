@echo off
chcp 936 >nul 2>&1
echo ============================================
echo   LcTTS 管家 (Port 5199) by licor
echo   一个端口调用全部 TTS 引擎，按 model 自动拉起/卸载
echo ============================================
echo.

:: 清除 conda/virtualenv 环境变量，防止污染子进程
SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

SET PYTHON_PATH=%cd%\py312env\
SET PYTHON_EXECUTABLE=%PYTHON_PATH%python.exe
SET FFMPEG_PATH=%cd%\py312env\ffmpeg
SET TORCH_HOME=%cd%\cache
SET HF_ENDPOINT=https://hf-mirror.com
SET HF_HOME=%CD%\hf_download
SET TQDM_DISABLE=1
SET HF_HUB_DISABLE_PROGRESS_BARS=1
SET HF_DATASETS_DISABLE_PROGRESS_BARS=1
SET PYTHONIOENCODING=utf-8
SET PYTHONUTF8=1

SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请确认 py312env 存在
    pause
    exit /b 1
)

echo 配置文件: config\tts_hub.yaml
echo   面板: http://localhost:5199/ui
echo   文档: http://localhost:5199/docs
echo.
echo 提示：首次请求某引擎会自动拉起它（含模型下载，可能数分钟），
echo       切换 model 时会自动卸载上一个引擎释放显存；
echo       空闲超过 hub.idle_ttl_seconds（默认 300 秒 = 5 分钟）也会自动卸载。
echo.

"%PYTHON_EXECUTABLE%" tts_hub_server.py %*

pause
