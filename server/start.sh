#!/bin/bash
# Start both Python and Node.js servers

# Start Node.js executor in background
cd /app/nodejs && npm start &

# Start Python server (foreground)
python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-5678} --log-level warning
