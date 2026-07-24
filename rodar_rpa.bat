@echo off
REM ============================================================
REM  RPA Gerar Boleto AVAPRO - execucao rapida (sem VS Code)
REM  Roda:  python main.py MOTORS   ou   python main.py IMOVEL
REM ============================================================
setlocal

set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERRO] Python da venv nao encontrado em:
    echo        %VENV_PY%
    echo Crie/ative a .venv na raiz do projeto antes de rodar.
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo            RPA GERAR BOLETO - AVAPRO
echo ============================================================
echo.
echo   [ 1 ] Rodar IMOVEL
echo   [ 2 ] Rodar MOTORS
echo   [ 0 ] Sair
echo.
echo ------------------------------------------------------------
echo   Durante a execucao:
echo     ESPACO      = pausar na proxima acao / retomar
echo     ESPACO 2x   = retomar imediatamente
echo     Ctrl+C      = encerrar suavemente (lote fica PAUSADO)
echo ------------------------------------------------------------
echo.
set "OPCAO="
set /p "OPCAO=Escolha uma opcao: "

if "%OPCAO%"=="1" set "MOD=IMOVEL" & goto rodar
if "%OPCAO%"=="2" set "MOD=MOTORS" & goto rodar
if "%OPCAO%"=="0" goto fim

echo.
echo Opcao invalida. Tente novamente.
timeout /t 2 >nul
goto menu

:rodar
echo.
echo ============================================================
echo  Iniciando RPA - modalidade: %MOD%
echo ------------------------------------------------------------
echo  ESPACO = pausar/retomar  ^|  Ctrl+C = encerrar (PAUSADO)
echo ============================================================
echo.
cd /d "%ROOT%"
"%VENV_PY%" main.py %MOD%

echo.
echo ============================================================
echo  RPA (%MOD%) finalizado. Pressione qualquer tecla para voltar ao menu.
echo ============================================================
pause >nul
goto menu

:fim
endlocal
