@echo off
rem =========================================================================
rem  Open-TTS-ServerHub —— 更新代码
rem  双击即可同步远程最新代码。
rem  无论成功、失败还是中途报错，本窗口都会保持打开并显示结果。
rem =========================================================================

rem --- 窗口停留保障：若双击运行（无参数），以 cmd /k 重新执行自身 ---
rem     cmd /k 执行完后不会关闭窗口，确保结果始终可见。
if "%~1"=="" (
    "%COMSPEC%" /k "%~f0" INNER
    exit /b
)

setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title Open-TTS-ServerHub - 更新代码
cd /d "%~dp0"

echo ============================================================
echo    Open-TTS-ServerHub  ——  更新代码
echo ============================================================
echo.
echo 项目目录: %CD%
echo.

set "STASHED="
set "WARN="

echo [步骤 0/4] 检查运行环境...
where git >nul 2>&1
if errorlevel 1 (
    echo   [失败] 未检测到 git。请先安装 Git for Windows: https://git-scm.com
    goto :FAIL
)
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo   [失败] 当前目录不是 Git 仓库。请把本脚本放在项目根目录（含 .git 的目录）后再运行。
    goto :FAIL
)
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo   [失败] 未配置远程 origin。请先添加远程: git remote add origin 你的仓库地址
    goto :FAIL
)
echo   [通过] git 可用、位于仓库内、远程 origin 已配置。

echo.
echo [步骤 1/4] 检查本地未提交的修改...
git diff --quiet
if errorlevel 1 goto :HASEDITS
git diff --cached --quiet
if errorlevel 1 goto :HASEDITS
echo   [通过] 工作区干净，可直接更新。
goto :DOPULL

:HASEDITS
echo   [提示] 检测到本地有未提交的修改。
echo.
set /p "CHOICE=  是否先 stash 暂存本地修改再更新？(直接回车=是 / 输入 n 取消): "
if /i "!CHOICE!"=="n" (
    echo   [已取消] 请先提交或处理本地修改，然后重新运行本脚本。
    goto :FAIL
)
echo   正在 git stash 暂存本地修改...
git stash push -m "auto-stash before update"
if errorlevel 1 (
    echo   [失败] stash 暂存失败。
    goto :FAIL
)
set "STASHED=1"
echo   [通过] 已暂存本地修改。

:DOPULL
echo.
echo [步骤 2/4] 拉取远程最新代码（git pull）...
git pull --ff-only
if errorlevel 1 (
    echo   [失败] git pull 失败。
    echo   可能原因: 网络不可达 / 未配置 SSH 密钥或 HTTPS 凭据 / 本地提交与远程冲突。
    echo   当前远程地址:
    git remote get-url origin
    if defined STASHED (
        echo   正在恢复之前暂存的本地修改...
        git stash pop
    )
    goto :FAIL
)
echo   [通过] 代码已拉取到最新。

echo.
echo [步骤 3/4] 同步 Git LFS 大文件（unidic 词典等）...
git lfs version >nul 2>&1
if errorlevel 1 (
    echo   [提示] 未安装 Git LFS，跳过。如需可安装 git-lfs 后手动执行: git lfs pull
) else (
    git lfs pull
    if errorlevel 1 (
        echo   [警告] LFS 大文件同步失败。代码已更新，但部分大文件可能缺失。
        set "WARN=1"
    ) else (
        echo   [通过] LFS 大文件已是最新。
    )
)

echo.
echo [步骤 4/4] 恢复本地修改（如有）...
if defined STASHED (
    git stash pop
    if errorlevel 1 (
        echo   [警告] 本地修改与更新冲突，未能自动恢复。
        echo   请手动处理: git stash show 查看，git stash drop 丢弃。
        set "WARN=1"
    ) else (
        echo   [通过] 本地修改已恢复。
    )
) else (
    echo   [跳过] 本次未暂存任何本地修改。
)

echo.
echo ============================================================
echo   更新完成！当前版本（commit）:
git --no-pager log -1 --oneline
echo ============================================================
if defined WARN (
    echo [结果] 更新已完成，但过程中有告警/失败步骤，请查看上方提示。
) else (
    echo [结果] 全部步骤成功，代码已是最新。
)
goto :END

:FAIL
echo.
echo ============================================================
echo   [结果] 更新未完成，请根据上方错误/提示处理后重新运行本脚本。
echo ============================================================

:END
echo.
echo -------- 结果已在上方显示 --------
echo 查看完毕后，按任意键结束本脚本（窗口会保留，可直接查看记录）。
pause >nul
endlocal
