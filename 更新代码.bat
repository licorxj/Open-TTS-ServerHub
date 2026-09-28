@echo off
chcp 65001 >nul
title 更新 Open-TTS-ServerHub 代码
cd /d "%~dp0"

echo ============================================================
echo    Open-TTS-ServerHub  ——  更新代码
echo ============================================================
echo.

:: 1. 检查 git 是否安装
where git >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 git，请先安装 Git for Windows：https://git-scm.com
    goto :wait
)

:: 2. 检查是否在 git 仓库内
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前目录不是 Git 仓库。请把本脚本放在项目根目录（含 .git 的目录）后运行。
    goto :wait
)

:: 3. 检查是否配置了远程 origin
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo [错误] 未配置远程 origin，无法拉取更新。请用 git remote add origin ^<仓库地址^> 添加。
    goto :wait
)

:: 4. 处理本地未提交的修改
git diff --quiet && git diff --cached --quiet
if not errorlevel 1 (
    echo [提示] 工作区干净，直接拉取更新。
) else (
    echo [提示] 检测到本地有未提交的修改。
    set /p "choice=是否先 stash 暂存本地修改再更新？(Y/n): "
    if /i "%choice%"=="n" (
        echo 已取消。请先提交或处理本地修改后再运行本脚本。
        goto :wait
    )
    echo 正在暂存本地修改...
    git stash push -m "auto-stash before update"
    set STASHED=1
)

:: 5. 拉取远程最新代码
echo.
echo [1/2] 拉取远程最新代码（git pull）...
git pull --ff-only
if errorlevel 1 (
    echo [错误] 拉取失败。常见原因：网络不可达 / 未配置 SSH 密钥或 HTTPS 凭据 / 本地提交与远程冲突。
    if defined STASHED (
        echo 正在恢复之前暂存的本地修改...
        git stash pop
    )
    goto :wait
)

:: 6. 同步 Git LFS 大文件（如 packages/index25 下的 unidic 词典 sys.dic 等）
echo.
echo [2/2] 同步 Git LFS 大文件...
git lfs version >nul 2>&1
if errorlevel 1 (
    echo [提示] 未安装 Git LFS，跳过。如需请安装 git-lfs 后手动执行 git lfs pull。
) else (
    git lfs pull
    if errorlevel 1 (
        echo [警告] LFS 大文件同步失败，代码已更新但部分大文件可能缺失，请检查网络后重试 git lfs pull。
    ) else (
        echo LFS 大文件同步完成。
    )
)

echo.
echo ============================================================
echo  更新完成！当前版本：
git log -1 --pretty=%%H %%s
echo ============================================================

if defined STASHED (
    echo.
    echo 正在恢复之前暂存的本地修改...
    git stash pop
    if errorlevel 1 (
        echo [警告] 本地修改与更新存在冲突，未自动恢复。请用 git stash show 查看，手动解决冲突。
    ) else (
        echo 本地修改已恢复。
    )
)

:wait
echo.
pause
