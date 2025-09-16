# -*- coding: utf-8 -*-
import os, random
from collections import Counter
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort
from dotenv import load_dotenv

# ===== line-bot-sdk v3 =====
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

# ===== Scheduler =====
from apscheduler.schedulers.background import BackgroundScheduler

# ================== 基本設定 ==================
load_dotenv()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
NIGHT_MINUTES = int(os.getenv("NIGHT_MINUTES", "6"))
DAY_MINUTES = int(os.getenv("DAY_MINUTES", "8"))

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise SystemExit("請先在 .env 設定 CHANNEL_SECRET / CHANNEL_ACCESS_TOKEN")

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

scheduler = BackgroundScheduler()
scheduler.start()

def with_api():
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
            PushMessageRequest(to=to_id, messages=[TextMessage(text=text)])
        )

def get_room_id(event):
    s = event.source
    return getattr(s, "group_id", None) or getattr(s, "room_id", None) or s.user_id

def get_user_id(event):
    return event.source.user_id

def get_display_name(room_id: str | None, user_id: str) -> str:
    try:
        with with_api() as api_client:
            api = MessagingApi(api_client)
            if room_id and room_id != user_id:
                prof = api.get_group_member_profile(room_id, user_id)
            else:
                prof = api.get_profile(user_id)
            return prof.display_name
    except Exception:
        return "玩家"

def now_utc():
    return datetime.now(timezone.utc)

# ================== 規則與資料 ==================
MIN_P, MAX_P = 5, 8
WOLF_COUNT_BY_N = {5: 1, 6: 2, 7: 2, 8: 2}

ROLE_DESCRIPTIONS = {
    "狼人": "夜晚可商議並擊殺一名玩家（『私訊』：擊殺 名字）。",
    "村民": "無主動技能，靠發言與投票。",
    "預言家": "夜晚可查驗一名玩家是否為狼人（『私訊』：查驗 名字，每晚一次）。",
    "醫生": "夜晚可救一名玩家（『私訊』：救 名字，每晚一次；自救全局僅一次；不得連續兩晚救同一人）。",
    "女巫": "擁有解藥與毒藥各一次（『私訊』：解救／投毒 名字）。「解救」只能救當晚狼刀對象，且**不得自救**。",
    "獵人": "被淘汰後可『私訊』：開槍 名字（帶走一人，一次）。",
}

class Player:
    def __init__(self, uid: str, name: str):
        self.user_id = uid
        self.name = name
        self.role: str | None = None
        self.alive: bool = True

class GameRoom:
    def __init__(self, room_id: str, host_id: str):
        self.room_id = room_id
        self.host_id = host_id
        self.players: dict[str, Player] = {}
        self.started: bool = False
        # waiting → config（模板與換角）→ night → day
        self.phase: str = "waiting"

        # 角色模板與調整後結果
        self.base_roles: list[str] = []
        self.current_roles: list[str] = []

        # 白天投票
        self.votes: dict[str, str] = {}

        # 夜晚：狼人刀票（收集投票，天亮時表決）
        self.wolf_targets: list[str] = []

        # 夜晚行動狀態（晚上結算後會重置「每晚」欄位）
        self.night_flags = {
            # 預言家
            "seer_done_uids": set(),            # 已查驗的預言家（每晚）
            # 醫生
            "doctor_saved_uid": None,           # 醫生本晚救的人（每晚）
            "doctor_selfheal_used": set(),      # 醫生自救已用（全局）
            "doctor_last_saved_uid": None,      # 上一晚醫生救的人（跨晚）
            # 女巫
            "witch_heal_left": True,            # 女巫解藥剩餘（全局）
            "witch_poison_left": True,          # 女巫毒藥剩餘（全局）
            "witch_save_flag": False,           # 女巫本晚是否使用解藥（每晚）
            "witch_poison_uid": None,           # 女巫本晚毒的人（每晚）
            "witch_uid": None,                  # 本局女巫的 user_id（指派後填）
        }

        # 獵人待開槍
        self.hunter_pending_uid: str | None = None

        # 自動結算
        self.deadline_at = None
        self.n_job_id = None    # 夜晚 job id
        self.d_job_id = None    # 白天 job id

    def alive_players(self):
        return [p for p in self.players.values() if p.alive]

