# -*- coding: utf-8 -*-
"""
LINE Werewolf Bot (Flask + line-bot-sdk v3)
- 群組指令：/create /join /leave /start /status /vote N /endday /reset /help
- 夜晚私訊（身份指令）：
    狼人：/kill N
    先知：/check N
    （可擴充女巫 /save N、/poison N 與獵人被放逐開槍等）
- 流程：Lobby -> Night -> Day -> （循環）
- 儲存：記憶體（部署時可改 Redis/DB）
"""

import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from flask import Flask, request, abort
from dotenv import load_dotenv

# ---- line-bot-sdk v3 ----
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent
)
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

# --------------------------------------------------------------------
# 環境與 Flask
# --------------------------------------------------------------------
load_dotenv()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("請在 Render 或本機 .env 設定 CHANNEL_SECRET / CHANNEL_ACCESS_TOKEN")
    raise SystemExit(1)

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# --------------------------------------------------------------------
# 遊戲資料結構（記憶體示範；正式建議放 DB/Redis）
# --------------------------------------------------------------------
# 6 人基礎套餐：狼人x2、先知、女巫、獵人、村民
ROLES_6 = ["狼人", "狼人", "先知", "女巫", "獵人", "村民"]
MIN_PLAYERS = 6

@dataclass
class Player:
    user_id: str
    display_name: str = ""
    seat: Optional[int] = None
    alive: bool = True
    role: Optional[str] = None
    vote: Optional[int] = None        # 白天投誰
    last_night_target: Optional[int] = None  # 夜晚選擇（如狼殺/查驗）

@dataclass
class GameRoom:
    room_id: str                              # groupId 或 roomId
    players: Dict[str, Player] = field(default_factory=dict)   # user_id -> Player
    seats: List[str] = field(default_factory=list)             # seat -> user_id
    started: bool = False
    day: int = 0
    phase: str = "lobby"                      # lobby | night | day
    votes: Dict[int, int] = field(default_factory=dict)        # seat -> 票數
    night_wolf_votes: Dict[int, int] = field(default_factory=dict)  # seat -> 狼票
    seer_check: Optional[int] = None          # 先知查驗目標座位
    dead_tonight: Set[int] = field(default_factory=set)        # 夜晚死亡（可擴充女巫救/毒後重計）
    revealed_today: Optional[str] = None      # 當天公布的結算資訊（示範）

    def reset_day_votes(self):
        self.votes.clear()
        for p in self.players.values():
            p.vote = None

    def reset_night_actions(self):
        self.night_wolf_votes.clear()
        self.seer_check = None
        self.dead_tonight.clear()
        for p in self.players.values():
            p.last_night_target = None

# 全域：room_id -> GameRoom
ROOMS: Dict[str, GameRoom] = {}

# --------------------------------------------------------------------
# 共用小工具
# --------------------------------------------------------------------
def with_api():
    """Context manager 產生 MessagingApi。"""
    return ApiClient(configuration)

def reply_text(event, text: str):
    with with_api() as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def push_text(to_id: str, text: str):
    with with_api() as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=to_id,
                messages=[TextMessage(text=text)]
            )
        )

def get_room_id(event) -> Optional[str]:
    src = event.source
    # 群組或多人聊天室才有 room_id / group_id
    return getattr(src, "group_id", None) or getattr(src, "room_id", None)

def get_user_id(event) -> str:
    return event.source.user_id

def get_display_name(group_id: Optional[str], user_id: str) -> str:
    # 嘗試取群組暱稱，若失敗退回一般 Profile
    try:
        with with_api() as api_client:
            api = MessagingApi(api_client)
            if group_id:
                prof = api.get_group_member_profile(group_id, user_id)
            else:
                prof = api.get_profile(user_id)
            return prof.display_name
    except Exception:
        return "玩家"

def room_or_error(event) -> Optional[GameRoom]:
    rid = get_room_id(event)
    if not rid:
        reply_text(event, "請把機器人拉進群組使用（本機器人以群組為房間單位）。")
        return None
    return ROOMS.get(rid)

def seat_str(p: Player) -> str:
    return f"{p.seat}" if p.seat is not None else "?"

def list_alive(room: GameRoom) -> List[Player]:
    return [p for p in room.players.values() if p.alive and p.seat is not None]

def seat_to_uid(room: GameRoom, seat: int) -> Optional[str]:
    if 1 <= seat <= len(room.seats):
        return room.seats[seat - 1]
    return None

