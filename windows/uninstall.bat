@echo off
chcp 65001 >nul
rem Fed rate monitor - remove the scheduled task (files and settings are kept).
schtasks /Delete /TN FedRateMonitor_poll /F
pause
