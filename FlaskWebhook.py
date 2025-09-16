# -*- coding: utf-8 -*-
import os
from flask import Flask, request, abort
from dotenv import load_dotenv

# ---- line-bot-sdk v3 版 imports ----
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

# 讀取 .env（需包含 CHANNEL_SECRET / CHANNEL_ACCESS_TOKEN）
load_dotenv()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("請確認 .env 的 CHANNEL_SECRET / CHANNEL_ACCESS_TOKEN 設定正確")
    raise SystemExit(1)

# 建立 Flask
app = Flask(__name__)

# ---- 額外加入的檢查路由 ----
@app.route("/")
def index():
    return "LINE bot running. Try /health or set /callback as your Webhook URL.", 200

@app.route("/health")
def health():
    return "ok", 200

# ---- v3：建立 Handler 與 Messaging API 設定 ----
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# ---- Webhook 入口：給 LINE 伺服器呼叫 ----
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        # 簽章不正確，多半是 CHANNEL_SECRET 不匹配
        abort(400)

    return 'OK'

# ---- 處理文字訊息（v3：TextMessageContent）----
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    reply_text = f"你剛剛說：{user_text}\n準備好玩狼人殺了嗎？"

    # 用 ApiClient + MessagingApi 回覆
    with ApiClient(configuration) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    # 本機測試：python FlaskWebhook.py
    # 若要在雲端（Render）使用，建議改成 host="0.0.0.0"，並用 $PORT
    app.run(host="127.0.0.1", port=5000)
