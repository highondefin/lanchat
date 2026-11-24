@echo off
rem Build dist\LanChat.exe on Windows. Needs Python 3.8+ from python.org.
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller==5.13.2 || goto :error
py -m PyInstaller --noconfirm --clean --onefile --windowed --name LanChat ^
    --icon packaging\lanchat.ico --add-data "lanchat\icon.png;lanchat" --add-data "lanchat\icon.ico;lanchat" ^
    --paths . packaging\run_lanchat.py || goto :error
echo.
echo Built dist\LanChat.exe
goto :eof
:error
echo Build failed.
exit /b 1
