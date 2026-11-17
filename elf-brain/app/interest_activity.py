"""Offer small, executable interest activities and persist their outcomes."""

import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from urllib.parse import urlsplit

from app.interest import database

router = APIRouter(prefix="/interest/activity")

# One observable result per activity. These suggestions work with resources people already have.
ACTIVITIES = (
    ("photo", r"摄影|拍照|影像|photo", "摄影", 5, "复盘一张旧照片", "从相册选一张最近拍的照片，只改一次裁切。保留你更喜欢的一版。"),
    ("photo-light", r"摄影|拍照|影像|photo", "摄影", 10, "给同一个物件试两种光", "用手机拍桌上同一个东西。一张朝窗，一张背窗，留下一张最能说明光线差异的照片。"),
    ("code", r"编程|代码|开发|软件|coding|programming", "编程", 10, "把一个小麻烦变成可复现的例子", "打开手头的项目，挑一个反复卡住的地方。写下最短的复现步骤，留一句下一步要验证什么。"),
    ("code-test", r"编程|代码|开发|软件|coding|programming", "编程", 5, "补一个能防退化的例子", "找一处刚改过的逻辑，写一条输入和预期结果。先覆盖你最容易忘的边界。"),
    ("music", r"音乐|作曲|乐器|吉他|钢琴|歌|music", "音乐", 5, "听出一首歌最抓你的八小节", "放一首最近喜欢的歌，记下最想重听的位置，再写一句抓住你的是节奏、声音还是歌词。"),
    ("music-create", r"音乐|作曲|乐器|吉他|钢琴|歌|music", "音乐", 10, "换一种节奏", "选一段熟悉的旋律，用手打两种节奏，或录一小段。留下更想继续的一版。"),
    ("game", r"游戏|game", "游戏", 5, "拆一个让你想再试一次的机制", "想起最近玩过的一段关卡，记下它给了你什么线索、让你做了什么选择。"),
    ("game-make", r"游戏|game", "游戏", 10, "画一关只用一种规则的小游戏", "用纸画入口、目标和一个障碍，写下玩家怎样发现规则。画完就算这一关有了雏形。"),
    ("food", r"咖啡|烹饪|料理|食谱|美食|coffee|cook", "咖啡与烹饪", 5, "写下一次还想复现的做法", "选最近做过的一杯咖啡或一道菜，记下最好的一步，下次只改一个量或一个时间。"),
    ("story", r"影视|电影|科幻|写作|剧集|fiction|writing", "影视与写作", 10, "替喜欢的一幕写一个不同的出口", "选一段你记得的场景。让角色做相反的决定，用三句话写出接下来会发生什么。"),
    ("hardware", r"数码|硬件|装机|hardware", "数码硬件", 5, "把一项升级冲动改成对照", "写下你想解决的实际问题，以及手头设备在什么条件下做不到。以后只对照这一条。"),
    ("movement", r"健身|运动|跑步|体育|fitness", "运动", 5, "复盘一小段最近的运动", "写下上一次运动时最顺的一个动作和最想改的一点。下次先观察这一点。"),
    ("finance", r"理财|投资|finance", "财务阅读", 10, "给一个财务概念找个反例", "挑一个读过的财务概念，用自己的话写出适用条件，再找一个它不成立的例子。"),
)


class Idea(BaseModel):
    key: str = Field(min_length=1, max_length=150)
    topic: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=180)
    step: str = Field(min_length=1, max_length=500)
    reason: str = Field(max_length=180)
    minutes: int = Field(ge=2, le=60)
    url: str = Field(default="", max_length=2000)
    source: str = Field(default="", max_length=100)


