@echo off
chcp 65001 >nul 2>&1
echo ============================================
echo   install_flash_attn  (Windows, 离线安装本地 whl)
echo   适用于 LcTTSHub 的 py312env (Python 3.12)
echo ============================================
echo.

:: 清除 conda / virtualenv 环境变量，防止污染
SET CONDA_PREFIX=
SET CONDA_DEFAULT_ENV=
SET CONDA_PROMPT_MODIFIER=
SET VIRTUAL_ENV=
SET PYTHONHOME=
SET PYTHONPATH=
SET PY_PYTHON=

:: 使用项目自带虚拟环境
SET PYTHON_PATH=%~dp0py312env\
SET PYTHON_EXECUTABLE=%PYTHON_PATH%python.exe
SET PATH=%PYTHON_PATH%;%PYTHON_PATH%Scripts;%SystemRoot%;%SystemRoot%\System32;%SystemRoot%\System32\Wbem

:: 检查 python
"%PYTHON_EXECUTABLE%" --version >nul 2>&1 || (
    echo [错误] 未找到 %PYTHON_EXECUTABLE%
    pause
    exit /b 1
)

:: 定位 whl 目录
SET WHL_DIR=%~dp0whl
if not exist "%WHL_DIR%" (
    echo [错误] 未找到 whl 目录: %WHL_DIR%
    echo 请将 flash_attn 的 Windows whl 放到 whl\ 目录下
    pause
    exit /b 1
)

:: 查找匹配的本地 whl (cp312 / win_amd64)
SET LOCAL_WHL=
for %%f in ("%WHL_DIR%\flash_attn*cp312*win_amd64.whl") do (
    if exist "%%f" SET LOCAL_WHL=%%f
)
if "%LOCAL_WHL%"=="" (
    echo [错误] 未在 %WHL_DIR% 找到 flash_attn*cp312*win_amd64.whl
    pause
    exit /b 1
)

echo 使用本地 whl: %LOCAL_WHL%
echo 开始离线安装 (--no-index, 不联网)...
"%PYTHON_EXECUTABLE%" -m pip install --no-index --find-links "%WHL_DIR%" "%LOCAL_WHL%"
if errorlevel 1 (
    echo [错误] 安装失败，请检查 whl 与 Python 版本是否匹配
    pause
    exit /b 1
)

echo.
echo 验证安装...
"%PYTHON_EXECUTABLE%" -c "import flash_attn; print('[OK] flash_attn', flash_attn.__version__)"
echo.
echo 完成。
pause
