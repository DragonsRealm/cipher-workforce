#!/bin/bash
# Start both Python and Node.js servers

# Start Node.js executor in background
cd /app/nodejs && npm start &

# Start Python server (foreground)
python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:?not set - defaults live in .env.template} --log-level warning
