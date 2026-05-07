import os
from flask import Flask, jsonify
from datetime import datetime, timezone

app = Flask(__name__)

SERVICE_NAME = os.environ.get("SERVICE_NAME", "smart-trucks")

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": SERVICE_NAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
