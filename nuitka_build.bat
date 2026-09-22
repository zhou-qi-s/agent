@echo off
setlocal
chcp 65001 >nul

echo 正在清理旧的构建目录...

rmdir /s /q nuitka_build_new 2>nul

echo 正在准备 Nuitka 构建环境...
python -m pip install -U nuitka ordered-set zstandard --disable-pip-version-check
if errorlevel 1 goto :fail

set CLCACHE_DISABLE=1

echo 开始 Nuitka 打包...
python -m nuitka ^
  --standalone ^
  --assume-yes-for-downloads ^
  --nofollow-import-to=GPUtil ^
  --include-data-file=config.yaml=config.yaml ^
  --output-dir=nuitka_build_new ^
  --remove-output ^
  main.py

if errorlevel 1 goto :fail

echo.
echo 打包成功！
echo 构建目录: nuitka_build_new
echo 可执行文件: nuitka_build_new\main.dist\main.exe
goto :end

:fail
echo.
echo 打包失败，请检查错误信息

:end
endlocal
pause
