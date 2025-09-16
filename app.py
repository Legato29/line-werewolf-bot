# app.py － safe skeleton
import os
from flask import Flask, request, abort

# line-bot-sdk v3
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

def api_client():
    cfg = Configuration(access_token=CHANNEL_ACCESS_TOKEN or "")
    return ApiClient(cfg)

# 缺 env 只警告，不退出
@app.before_first_request
def warn_env():
    if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
        app.logger.warning("Missing CHANNEL_SECRET or CHANNEL_ACCESS_TOKEN. "
                           "Set them in Render → Environment. Webhook will fail until set.")

handler = WebhookHandler(CHANNEL_SECRET or "DUMMY_SECRET")

@app.route("/")
def root():
    return "LINE Werewolf Bot is running. POST /callback for webhook.", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

def reply_text(event, text):
    with api_client() as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    txt = (event.message.text or "").strip()
    if txt == "/help" or txt == "幫助":
        reply_text(event, "機器人已上線：輸入 /help 看到這行")
    else:
        return  # 不打擾

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
