@echo off
REM ==========================================================
REM Executa o Chatbot INEP/Censo Escolar (interface Streamlit)
REM Use este arquivo com duplo clique no Windows.
REM ==========================================================
cd /d "%~dp0"
echo Iniciando o chatbot... (uma aba do navegador sera aberta)
python -m streamlit run src/app.py
pause