ROOMS: dict[str, GameRoom] = {}

# ================== 模板與換角 ==================
def build_base_roles(n: int) -> list[str]:
    """
    5人：1 狼、1 預言家、1 醫生、2 村民
    6人：2 狼、1 預言家、1 醫生、2 村民
    7人：2 狼、1 預言家、1 醫生、3 村民
    8人：2 狼、1 預言家、1 醫生、4 村民
    """
    wolves = WOLF_COUNT_BY_N.get(n, max(1, n // 4))
    roles = ["狼人"] * wolves + ["預言家", "醫生"]
    while len(roles) < n:
        roles.append("村民")
    return roles

def pretty_roles(roles: list[str]) -> str:
    c = Counter(roles)
    order = ["狼人", "預言家", "醫生", "女巫", "獵人", "村民"]
    parts = []
    for r in order:
        if c[r]:
            parts.append(f"{r}×{c[r]}")
    for r, v in c.items():
        if r not in order:
            parts.append(f"{r}×{v}")
    return "、".join(parts) if parts else "（空）"

def swap_doctor_to_witch(roles: list[str]) -> tuple[bool, str]:
    if "女巫" in roles:
        return False, "已有『女巫』，無法再換。"
    if "醫生" not in roles:
        return False, "模板中沒有『醫生』可供替換。"
    idx = roles.index("醫生")
    roles[idx] = "女巫"
    return True, "已將『醫生』替換為『女巫』。"

def swap_villager_to_hunter(roles: list[str]) -> tuple[bool, str]:
    if "獵人" in roles:
        return False, "已有『獵人』，無法再換。"
    if "村民" not in roles:
        return False, "模板中沒有『村民』可供替換。"
    idx = roles.index("村民")
    roles[idx] = "獵人"
    return True, "已將一名『村民』替換為『獵人』。"

# ================== 共用邏輯 ==================
def role_intro_text() -> str:
    lines = ["📚 角色清單（名稱｜能力）"]
    for k, v in ROLE_DESCRIPTIONS.items():
        lines.append(f"{v}")
    return "\n".join(lines)

def assign_and_notify(room: GameRoom, roles: list[str]):
    uids = list(room.players.keys())
    random.shuffle(uids)
    random.shuffle(roles)
    for uid, r in zip(uids, roles):
        room.players[uid].role = r
        if r == "女巫":
            room.night_flags["witch_uid"] = uid

    wolves = [p for p in room.players.values() if p.role == "狼人"]
    wolf_names = [w.name for w in wolves]
    for p in room.players.values():
        msg = f"你的身份是：{p.role}"
        if p.role == "狼人":
            mates = [n for n in wolf_names if n != p.name]
            msg += "\n你的同伴：" + ("、".join(mates) if mates else "（無）")
        push_text(p.user_id, msg)

def check_game_end(room: GameRoom, announce_event=None) -> bool:
    alive = room.alive_players()
    wolves = [p for p in alive if p.role == "狼人"]
    good = [p for p in alive if p.role != "狼人"]

    if not wolves:
        msg = "🎉 遊戲結束：好人獲勝！"
    elif len(wolves) >= len(good):
        msg = "💀 遊戲結束：狼人獲勝！"
    else:
        return False

    if announce_event:
        reply_text(announce_event, msg)
    else:
        push_text(room.room_id, msg)

    clear_schedules(room)
    ROOMS.pop(room.room_id, None)
    return True

def ensure_in_room(uid: str) -> GameRoom | None:
    for r in ROOMS.values():
        if uid in r.players:
            return r
    return None

# ================== 自動結算：排程 ==================
def schedule_night_timeout(room: GameRoom, minutes=None):
    minutes = minutes or NIGHT_MINUTES
    if room.n_job_id:
        try: scheduler.remove_job(room.n_job_id)
        except: pass
    room.deadline_at = now_utc() + timedelta(minutes=minutes)
    job = scheduler.add_job(
        func=night_timeout_job,
        trigger='date',
        run_date=room.deadline_at,
        args=[room.room_id],
        id=f"night-{room.room_id}",
        replace_existing=True
    )
    room.n_job_id = job.id
    push_text(room.room_id, f"🌙 夜晚開始（{minutes} 分鐘）。到時自動結算。")

def schedule_day_timeout(room: GameRoom, minutes=None):
    minutes = minutes or DAY_MINUTES
    if room.d_job_id:
        try: scheduler.remove_job(room.d_job_id)
        except: pass
    room.deadline_at = now_utc() + timedelta(minutes=minutes)
    job = scheduler.add_job(
        func=day_timeout_job,
        trigger='date',
        run_date=room.deadline_at,
        args=[room.room_id],
        id=f"day-{room.room_id}",
        replace_existing=True
    )
    room.d_job_id = job.id
    push_text(room.room_id, f"🌞 白天開始（{minutes} 分鐘）。到時自動結算。")

def clear_schedules(room: GameRoom):
    for jid in (room.n_job_id, room.d_job_id):
        if jid:
            try: scheduler.remove_job(jid)
            except: pass
    room.n_job_id = room.d_job_id = None
    room.deadline_at = None

def night_timeout_job(room_id: str):
    room = ROOMS.get(room_id)
    if not room or room.phase != "night":
        return
    resolve_night_and_start_day(room, event=None)
    if room and room.phase == "day":
        schedule_day_timeout(room)

def day_timeout_job(room_id: str):
    room = ROOMS.get(room_id)
    if not room or room.phase != "day":
        return
    # 自動白天結算
    auto_endday(room)
    if room and room.phase == "night":
        schedule_night_timeout(room)

def extend_current_phase(room: GameRoom, add_minutes: int):
    if room.phase == "night":
        schedule_night_timeout(room, minutes=add_minutes)
    elif room.phase == "day":
        schedule_day_timeout(room, minutes=add_minutes)

def force_settle(room: GameRoom):
    if room.phase == "night":
        resolve_night_and_start_day(room, event=None)
        if room and room.phase == "day":
            schedule_day_timeout(room)
    elif room.phase == "day":
        auto_endday(room)
        if room and room.phase == "night":
            schedule_night_timeout(room)

# ================== 指令（中文） ==================
def cmd_help(event):
    reply_text(event,
        "📜 指令列表（中文）\n"
        f"・建房／加入／狀態／角色清單／重置\n"
        "・開始 → 產生『預設模板』 → 房主可『換 女巫 / 換 獵人』 → 『確認角色』發牌\n"
        "・夜晚（請私訊機器人）：\n"
        "   狼人：擊殺 名字\n"
        "   預言家：查驗 名字（每晚一次）\n"
        "   醫生：救 名字（每晚一次；自救全局一次；不得連續兩晚救同一人）\n"
        "   女巫：解救（只能救當晚刀口、且不得自救；一次）／投毒 名字（一次）\n"
        "・白天：投票 名字 → 結算（放逐最高票）\n"
        "・自動結算：夜/日各有倒數，到時自動結算；房主可輸入『延長 分鐘數』或『立即結算』"
    )

def cmd_rolelist(event):
    reply_text(event, role_intro_text())

def cmd_build(event):
    rid, uid = get_room_id(event), get_user_id(event)
    if rid in ROOMS:
        reply_text(event, "本群已有房間，如需重來請先「重置」。")
        return
    ROOMS[rid] = GameRoom(room_id=rid, host_id=uid)
    reply_text(event,
        "✅ 房間已建立！支援 5～8 人。\n"
        "玩家輸入「加入」報名；人數達標後房主輸入「開始」。\n"
        "開始後會產生預設模板，房主可：『換 女巫』（醫生→女巫）、『換 獵人』（村民→獵人），再『確認角色』發牌。"
    )

def cmd_join(event):
    rid = get_room_id(event)
    if rid not in ROOMS:
        reply_text(event, "尚未建房，請先「建房」。")
        return
    room = ROOMS[rid]
    if room.started:
        reply_text(event, "遊戲已開始，無法加入。")
        return
    uid, name = get_user_id(event), get_display_name(rid, get_user_id(event))
    if uid in room.players:
        reply_text(event, f"{name} 已在房內。")
        return
    if len(room.players) >= MAX_P:
        reply_text(event, f"人數已滿（{MAX_P}）。")
        return
    room.players[uid] = Player(uid, name)
    reply_text(event, f"🙋 {name} 加入！目前人數：{len(room.players)}")

def cmd_start(event):
    rid, uid = get_room_id(event), get_user_id(event)
    if rid not in ROOMS:
        reply_text(event, "尚未建房。")
        return
    room = ROOMS[rid]
    if uid != room.host_id:
        reply_text(event, "只有建房者可「開始」。")
        return
    n = len(room.players)
    if not (MIN_P <= n <= MAX_P):
        reply_text(event, f"目前人數 {n}，需 {MIN_P}～{MAX_P} 人。")
        return
    if room.started:
        reply_text(event, "遊戲已開始。")
        return

    room.base_roles = build_base_roles(n)
    room.current_roles = room.base_roles.copy()
    room.phase = "config"
    wolves = WOLF_COUNT_BY_N.get(n, max(1, n // 4))
    reply_text(event,
        "🔧 已產生預設模板（可換角）：\n"
        f"・建議狼人數：{wolves}\n"
        f"・目前角色：{pretty_roles(room.current_roles)}\n"
        "可用：『換 女巫』（醫生→女巫）、『換 獵人』（村民→獵人）、『確認角色』"
    )

def cmd_swap(event, target: str):
    rid, uid = get_room_id(event), get_user_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "尚未建房。")
        return
    if uid != room.host_id:
        reply_text(event, "僅建房者可換角。")
        return
    if room.phase != "config":
        reply_text(event, "現在不是換角階段。")
        return

    if target == "女巫":
        ok, msg = swap_doctor_to_witch(room.current_roles)
    elif target == "獵人":
        ok, msg = swap_villager_to_hunter(room.current_roles)
    else:
        reply_text(event, "只能換『女巫』或『獵人』。")
        return

    reply_text(event, (msg if ok else f"換角失敗：{msg}") + f"\n目前角色：{pretty_roles(room.current_roles)}")

def cmd_confirm_roles(event):
    rid, uid = get_room_id(event), get_user_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "尚未建房。")
        return
    if uid != room.host_id:
        reply_text(event, "僅建房者可確認角色。")
        return
    if room.phase != "config":
        reply_text(event, "現在不是確認階段。請先「開始」。")
        return
    if len(room.current_roles) != len(room.players):
        reply_text(event, "角色數與玩家數不符，請確認後再試。")
        return

    # 發牌 & 進夜晚
    room.started = True
    room.phase = "night"
    # 重置每晚旗標
    room.wolf_targets = []
    room.night_flags["seer_done_uids"] = set()
    room.night_flags["doctor_saved_uid"] = None
    room.night_flags["witch_save_flag"] = False
    room.night_flags["witch_poison_uid"] = None
    # 女巫/醫生跨晚限制保留：doctor_selfheal_used, doctor_last_saved_uid, witch_xxx_left

    assign_and_notify(room, room.current_roles.copy())
    reply_text(event,
        "🎲 已發牌！\n"
        f"本局角色：{pretty_roles(room.current_roles)}\n"
        f"🌙 夜晚開始（自動倒數 {NIGHT_MINUTES} 分鐘）：\n"
        "  狼人私訊『擊殺 名字』\n"
        "  預言家私訊『查驗 名字』\n"
        "  醫生私訊『救 名字』\n"
        "  女巫私訊『解救』（只能救當晚刀口，不得自救）或『投毒 名字』"
    )
    schedule_night_timeout(room)

def cmd_status(event):
    rid = get_room_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "尚未建房或房已結束。")
        return
    left = None
    if room.deadline_at:
        sec = int((room.deadline_at - now_utc()).total_seconds())
        left = max(0, sec)
    lines = [
        f"📋 狀態：phase={room.phase}",
        f"玩家數：{len(room.players)}",
        (f"本階段剩餘：{left // 60} 分 {left % 60} 秒" if left is not None else ""),
    ]
    if room.phase == "config":
        lines.append(f"模板角色（目前）：{pretty_roles(room.current_roles)}")
    for p in room.players.values():
        lines.append(f" - {p.name}：{'存活' if p.alive else '出局'}")
    reply_text(event, "\n".join([x for x in lines if x]))

def cmd_reset(event):
    rid, uid = get_room_id(event), get_user_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "無房可重置。")
        return
    if uid != room.host_id:
        reply_text(event, "僅建房者可重置。")
        return
    clear_schedules(room)
    ROOMS.pop(rid, None)
    reply_text(event, "🔁 已重置房間。")

