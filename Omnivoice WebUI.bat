@echo off
chcp 65001 >nul
echo ============================================================
echo   OmniVoice WebUI启动器  整合包作者：licor
echo   B站ID：单手飞机  更多AI整合包资源：https://www.kdocs.cn/l/ch0DFzxFCWhA
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
SET PYTHONPATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%PATH%

echo [1/3] 检查 Python 环境...
"%PYTHON_EXECUTABLE%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 环境未找到！
    pause
    exit /b 1
)
echo [OK] Python 环境正常


echo.
echo [2/3] 检查 PyTorch DLL...
"%PYTHON_EXECUTABLE%" -c "import torch; print('[OK] PyTorch:', torch.__version__)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo [ERROR] PyTorch 未安装或者不匹配,请安装适合你的系统的版本
    pause
    exit /b 1
)
echo [OK] PyTorch 正常

echo.
echo [3/3] 启动 WebUI 服务器...
echo.
echo ============================================================
echo  WebUI 将在浏览器中打开：http://localhost:8852
echo  按 Ctrl+C 可停止服务器
echo ============================================================
echo.

"%PYTHON_EXECUTABLE%" -s omnivoice\webuiapp.py --port 8852

pause
