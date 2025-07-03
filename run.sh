#!/bin/bash

# This script sets environment variables and runs the Vocalize AI Chatbot.

# --- Configuration ---
# Set your API keys here.
# It is recommended to replace these with actual keys or use a .env file for production.
export OPENAI_API_KEY="你的OpenAI API密钥"
export SENSENOVA_ACCESS_KEY_ID="你的Sensenova Access Key ID"
export SENSENOVA_SECRET_ACCESS_KEY="你的Sensenova Secret Access Key"
# --- End Configuration ---

echo "Setting environment variables..."
echo "OPENAI_API_KEY: $OPENAI_API_KEY"
echo "SENSENOVA_ACCESS_KEY_ID: $SENSENOVA_ACCESS_KEY_ID"
echo "SENSENOVA_SECRET_ACCESS_KEY: $SENSENOVA_SECRET_ACCESS_KEY"

echo "Running Vocalize AI Chatbot..."
python3 src/chatbot.py

echo "Script finished." 