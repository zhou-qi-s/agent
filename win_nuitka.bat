@echo off
setlocal
chcp 65001 >nul
echo ===============================

echo  Agent 项目 Nuitka 打包脚本
echo ===============================

echo 正在清理旧的构建目录...
rmdir /s /q nuitka_build_new 2>nul

echo 正在安装/升级 Nuitka 依赖...
python -m pip install -U nuitka ordered-set zstandard httptools anyio h11 --disable-pip-version-check
if errorlevel 1 goto :fail

set CLCACHE_DISABLE=1

echo 开始 Nuitka 打包...
echo 这可能需要几分钟时间，请耐心等待...

python -m nuitka ^
  --standalone ^
  --assume-yes-for-downloads ^
  --nofollow-import-to=GPUtil ^
  --include-package=uvicorn ^
  --include-package=fastapi ^
  --include-package=redis ^
  --include-package=psutil ^
  --include-package=requests ^
  --include-package=anyio ^
  --include-package=httptools ^
  --include-package=h11 ^
  --include-data-file=config.yaml=config.yaml ^
  --output-dir=nuitka_build_new ^
  --windows-console-mode=disable ^
  --remove-output ^
  main.py

if errorlevel 1 goto :fail

echo.
echo ===============================
echo 打包成功！
echo ===============================
echo 构建目录: nuitka_build_new
echo 可执行文件: nuitka_build_new\main.dist\main.exe
echo.
echo 使用方法:
echo   1. 进入目录: cd nuitka_build_new\main.dist
echo   2. 运行程序: main.exe
echo   3. 访问 API: http://localhost:8000/docs
goto :end

:fail
echo.
echo ===============================
echo 打包失败
echo ===============================
echo 请检查错误信息

:end
endlocal
pause