def cmd_extend(event, minutes: int):
    rid, uid = get_room_id(event), get_user_id(event)
    room = ROOMS.get(rid)
    if not room or not room.started:
        reply_text(event, "尚未建房或遊戲未開始。")
        return
    if uid != room.host_id:
        reply_text(event, "僅房主可延長。")
        return
    extend_current_phase(room, minutes)
    reply_text(event, f"⏳ 已將本階段重設為 {minutes} 分鐘倒數。")

def cmd_force(event):
    rid, uid = get_room_id(event), get_user_id(event)
    room = ROOMS.get(rid)
    if not room or not room.started:
        reply_text(event, "尚未建房或遊戲未開始。")
        return
    if uid != room.host_id:
        reply_text(event, "僅房主可立即結算。")
        return
    force_settle(room)
    # 後續訊息在各結算函式內會自動發布

# ================== 夜晚：私訊技能 ==================
def pm_kill(uid: str, text: str):
    room = ensure_in_room(uid)
    if not room or not room.started or room.phase != "night":
        push_text(uid, "現在不是夜晚，或你未在房間。")
        return
    me = room.players[uid]
    if not (me.alive and me.role == "狼人"):
        push_text(uid, "只有存活的狼人可行動。")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        push_text(uid, "用法：擊殺 名字")
        return
    target_name = parts[1].strip()
    cands = [p for p in room.alive_players() if p.name == target_name]
    if not cands:
        push_text(uid, f"找不到活著的「{target_name}」。")
        return
    room.wolf_targets.append(cands[0].user_id)
    push_text(uid, f"已提名刀：{target_name}（待結算）")