# --------------------------------------------------------------------
# 指令：大廳
# --------------------------------------------------------------------
def cmd_help(event):
    reply_text(event,
        "📝 指令列表：\n"
        "群組：\n"
        "  /create 建房\n"
        "  /join 加入、/leave 離開\n"
        "  /start 開始（6人起）\n"
        "  /status 狀態\n"
        "  /vote N 投票、/endday 強制結算投票\n"
        "  /reset 重置\n"
        "夜晚（私訊身分指令）：\n"
        "  狼人：/kill N\n"
        "  先知：/check N\n"
        "（可擴充：女巫 /save N、/poison N）"
    )

def cmd_create(event):
    rid = get_room_id(event)
    if not rid:
        reply_text(event, "請把機器人拉進群組後再 /create 建房。")
        return
    if rid in ROOMS:
        reply_text(event, "此群已有房間；如需重來請用 /reset。")
        return
    ROOMS[rid] = GameRoom(room_id=rid)
    reply_text(event, "🟢 房間已建立！玩家輸入 /join 加入，滿 6 人可 /start 開始。")

def cmd_join(event):
    rid = get_room_id(event)
    if not rid or rid not in ROOMS:
        reply_text(event, "尚未建房，請先 /create。")
        return
    room = ROOMS[rid]
    if room.started:
        reply_text(event, "遊戲已開始，無法加入。")
        return
    uid = get_user_id(event)
    if uid in room.players:
        reply_text(event, "你已在房內。")
        return
    name = get_display_name(rid, uid)
    seat = len(room.seats) + 1
    room.players[uid] = Player(user_id=uid, display_name=name, seat=seat)
    room.seats.append(uid)
    reply_text(event, f"✅ {name} 加入，座位：{seat}\n目前人數：{len(room.players)}")

def cmd_leave(event):
    room = room_or_error(event)
    if not room: return
    if room.started:
        reply_text(event, "遊戲已開始，不能離開。")
        return
    uid = get_user_id(event)
    if uid not in room.players:
        reply_text(event, "你不在房內。")
        return
    # 重新整理座位
    leaving_seat = room.players[uid].seat
    del room.players[uid]
    room.seats = [u for u in room.seats if u != uid]
    # Re-seating
    for i, u in enumerate(room.seats, start=1):
        room.players[u].seat = i
    reply_text(event, f"🚪 已離開。座位已重排（原座位 {leaving_seat} 釋出）。")

def cmd_status(event):
    room = room_or_error(event)
    if not room: return
    lines = [
        f"📋 狼人殺狀態｜Day {room.day}｜Phase: {room.phase}",
        f"玩家數：{len(room.players)}（生存 {len(list_alive(room))}）",
    ]
    for uid in room.seats:
        p = room.players[uid]
        lines.append(f"{p.seat}. {p.display_name} {'(生)' if p.alive else '(亡)'}")
    if room.revealed_today:
        lines.append(f"今日公告：{room.revealed_today}")
    reply_text(event, "\n".join(lines))

def cmd_reset(event):
    rid = get_room_id(event)
    if rid in ROOMS:
        del ROOMS[rid]
        reply_text(event, "🔁 已重置本群遊戲。")
    else:
        reply_text(event, "尚未建房，無需重置。")

# --------------------------------------------------------------------
# 遊戲開始與發牌
# --------------------------------------------------------------------
def cmd_start(event):
    room = room_or_error(event)
    if not room: return
    if room.started:
        reply_text(event, "遊戲已開始。")
        return
    if len(room.players) < MIN_PLAYERS:
        reply_text(event, f"人數不足（{len(room.players)}/{MIN_PLAYERS}）。")
        return

    # 發牌（以 6 人套餐為例；可依人數擴充）
    roles = ROLES_6[:]
    random.shuffle(roles)
    for uid in room.seats[:MIN_PLAYERS]:
        p = room.players[uid]
        p.role = roles[p.seat - 1]
        try:
            push_text(uid, f"🎭 你的身分：{p.role}\n座位：{p.seat}\n夜晚請注意私訊指令提示。")
        except Exception:
            pass

    room.started = True
    room.day = 0
    start_night(room, announce_event=event)

