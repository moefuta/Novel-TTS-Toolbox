@echo off
chcp 65001 >nul
:run_tts_service
echo.
echo ============================================
echo   TTS 服务运行中, 请不要关闭此窗口!
echo   停止服务: Ctrl + C
echo   重启服务: 在本窗口按 Ctrl + R 或 F5
echo   接口地址: http://127.0.0.1:8000
echo   文档地址: http://127.0.0.1:8000/docs
echo ============================================
echo.

py tts.py