def pm_seer(uid: str, text: str):
    room = ensure_in_room(uid)
    if not room or not room.started or room.phase != "night":
        push_text(uid, "現在不是夜晚，或你未在房間。")
        return
    me = room.players[uid]
    if not (me.alive and me.role == "預言家"):
        push_text(uid, "只有存活的『預言家』可行動。")
        return
    if uid in room.night_flags["seer_done_uids"]:
        push_text(uid, "本晚已查驗過了。")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        push_text(uid, "用法：查驗 名字")
        return
    target_name = parts[1].strip()
    cands = [p for p in room.alive_players() if p.name == target_name]
    if not cands:
        push_text(uid, f"找不到活著的「{target_name}」。")
        return
    room.night_flags["seer_done_uids"].add(uid)
    result = "狼人" if cands[0].role == "狼人" else "非狼人"
    push_text(uid, f"查驗結果：{target_name} 是")

def pm_doctor(uid: str, text: str):
    room = ensure_in_room(uid)
    if not room or not room.started or room.phase != "night":
        push_text(uid, "現在不是夜晚，或你未在房間。")
        return
    me = room.players[uid]
    if not (me.alive and me.role == "醫生"):
        push_text(uid, "只有存活的『醫生』可行動。")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        push_text(uid, "用法：救 名字")
        return
    target_name = parts[1].strip()
    cands = [p for p in room.players.values() if p.alive and p.name == target_name]
    if not cands:
        push_text(uid, f"找不到活著的「{target_name}」。")
        return
    target = cands[0]
    # 不得連續兩晚救同一人
    if room.night_flags["doctor_last_saved_uid"] == target.user_id:
        push_text(uid, "不得連續兩晚救同一人。")
        return
    # 自救全局僅一次
    if target.user_id == uid and uid in room.night_flags["doctor_selfheal_used"]:
        push_text(uid, "你的自救次數已用完。")
        return
    room.night_flags["doctor_saved_uid"] = target.user_id
    if target.user_id == uid:
        room.night_flags["doctor_selfheal_used"].add(uid)
    push_text(uid, f"已標記救援：{target.name}")