def start_night(room: GameRoom, announce_event=None):
    room.phase = "night"
    room.reset_night_actions()
    if announce_event:
        reply_text(announce_event, "🌙 夜幕降臨…\n狼人請私訊輸入 /kill 座位號；先知請私訊輸入 /check 座位號。")
    # 私訊提示對應角色
    for uid in room.seats:
        p = room.players[uid]
        if not p.alive or not p.role:
            continue
        if p.role == "狼人":
            push_text(uid, "【夜晚】你是狼人。請輸入：/kill 座位號（例如 /kill 3）")
        elif p.role == "先知":
            push_text(uid, "【夜晚】你是先知。請輸入：/check 座位號（例如 /check 2）")
        elif p.role == "女巫":
            push_text(uid, "【夜晚】你是女巫（Demo 未開啟藥水，未來可加 /save N、/poison N）")
        elif p.role == "獵人":
            push_text(uid, "【夜晚】你是獵人（Demo：被放逐時公開角色即可，未實作開槍）")

def resolve_night_and_start_day(room: GameRoom, announce_event=None):
    # 狼人票決：最高票為被殺目標；平票則無人死亡（可改規則）
    if room.night_wolf_votes:
        max_cnt = max(room.night_wolf_votes.values())
        targets = [s for s,c in room.night_wolf_votes.items() if c == max_cnt]
        if len(targets) == 1:
            room.dead_tonight.add(targets[0])

    # 先知查驗公告（Demo 公開；正式版通常私訊先知即可）
    seer_note = None
    if room.seer_check:
        uid = seat_to_uid(room, room.seer_check)
        role = room.players[uid].role if uid else "未知"
        seer_note = f"先知查驗：{room.seer_check} 號是「{role}」"

    # 結算死亡
    death_msg = "今晚平安夜。" if not room.dead_tonight else ""
    for seat in room.dead_tonight:
        uid = seat_to_uid(room, seat)
        if uid and room.players[uid].alive:
            room.players[uid].alive = False
            death_msg += f"\n{seat} 號（{room.players[uid].display_name}）遇害。"

    room.day += 1
    room.phase = "day"
    room.reset_day_votes()
    room.revealed_today = (death_msg.strip() if death_msg else None)
    msg = f"☀️ 天亮了（Day {room.day}）！\n"
    if room.revealed_today:
        msg += f"{room.revealed_today}\n"
    if seer_note:
        msg += f"{seer_note}\n"
    msg += "請發言後輸入 /vote 座位號 進行投票（例：/vote 3）。主持人可用 /endday 強制結算。"
    if announce_event:
        reply_text(announce_event, msg)

# --------------------------------------------------------------------
# 夜晚：私訊身份行動
# --------------------------------------------------------------------
def pm_kill(user_id: str, text: str):
    # 找到玩家所在房（按 user_id 搜）
    room = None
    for rp in ROOMS.values():
        if user_id in rp.players:
            room = rp
            break
    if not room or room.phase != "night":
        push_text(user_id, "現在不是夜晚，或你不在任何房間。")
        return
    p = room.players[user_id]
    if not (p.alive and p.role == "狼人"):
        push_text(user_id, "你不是狼人或你已出局。")
        return
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        push_text(user_id, "用法：/kill 座位號（例如 /kill 3）")
        return
    target = int(parts[1])
    tgt_uid = seat_to_uid(room, target)
    if not tgt_uid or not room.players[tgt_uid].alive:
        push_text(user_id, "無效座位或該座位已死亡。")
        return
    # 記錄狼人票
    p.last_night_target = target
    room.night_wolf_votes[target] = room.night_wolf_votes.get(target, 0) + 1
    push_text(user_id, f"已提交夜殺票：{target} 號")

def pm_check(user_id: str, text: str):
    room = None
    for rp in ROOMS.values():
        if user_id in rp.players:
            room = rp
            break
    if not room or room.phase != "night":
        push_text(user_id, "現在不是夜晚，或你不在任何房間。")
        return
    p = room.players[user_id]
    if not (p.alive and p.role == "先知"):
        push_text(user_id, "你不是先知或你已出局。")
        return
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        push_text(user_id, "用法：/check 座位號（例如 /check 2）")
        return
    target = int(parts[1])
    tgt_uid = seat_to_uid(room, target)
    if not tgt_uid or not room.players[tgt_uid].alive:
        push_text(user_id, "無效座位或該座位已死亡。")
        return
    p.last_night_target = target
    room.seer_check = target
    push_text(user_id, f"已提交查驗：{target} 號")

