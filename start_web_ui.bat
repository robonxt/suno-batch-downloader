@echo off
echo Starting Suno Batch Recovery Web UI...
start http://localhost:8080
python suno_batch_recovery.py --web
pause
