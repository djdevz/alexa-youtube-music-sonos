@echo off
REM Carrot: refresh the YouTube Music sign-in and send it to the Pi.
REM Before running: in the Carrot Chrome profile, copy a "browse" request as cURL (bash).
cd /d "%~dp0"
powershell -NoProfile -Command "Get-Clipboard -Raw | Set-Content -Encoding utf8 headers.txt"
python cli.py signin
if errorlevel 1 (
  echo.
  echo Sign-in failed - see the message above. Nothing was sent to the Pi.
  pause
  exit /b 1
)
echo.
echo Sending the new sign-in to the Pi...
scp browser.json pi@carrot.local:/home/pi/carrot/ && ssh pi@carrot.local "sudo systemctl restart carrot && echo Carrot restarted."
echo.
pause
