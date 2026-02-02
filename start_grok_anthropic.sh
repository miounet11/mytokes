#!/bin/bash

# Grok to Anthropic Proxy Service Startup Script
# Port: 8300

cd /www/wwwroot/ai-history-manager

# Kill existing process
echo "🔄 Stopping existing Grok-Anthropic proxy..."
pkill -9 -f "uvicorn api_server_grok_anthropic:app" 2>/dev/null
sleep 1

# Create log file if not exists
touch /var/log/grok-anthropic-proxy.log

# Start service
echo "🚀 Starting Grok-Anthropic proxy on port 8300..."
nohup uvicorn api_server_grok_anthropic:app \
    --host 0.0.0.0 \
    --port 8300 \
    --loop uvloop \
    --http httptools \
    >> /var/log/grok-anthropic-proxy.log 2>&1 &

sleep 2

# Check if started
if pgrep -f "uvicorn api_server_grok_anthropic:app" > /dev/null; then
    echo "✅ Grok-Anthropic proxy started successfully!"
    echo "   Health check: curl http://127.0.0.1:8300/"
    echo "   Logs: tail -f /var/log/grok-anthropic-proxy.log"
else
    echo "❌ Failed to start. Check logs:"
    tail -20 /var/log/grok-anthropic-proxy.log
fi