def pm_witch_heal(uid: str):
    room = ensure_in_room(uid)
    if not room or not room.started or room.phase != "night":
        push_text(uid, "現在不是夜晚，或你未在房間。")
        return
    me = room.players[uid]
    if not (me.alive and me.role == "女巫"):
        push_text(uid, "只有存活的『女巫』可行動。")
        return
    if not room.night_flags["witch_heal_left"]:
        push_text(uid, "你的解藥已用完。")
        return
    # 不直接指定對象；僅標記本晚使用。實際救誰在結算時計算狼刀目標。
    room.night_flags["witch_save_flag"] = True
    push_text(uid, "已使用『解救』（僅對當晚刀口生效，且不得自救）。")

def pm_witch_poison(uid: str, text: str):
    room = ensure_in_room(uid)
    if not room or not room.started or room.phase != "night":
        push_text(uid, "現在不是夜晚，或你未在房間。")
        return
    me = room.players[uid]
    if not (me.alive and me.role == "女巫"):
        push_text(uid, "只有存活的『女巫』可行動。")
        return
    if not room.night_flags["witch_poison_left"]:
        push_text(uid, "你的毒藥已用完。")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        push_text(uid, "用法：投毒 名字")
        return
    target_name = parts[1].strip()
    cands = [p for p in room.alive_players() if p.name == target_name]
    if not cands:
        push_text(uid, f"找不到活著的「{target_name}」。")
        return
    room.night_flags["witch_poison_uid"] = cands[0].user_id
    push_text(uid, f"已標記『投毒』對象：{target_name}")

