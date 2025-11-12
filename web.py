# web.py
import os
import subprocess
from flask import Flask, jsonify

app = Flask(__name__)

# Default command to run bot — same as repo's start.sh
BOT_CMD = os.environ.get("BOT_CMD", "bash start.sh")

bot_proc = None

@app.before_first_request
def start_bot_process():
    global bot_proc
    if bot_proc is None:
        try:
            parts = BOT_CMD.strip().split()
            # Don't pipe output — let Render show bot logs
            bot_proc = subprocess.Popen(parts)
            app.logger.info(f"✅ Started bot subprocess (pid={bot_proc.pid}) using: {BOT_CMD}")
        except Exception as e:
            app.logger.error(f"❌ Failed to start bot subprocess: {e}")

@app.route("/")
def index():
    return "✅ Bot is running on Render", 200

@app.route("/health")
def health():
    alive = bot_proc is not None and bot_proc.poll() is None
    return jsonify({
        "bot_running": alive,
        "bot_pid": bot_proc.pid if bot_proc else None
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
