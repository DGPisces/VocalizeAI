@echo off

REM This script sets environment variables and runs the Vocalize AI Chatbot.

REM --- Configuration ---
REM Set your API keys here.
REM It is recommended to replace these with actual keys or use a .env file for production.
set OPENAI_API_KEY="你的OpenAI API密钥"
set SENSENOVA_ACCESS_KEY_ID="你的Sensenova Access Key ID"
set SENSENOVA_SECRET_ACCESS_KEY="你的Sensenova Secret Access Key"
set OPENAI_BASE_URL="https://api.sensenova.cn/compatible-mode/v1/" REM 你的OpenAI API基础URL
set OPENAI_MODEL="DeepSeek-V3" REM 你使用的模型名称
REM --- End Configuration ---

echo Setting environment variables...
echo OPENAI_API_KEY: %OPENAI_API_KEY%
echo SENSENOVA_ACCESS_KEY_ID: %SENSENOVA_ACCESS_KEY_ID%
echo SENSENOVA_SECRET_ACCESS_KEY: %SENSENOVA_SECRET_ACCESS_KEY%
echo OPENAI_BASE_URL: %OPENAI_BASE_URL%
echo OPENAI_MODEL: %OPENAI_MODEL%

echo Running Vocalize AI Chatbot...
python -m src.chatbot

echo Script finished.
pause 