def pm_hunter_shoot(uid: str, text: str):
    room = ensure_in_room(uid)
    if not room:
        return
    if room.hunter_pending_uid != uid:
        push_text(uid, "你目前無法開槍。")
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        push_text(uid, "用法：開槍 名字")
        return
    target_name = parts[1].strip()
    cands = [p for p in room.alive_players() if p.name == target_name]
    if not cands:
        push_text(uid, f"找不到活著的「{target_name}」。")
        return
    victim = cands[0]
    victim.alive = False
    room.hunter_pending_uid = None
    push_text(room.room_id, f"🔫 獵人開槍：{victim.name} 被帶走。")
    if check_game_end(room):
        return

# ================== 夜晚結算 → 白天 ==================
def resolve_night_and_start_day(room: GameRoom, event=None):
    # 1) 狼人票選刀口
    wolf_target_uid = None
    if room.wolf_targets:
        tally = Counter(room.wolf_targets)
        maxv = max(tally.values())
        tied = [uid for uid, v in tally.items() if v == maxv]
        wolf_target_uid = random.choice(tied)

    # 2) 醫生救人（覆蓋狼刀）
    if room.night_flags["doctor_saved_uid"] == wolf_target_uid:
        wolf_target_uid = None  # 被救

    # 3) 女巫解藥（僅能救當晚刀口，且不得自救）
    if room.night_flags["witch_save_flag"] and room.night_flags["witch_heal_left"]:
        if wolf_target_uid is not None:
            # 不得自救：若刀口就是女巫本人，則解藥無效
            witch_uid = room.night_flags["witch_uid"]
            if wolf_target_uid != witch_uid:
                wolf_target_uid = None
                room.night_flags["witch_heal_left"] = False

    # 4) 女巫毒藥（與解藥獨立生效；可毒任意活人）
    poison_uid = None
    if room.night_flags["witch_poison_uid"] and room.night_flags["witch_poison_left"]:
        poison_uid = room.night_flags["witch_poison_uid"]
        room.night_flags["witch_poison_left"] = False

    # 死亡名單
    deaths = []
    if wolf_target_uid:
        p = room.players.get(wolf_target_uid)
        if p and p.alive:
            p.alive = False
            deaths.append(p)
    if poison_uid and (poison_uid != wolf_target_uid):
        p = room.players.get(poison_uid)
        if p and p.alive:
            p.alive = False
            deaths.append(p)

    # 獵人待開槍
    for p in deaths:
        if p.role == "獵人":
            room.hunter_pending_uid = p.user_id
            push_text(p.user_id, "你被淘汰了！可『私訊』輸入：開槍 名字（一次）。")

    # 公告
    if deaths:
        msg = "🌞 天亮了！昨晚淘汰：" + "、".join(p.name for p in deaths)
    else:
        msg = "🌞 天亮了！昨晚是平安夜。"
    if event: reply_text(event, msg)
    else: push_text(room.room_id, msg)

    # 清空當晚狀態（保留跨晚限制）
    room.wolf_targets = []
    room.night_flags["seer_done_uids"] = set()
    # 記錄「上一晚醫生救的人」用於「不得連救同一人」
    room.night_flags["doctor_last_saved_uid"] = room.night_flags["doctor_saved_uid"]
    room.night_flags["doctor_saved_uid"] = None
    room.night_flags["witch_save_flag"] = False
    room.night_flags["witch_poison_uid"] = None

    # 終局判定
    if check_game_end(room, event):
        return

    # 進入白天並啟動倒數
    room.phase = "day"
    schedule_day_timeout(room)
    tip = "請討論並『投票 名字』，時間到自動『結算』放逐最高票。"
    if event: reply_text(event, tip)
    else: push_text(room.room_id, tip)