class Outcome(BaseModel):
    id: str
    outcome: Literal["done", "later", "skip"]
    note: str = Field(default="", max_length=500)


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS interest_actions (
        id TEXT PRIMARY KEY, idea TEXT NOT NULL, started REAL NOT NULL, until REAL NOT NULL,
        outcome TEXT, note TEXT NOT NULL DEFAULT '', finished REAL
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS current_interest_action
        ON interest_actions ((1)) WHERE outcome IS NULL""")


def _web_url(value) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        return ""
    try:
        url = urlsplit(value)
        if url.scheme in ("https", "http") and url.hostname and not (url.username or url.password):
            return value
    except ValueError:
        pass
    return ""


def suggest(interests: list[dict], minutes: int, intent: str, exclude: list[str]):
    now = time.time()
    topics = sorted(
        ((str(row["name"])[:120], float(row.get("temperature", 0))) for row in interests
         if row.get("name") and row.get("status") != "paused"),
        key=lambda topic: topic[1], reverse=True,
    )[:15]
    search = intent.strip()[:120]
    with database() as db:
        _schema(db)
        saved = db.execute("""SELECT item.* FROM interest_items item WHERE done = 0 AND (
            (kind = 'pick' AND NOT EXISTS (
                SELECT 1 FROM interest_items pending WHERE pending.kind = 'handoff'
                  AND pending.done = 0 AND json_extract(pending.data, '$.action_pick') = item.id
            )) OR (kind = 'handoff' AND due <= ?)
        ) ORDER BY created DESC LIMIT 80""", (now,)).fetchall()
        recent = db.execute("""SELECT idea, outcome, finished FROM interest_actions
            WHERE finished > ? ORDER BY finished DESC LIMIT 100""", (now - 14 * 86400,)).fetchall()

    penalties: dict[str, int] = {}
    for past in recent:
        idea = json.loads(past["idea"])
        outcome = past["outcome"]
        if outcome == "later" and idea["topic"] == "待续":
            continue  # Its due time already controls when the user wants to see it again.
        key = idea["key"]
        penalties[key] = penalties.get(key, 0) + {"done": 24, "skip": 34, "later": 18}[outcome]

    def affinity(text: str):
        return 35 if search and any(
            re.search(pattern, search, re.I) and re.search(pattern, text, re.I)
            for _, pattern, *_ in ACTIVITIES
        ) else 0

    candidates = []
    for row in saved:
        data = json.loads(row["data"])
        is_handoff = row["kind"] == "handoff"
        try:
            length = int(data.get("minutes", 5))
        except (ValueError, TypeError, OverflowError):
            continue
        url = data.get("url")
        urls = data.get("urls")
        if is_handoff and isinstance(urls, list) and urls:
            url = urls[0]
        note = str(data.get("note") or "").strip()
        label = row["title"]
        if 2 <= length <= minutes:
            score = (55 if is_handoff else 44) + affinity(label + " " + note)
            candidates.append((score, Idea(
                key="item:" + row["id"], topic="待续" if is_handoff else "兴趣清单",
                title=label, minutes=length, url=_web_url(url), source=row["id"],
                step=note[:500] or ("打开链接，做你上次留下的下一步。" if is_handoff else "按清单里的安排开始，留一句以后还想继续的地方。"),
                reason="你上次留到现在的事" if is_handoff else "你自己收进兴趣清单的事",
            )))

    for key, pattern, label, length, title, step in ACTIVITIES:
        match, temperature = next(((topic, heat) for topic, heat in topics if re.search(pattern, topic, re.I)), ("", 0))
        explicit = bool(search and re.search(pattern, search, re.I))
        if length <= minutes and (explicit or match):
            candidates.append(((74 if explicit else 32) + int(12 * temperature), Idea(
                key=key, topic=match or label, title=title, step=step, minutes=length,
                reason=("按你这次选的兴趣" if explicit else "来自你关注的「" + match + "」"),
            )))

    topic = topics[0][0] if topics else "此刻好奇的事"
    candidates.extend([
        (10, Idea(key="small-question", topic=topic, title="给一个好奇点留下一步", minutes=min(5, minutes),
                  step=f"围绕「{topic}」写下一个具体问题，打开一份手边的材料，找出一小条线索。把下一步留在结束时的记录里。",
                  reason="从此刻能完成的一小步开始")),
        (8, Idea(key="small-output", topic=topic, title="留下一样看得见的小东西", minutes=min(5, minutes),
                 step=f"围绕「{topic}」做一个可保存的小样。一张草图、一段记录，或一个你愿意留到明天的观察都行。",
                 reason="适合只有几分钟的时候")),
    ])
    ranked = sorted(candidates, key=lambda pair: pair[0] - penalties.get(pair[1].key, 0), reverse=True)
    return next((idea for _, idea in ranked if idea.key not in exclude), None)


def _read_session(row):
    return None if row is None else {
        "id": row["id"], "idea": json.loads(row["idea"]), "started": row["started"], "until": row["until"],
        "outcome": row["outcome"], "note": row["note"],
    }


@router.get("/current")
def get_current():
    with database() as db:
        _schema(db)
        current = db.execute("SELECT * FROM interest_actions WHERE outcome IS NULL").fetchone()
        recent = db.execute("""SELECT * FROM interest_actions WHERE outcome = 'done'
            ORDER BY finished DESC LIMIT 1""").fetchone()
    return {"current": _read_session(current), "recent": _read_session(recent)}


@router.post("/current")
def start_activity(idea: Idea):
    with database() as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM interest_actions WHERE outcome IS NULL").fetchone()
        if row:
            return _read_session(row)
        now = time.time()
        action_id = uuid.uuid4().hex
        db.execute("INSERT INTO interest_actions (id, idea, started, until) VALUES (?, ?, ?, ?)",
                   (action_id, idea.model_dump_json(), now, now + idea.minutes * 60))
        return _read_session(db.execute("SELECT * FROM interest_actions WHERE id = ?", (action_id,)).fetchone())


@router.patch("/current")
def finish_activity(change: Outcome):
    with database() as db:
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM interest_actions WHERE id = ?", (change.id,)).fetchone()
        if row is None:
            raise HTTPException(404, "这段行动已找不到")
        item = _read_session(row)
        if item["outcome"]:
            return item

        idea = item["idea"]
        now = time.time()
        note = change.note.strip()
        db.execute("UPDATE interest_actions SET outcome = ?, note = ?, finished = ? WHERE id = ?",
                   (change.outcome, note, now, change.id))
        source = db.execute("SELECT * FROM interest_items WHERE id = ? AND kind IN ('pick','handoff')",
                            (idea["source"],)).fetchone() if idea["source"] else None
        carried = bool(source and source["kind"] == "handoff")
        previous = json.loads(source["data"]) if carried else {}
        if change.outcome == "done" and source:
            db.execute("UPDATE interest_items SET done = 1 WHERE id = ?", (source["id"],))
            if previous.get("action_pick"):
                db.execute("UPDATE interest_items SET done = 1 WHERE id = ? AND kind = 'pick'",
                           (previous["action_pick"],))
        if change.outcome == "later":
            previous.setdefault("urls", [idea["url"]] if _web_url(idea["url"]) else [])
            previous.update(note=note or idea["step"], minutes=idea["minutes"])
            if source and source["kind"] == "pick":
                previous["action_pick"] = source["id"]
            data = json.dumps(previous, ensure_ascii=False)
            if carried:
                db.execute("UPDATE interest_items SET due = ?, data = ? WHERE id = ?",
                           (now + 1800, data, source["id"]))
            else:
                db.execute("""INSERT INTO interest_items (id, kind, title, data, created, due)
                    VALUES (?, 'handoff', ?, ?, ?, ?)""", ("action-" + change.id, idea["title"], data, now, now + 1800))
        return {**item, "outcome": change.outcome, "note": note}
