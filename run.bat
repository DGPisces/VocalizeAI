@echo off

REM Vocalize AI Chatbot 启动脚本
REM 支持环境变量和脚本配置两种方式

echo Vocalize AI Chatbot 启动脚本
echo 请选择配置方式:
echo 1. 使用 .env 文件或系统环境变量
echo 2. 使用脚本内置配置
set /p choice="请输入选择 (1/2): "

if "%choice%"=="1" (
    echo 使用环境变量配置...
    if exist ".env" (
        echo 检测到 .env 文件
    ) else (
        echo 未找到 .env 文件，将使用系统环境变量
    )
    goto run
)

if "%choice%"=="2" (
    echo 使用脚本配置...
    REM --- 脚本配置 ---
    REM 请在下方设置您的 API 密钥
    set OPENAI_API_KEY=你的OpenAI API密钥
    set GOOGLE_API_KEY=你的Google API密钥
    set OPENAI_BASE_URL=https://api.sensenova.cn/compatible-mode/v1/
    set OPENAI_MODEL=DeepSeek-V3
    set GOOGLE_MODEL_ID=gemini-2.5-flash-preview-tts
    REM --- 配置结束 ---
    
    echo 当前配置:
    echo OPENAI_API_KEY: %OPENAI_API_KEY%
    echo GOOGLE_API_KEY: %GOOGLE_API_KEY%
    echo OPENAI_BASE_URL: %OPENAI_BASE_URL%
    echo OPENAI_MODEL: %OPENAI_MODEL%
    echo GOOGLE_MODEL_ID: %GOOGLE_MODEL_ID%
    goto run
)

echo 无效选择，退出
pause
exit /b 1

:run
echo 启动 Vocalize AI Chatbot...
python3 -m src.chatbot

echo 程序结束
pause 