# ================== 白天流程（手動與自動共用） ==================
def auto_endday(room: GameRoom):
    """自動版白天結算（與手動邏輯一致）。"""
    if not room.votes:
        push_text(room.room_id, "⌛ 白天時間到：今天無人投票，進入夜晚。")
        room.phase = "night"
        schedule_night_timeout(room)
        return
    tally = Counter(room.votes.values())
    maxv = max(tally.values())
    losers = [uid for uid, v in tally.items() if v == maxv]
    victim_uid = random.choice(losers)
    victim = room.players[victim_uid]
    victim.alive = False
    room.votes.clear()
    push_text(room.room_id, f"📢 白天結算：{victim.name} 被放逐。")

    if victim.role == "獵人":
        room.hunter_pending_uid = victim.user_id
        push_text(victim.user_id, "你被淘汰了！可『私訊』輸入：開槍 名字（一次）。")

    if check_game_end(room):
        return
    room.phase = "night"
    schedule_night_timeout(room)
    push_text(room.room_id, "🌙 夜晚來臨，狼人請在『私訊』輸入「擊殺 名字」。")

def cmd_vote(event, target_name: str):
    rid = get_room_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "尚未建房。")
        return
    if room.phase != "day":
        reply_text(event, "現在不是白天投票階段。")
        return
    voter = get_user_id(event)
    if voter not in room.players or not room.players[voter].alive:
        reply_text(event, "你未參與本局或已出局，不能投票。")
        return
    cands = [p for p in room.alive_players() if p.name == target_name]
    if not cands:
        reply_text(event, f"找不到活著的「{target_name}」。")
        return
    room.votes[voter] = cands[0].user_id
    reply_text(event, f"✅ 已投票給：{target_name}")

def cmd_endday(event):
    rid = get_room_id(event)
    room = ROOMS.get(rid)
    if not room:
        reply_text(event, "尚未建房。")
        return
    if room.phase != "day":
        reply_text(event, "現在不是白天結算階段。")
        return
    # 手動結算
    auto_endday(room)

# ================== 路由 ==================
@app.route("/")
def index():
    return "Werewolf LINE Bot：/callback", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ================== 事件處理 ==================
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    # ==== 私訊技能 ====
    if text.startswith("擊殺"):
        pm_kill(get_user_id(event), text); return
    if text.startswith("查驗"):
        pm_seer(get_user_id(event), text); return
    if text.startswith("救"):
        pm_doctor(get_user_id(event), text); return
    if text == "解救":
        pm_witch_heal(get_user_id(event)); return
    if text.startswith("投毒"):
        pm_witch_poison(get_user_id(event), text); return
    if text.startswith("開槍"):
        pm_hunter_shoot(get_user_id(event), text); return

    # ==== 群組/私訊中文指令 ====
    if text == "幫助": cmd_help(event); return
    if text == "角色清單": cmd_rolelist(event); return
    if text == "建房": cmd_build(event); return
    if text == "加入": cmd_join(event); return
    if text == "狀態": cmd_status(event); return
    if text == "重置": cmd_reset(event); return

    if text == "開始": cmd_start(event); return
    if text == "確認角色": cmd_confirm_roles(event); return
    if text.startswith("換"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            cmd_swap(event, parts[1].strip())
        else:
            reply_text(event, "用法：換 女巫／換 獵人")
        return

    if text.startswith("投票"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2: cmd_vote(event, parts[1].strip())
        else: reply_text(event, "用法：投票 名字（例：投票 小明）")
        return
    if text == "結算":
        cmd_endday(event); return

    # 房主工具
    if text.startswith("延長"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            cmd_extend(event, int(parts[1])); return
        reply_text(event, "用法：延長 分鐘數（例：延長 2）"); return
    if text == "立即結算":
        cmd_force(event); return

    # 默認不回覆，避免干擾群聊
    return

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
