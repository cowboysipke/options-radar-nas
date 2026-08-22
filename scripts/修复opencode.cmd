@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0fix-opencode.ps1"