@echo off
echo === Criando .venv ===
python -m venv .venv

echo === Instalando dependencias do requirements.txt ===
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt

echo === Instalando playwright e navegadores ===
.venv\Scripts\pip install playwright
.venv\Scripts\playwright install chromium

echo === Concluido! ===
pause
