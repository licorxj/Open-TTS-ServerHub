@echo off
CHCP 65001
echo ============================================================
echo   OmniVoice 项目虚拟环境  
echo ============================================================
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
set CU_PATH=%PYTHON_PATH%\Lib\site-packages\torch\lib
set cuda_PATH=%PYTHON_PATH%\Library
set CUDA_HOME=%PYTHON_PATH%\Library
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%\Scripts;%FFMPEG_PATH%;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

echo 已经进入项目虚拟环境

cmd /k 
