# app.py － smoke test
from flask import Flask

app = Flask(__name__)

@app.route("/")
def health():
    return "OK: Flask & Gunicorn are running."

if __name__ == "__main__":
    # 本機測試才會用到；Render 用 gunicorn 不會跑到這段
    app.run(host="0.0.0.0", port=5000)