# --------------------------------------------------------------------
# 白天：群組投票與結算
# --------------------------------------------------------------------
def cmd_vote(event, arg: str):
    room = room_or_error(event)
    if not room: return
    if room.phase != "day":
        reply_text(event, "現在不是白天。")
        return
    try:
        target = int(arg)
    except:
        reply_text(event, "用法：/vote 座位號（例如 /vote 3）")
        return

    uid = get_user_id(event)
    if uid not in room.players:
        reply_text(event, "你不在本局中。")
        return
    voter = room.players[uid]
    if not voter.alive:
        reply_text(event, "你已死亡，不能投票。")
        return
    tgt_uid = seat_to_uid(room, target)
    if not tgt_uid or not room.players[tgt_uid].alive:
        reply_text(event, "無效座位或該座位已死亡。")
        return

    # 取消舊票
    if voter.vote is not None and voter.vote in room.votes:
        room.votes[voter.vote] = max(0, room.votes[voter.vote] - 1)
    voter.vote = target
    room.votes[target] = room.votes.get(target, 0) + 1
    reply_text(event, f"🗳 已投：{target} 號")

def cmd_endday(event):
    room = room_or_error(event)
    if not room: return
    if room.phase != "day":
        reply_text(event, "現在不是白天。")
        return
    alive = list_alive(room)
    if not alive:
        reply_text(event, "場上無存活玩家。")
        return

    if room.votes:
        max_cnt = max(room.votes.values())
        top = [s for s,c in room.votes.items() if c == max_cnt]
    else:
        max_cnt = 0
        top = []

    if len(top) != 1:
        reply_text(event, f"📣 投票結束：平票（最高票 {max_cnt} 票）→ 無人出局。\n即將進入夜晚…")
        start_night(room, announce_event=event)
        return

    out_seat = top[0]
    out_uid = seat_to_uid(room, out_seat)
    if out_uid and room.players[out_uid].alive:
        room.players[out_uid].alive = False
        name = room.players[out_uid].display_name
        role = room.players[out_uid].role or "未知"
        reply_text(event, f"📣 投票結束：{out_seat} 號（{name}）被放逐！\n其身分（Demo 公開）：{role}\n即將進入夜晚…")
    else:
        reply_text(event, "📣 投票結束：目標已死亡或不存在 → 無效投票。\n即將進入夜晚…")

    start_night(room, announce_event=event)

# --------------------------------------------------------------------
# LINE 路由
# --------------------------------------------------------------------
@app.route("/")
def index():
    return "LINE Werewolf Bot running. Try /health or set /callback for webhook.", 200

@app.route("/health")
def health():
    return "ok", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(JoinEvent)
def on_join(event):
    reply_text(event,
        "👋 我是狼人殺助理。\n"
        "在群組輸入 /create 建房；/join 加入；滿 6 人 /start 發牌。\n"
        "夜晚會用私訊通知身分與指令；白天用 /vote N 投票。\n"
        "更多指令：/help"
    )

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    text = (event.message.text or "").strip()
    lower = text.lower()

    # 1) 私訊的夜晚角色行動
    # 注意：LINE 私訊中 get_room_id(event) 會是 None，因此要靠 user_id 找房
    if lower.startswith("/kill"):
        pm_kill(get_user_id(event), text)
        return
    if lower.startswith("/check"):
        pm_check(get_user_id(event), text)
        return

    # 2) 群組指令
    if lower == "/help":
        cmd_help(event); return
    if lower == "/create":
        cmd_create(event); return
    if lower == "/join":
        cmd_join(event); return
    if lower == "/leave":
        cmd_leave(event); return
    if lower == "/start":
        cmd_start(event); return
    if lower == "/status":
        cmd_status(event); return
    if lower.startswith("/vote"):
        parts = text.split()
        if len(parts) == 2:
            cmd_vote(event, parts[1]); return
        reply_text(event, "用法：/vote 座位號（例如 /vote 3）"); return
    if lower == "/endday":
        cmd_endday(event); return
    if lower == "/reset":
        cmd_reset(event); return

    # 3) 特殊控制：當夜晚行動都提交後，由主持人或任意人輸入「天亮了」觸發結算
    if lower in ("天亮了", "天亮", "day"):
        room = room_or_error(event)
        if room and room.phase == "night":
            resolve_night_and_start_day(room, announce_event=event)
        else:
            reply_text(event, "現在不是夜晚或尚未開始。")
        return

    # 4) 其他文字：提示
    cmd_help(event)

# --------------------------------------------------------------------
# 啟動（Render 友善）
# --------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    # Render 需 0.0.0.0；本機也可
    app.run(host="0.0.0.0", port=port)
