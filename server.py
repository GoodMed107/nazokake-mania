"""なぞかけマニア！ — オンライン対戦サーバー（フェーズ1: ローカル完成版）

- aiohttp で ゲーム画面配信 + WebSocket を1ポートで提供
- ゲーム進行はサーバー権威（タイマー・投票集計はサーバーが持つ）
- 空き枠は Claude 生成のAIで埋める

起動:  python server.py
既定 http://localhost:8080
"""

import os
import json
import random
import string
import asyncio
import pathlib

from aiohttp import web, WSMsgType

import odai
import ai

HERE = pathlib.Path(__file__).parent
PUBLIC = HERE / "public"


def _load_dotenv():
    """.env があれば環境変数に読み込む（追加ライブラリ不要の簡易版）。"""
    env = HERE / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

TOTAL_ROUNDS = int(os.environ.get("NAZOKAKE_ROUNDS", "5"))
THINK_SECONDS = int(os.environ.get("NAZOKAKE_THINK", "90"))
VOTE_SECONDS = int(os.environ.get("NAZOKAKE_VOTE", "45"))
REVEAL_SECONDS = int(os.environ.get("NAZOKAKE_REVEAL", "20"))
MAX_PLAYERS = 4

AI_NAMES = ["蒼太", "あかね", "こまち", "げん"]
AI_INITIALS = {"蒼太": "蒼", "あかね": "茜", "こまち": "こ", "げん": "玄"}

rooms = {}  # code -> Room


def new_code():
    alphabet = "ABCDEFGHJKLMNPRSTUVWXYZ23456789"  # 紛らわしい文字は除外
    while True:
        code = "".join(random.choices(alphabet, k=4))
        if code not in rooms:
            return code


def new_id():
    return "p_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


class Player:
    def __init__(self, pid, name, ws=None, is_ai=False):
        self.id = pid
        self.name = name
        self.ws = ws          # None = AI or disconnected
        self.is_ai = is_ai
        self.connected = ws is not None or is_ai
        self.score = 0
        self.initial = (AI_INITIALS.get(name) if is_ai else (name[0] if name else "?"))

    def public(self, is_host):
        return {"id": self.id, "name": self.name, "isAI": self.is_ai,
                "isHost": is_host, "connected": self.connected,
                "score": self.score, "initial": self.initial}


class Room:
    def __init__(self, code):
        self.code = code
        self.players = {}      # id -> Player
        self.order = []        # id order (join order)
        self.host_id = None
        self.state = "lobby"   # lobby / playing
        self.game_task = None
        self.ai_batch_task = None  # ゲーム開始時に全AI解答をまとめて生成するタスク
        # per-round working state
        self.round_index = 0
        self.odai_list = []
        self.submissions = {}  # pid -> {"b","c"} (存在=提出済み, パスは入れない)
        self.votes = {}        # voter_id -> target_id
        self.submit_event = asyncio.Event()
        self.vote_event = asyncio.Event()
        self.next_event = asyncio.Event()

    # ---- players ----
    def add_human(self, name, ws):
        pid = new_id()
        p = Player(pid, name or "プレイヤー", ws=ws)
        self.players[pid] = p
        self.order.append(pid)
        if self.host_id is None:
            self.host_id = pid
        return p

    def add_ai(self):
        used = {p.name for p in self.players.values() if p.is_ai}
        name = next((n for n in AI_NAMES if n not in used), None)
        if not name:
            return None
        pid = new_id()
        p = Player(pid, name, is_ai=True)
        self.players[pid] = p
        self.order.append(pid)
        return p

    def remove_player(self, pid):
        p = self.players.pop(pid, None)
        if pid in self.order:
            self.order.remove(pid)
        if self.host_id == pid:
            humans = [q for q in self.ordered() if not q.is_ai]
            self.host_id = humans[0].id if humans else None
        return p

    def ordered(self):
        return [self.players[i] for i in self.order if i in self.players]

    def humans(self):
        return [p for p in self.ordered() if not p.is_ai]

    def connected_humans(self):
        return [p for p in self.humans() if p.connected]

    def count_total(self):
        return len(self.players)

    # ---- broadcasting ----
    async def send(self, player, payload):
        if player.ws is None:
            return
        try:
            await player.ws.send_str(json.dumps(payload, ensure_ascii=False))
        except Exception:
            player.connected = False

    async def broadcast(self, payload, per_player=None):
        for p in self.ordered():
            if p.ws is None:
                continue
            data = per_player(p) if per_player else payload
            await self.send(p, data)

    def lobby_state(self):
        return {
            "type": "lobby",
            "code": self.code,
            "players": [p.public(p.id == self.host_id) for p in self.ordered()],
            "canStart": len(self.connected_humans()) >= 1,
            "canAddAI": self.count_total() < MAX_PLAYERS,
            "totalRounds": TOTAL_ROUNDS,
        }

    async def push_lobby(self):
        await self.broadcast(None, per_player=lambda p: {
            **self.lobby_state(),
            "you": {"id": p.id, "isHost": p.id == self.host_id},
        })

    # ---- game loop ----
    async def run_game(self):
        try:
            for p in self.players.values():
                p.score = 0
            self.state = "playing"
            self.odai_list = odai.random_odai(TOTAL_ROUNDS)
            # 全お題×全AIのなぞかけを1回でまとめて生成（開始と同時に裏で実行）。
            # 各席のシンキングタイム中に生成が終わるので待ち時間は目立たない。
            ai_names = [p.name for p in self.ordered() if p.is_ai]
            self.ai_batch_task = (
                asyncio.create_task(ai.generate_nazokake_batch(self.odai_list, ai_names))
                if ai_names else None)
            for r in range(TOTAL_ROUNDS):
                self.round_index = r
                await self.play_round(r)
            await self.broadcast({"type": "final", "ranking": self.ranking()})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[room {self.code}] ゲーム中エラー: {e}")
        finally:
            # ゲーム終了。最終結果の画面を残すため、ここではロビーを送らない。
            # ホストが「もう一席」を押すと push_lobby でロビーへ戻る。
            self.state = "lobby"
            self.game_task = None

    async def play_round(self, r):
        current_odai = self.odai_list[r]
        self.submissions = {}
        self.votes = {}
        self.submit_event = asyncio.Event()
        self.vote_event = asyncio.Event()

        # --- フェーズ2: シンキングタイム ---
        loop = asyncio.get_event_loop()
        ends_at = loop.time() + THINK_SECONDS
        await self.broadcast({
            "type": "round", "roundIndex": r, "totalRounds": TOTAL_ROUNDS,
            "odai": current_odai, "seconds": THINK_SECONDS,
        })

        # 人間の提出待ち（全員提出で早期終了）or タイムアウト
        self._check_submit_done()
        try:
            await asyncio.wait_for(self.submit_event.wait(),
                                   timeout=max(0.1, ends_at - loop.time()))
        except asyncio.TimeoutError:
            pass

        # AIの解答は「ゲーム開始時に1回でまとめて生成したバッチ」から取得
        batch = {}
        if self.ai_batch_task is not None:
            try:
                batch = await self.ai_batch_task
            except Exception:
                batch = {}
        for p in self.ordered():
            if p.is_ai:
                ans = batch.get(r, {}).get(p.name)
                if not ans:
                    ans = ai._fallback_generate(current_odai)
                self.submissions[p.id] = {"b": ans["b"], "c": ans["c"]}

        # --- フェーズ4前半: 発表 & 投票 ---
        answers = [p for p in self.ordered() if p.id in self.submissions]
        random.shuffle(answers)
        anon = {}  # anon_id -> player_id
        anon_list = []
        for i, p in enumerate(answers):
            aid = f"a{i}"
            anon[aid] = p.id
            s = self.submissions[p.id]
            anon_list.append({"aid": aid, "b": s["b"], "c": s["c"], "ownerId": p.id})
        self._anon = anon

        vote_ends = loop.time() + VOTE_SECONDS
        await self.broadcast(None, per_player=lambda p: {
            "type": "vote",
            "seconds": VOTE_SECONDS,
            "answers": [{"aid": a["aid"], "b": a["b"], "c": a["c"],
                         "isYou": a["ownerId"] == p.id} for a in anon_list],
            "canVote": p.id in self.submissions or True,
        })

        self._check_vote_done()
        try:
            await asyncio.wait_for(self.vote_event.wait(),
                                   timeout=max(0.1, vote_ends - loop.time()))
        except asyncio.TimeoutError:
            pass

        # 成立チェック（参考表示）を1回でまとめて実行（無料枠の節約）
        try:
            verdicts = await ai.check_nazokake_batch(
                current_odai,
                [{"id": p.id, "b": self.submissions[p.id]["b"],
                  "c": self.submissions[p.id]["c"]} for p in answers])
        except Exception:
            verdicts = {p.id: {"verdict": "good", "reason": ""} for p in answers}

        # AIの投票（自分以外・重み付き）
        for p in self.ordered():
            if p.is_ai and p.id in self.submissions:
                self._ai_vote(p, answers, verdicts)

        # 集計
        tally = {p.id: 0 for p in self.ordered()}
        voters_of = {p.id: [] for p in self.ordered()}
        for voter_id, target_id in self.votes.items():
            if target_id in tally:
                tally[target_id] += 1
                if voter_id in self.players:
                    voters_of[target_id].append(self.players[voter_id].name)
        for pid, n in tally.items():
            if pid in self.players:
                self.players[pid].score += n

        max_v = max(tally.values()) if tally else 0
        results = []
        for p in answers:
            s = self.submissions[p.id]
            v = verdicts.get(p.id, {"verdict": "good", "reason": ""})
            results.append({
                "playerId": p.id, "name": p.name, "initial": p.initial,
                "isAI": p.is_ai, "b": s["b"], "c": s["c"],
                "verdict": v["verdict"], "reason": v.get("reason", ""),
                "votes": tally[p.id], "voters": voters_of[p.id],
                "winner": tally[p.id] == max_v and max_v > 0,
            })
        # パスした人も見せる
        passed = [{"name": p.name, "initial": p.initial, "isAI": p.is_ai}
                  for p in self.ordered() if p.id not in self.submissions]

        # --- フェーズ5: 開票 & 暫定順位 ---
        self.next_event = asyncio.Event()
        await self.broadcast({
            "type": "reveal", "odai": current_odai,
            "roundIndex": r, "totalRounds": TOTAL_ROUNDS,
            "results": results, "passed": passed,
            "standings": self.ranking(),
            "isFinal": r == TOTAL_ROUNDS - 1,
        })
        try:
            await asyncio.wait_for(self.next_event.wait(), timeout=REVEAL_SECONDS)
        except asyncio.TimeoutError:
            pass

    def _ai_vote(self, ai_player, answers, verdicts):
        candidates = [p for p in answers if p.id != ai_player.id]
        if not candidates:
            return
        weights = []
        for p in candidates:
            w = 1.0
            v = verdicts.get(p.id, {}).get("verdict", "good")
            if v == "good":
                w += 1.1
            elif v == "warn":
                w += 0.2
            if not p.is_ai:
                w += 1.3  # 人間に少し下駄
            weights.append(w)
        pick = random.choices(candidates, weights=weights, k=1)[0]
        self.votes[ai_player.id] = pick.id

    def _check_submit_done(self):
        need = self.connected_humans()
        if need and all(p.id in self.submissions for p in need):
            self.submit_event.set()
        elif not need:
            self.submit_event.set()

    def _check_vote_done(self):
        # 提出した接続中の人間が全員投票したら終了
        need = [p for p in self.connected_humans() if p.id in self.submissions]
        if need and all(p.id in self.votes for p in need):
            self.vote_event.set()
        elif not need:
            self.vote_event.set()

    def ranking(self):
        arr = sorted(self.ordered(), key=lambda p: -p.score)
        out = []
        rank = 0
        prev = None
        for i, p in enumerate(arr):
            if prev is None or p.score != prev:
                rank = i + 1
                prev = p.score
            out.append({"id": p.id, "name": p.name, "initial": p.initial,
                        "isAI": p.is_ai, "score": p.score, "rank": rank})
        return out

    # ---- actions from clients ----
    def submit(self, pid, b, c):
        if self.state != "playing":
            return
        b = (b or "").strip()[:40]
        c = (c or "").strip()[:60]
        if not b or not c:
            return
        if pid in self.players and pid not in self.submissions:
            self.submissions[pid] = {"b": b, "c": c}
            self._check_submit_done()

    def vote(self, pid, aid):
        if self.state != "playing":
            return
        anon = getattr(self, "_anon", {})
        target = anon.get(aid)
        if not target or target == pid:
            return
        if pid in self.players and pid not in self.votes:
            self.votes[pid] = target
            self._check_vote_done()


# ================= WebSocket handler =================

async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    room = None
    me = None

    async def error(msg):
        await ws.send_str(json.dumps({"type": "error", "message": msg}, ensure_ascii=False))

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except Exception:
            continue
        t = data.get("type")

        if t == "create":
            code = new_code()
            room = Room(code)
            rooms[code] = room
            me = room.add_human(data.get("name"), ws)
            await room.push_lobby()

        elif t == "join":
            code = (data.get("code") or "").upper().strip()
            room = rooms.get(code)
            if not room:
                await error("その部屋コードは見つかりません")
                room = None
                continue
            if room.state != "lobby":
                await error("その部屋はゲーム中です")
                room = None
                continue
            if len(room.humans()) + 1 > MAX_PLAYERS:
                await error("その部屋は満員です")
                room = None
                continue
            me = room.add_human(data.get("name"), ws)
            await room.push_lobby()

        elif t == "addAI" and room and me and me.id == room.host_id:
            if room.state == "lobby":
                room.add_ai()
                await room.push_lobby()

        elif t == "removeAI" and room and me and me.id == room.host_id:
            if room.state == "lobby":
                room.remove_player(data.get("id"))
                await room.push_lobby()

        elif t == "start" and room and me and me.id == room.host_id:
            if room.state == "lobby" and len(room.connected_humans()) >= 1:
                room.game_task = asyncio.create_task(room.run_game())

        elif t == "submit" and room and me:
            room.submit(me.id, data.get("b"), data.get("c"))

        elif t == "vote" and room and me:
            room.vote(me.id, data.get("aid"))

        elif t == "next" and room and me and me.id == room.host_id:
            room.next_event.set()

        elif t == "again" and room and me and me.id == room.host_id:
            if room.state == "lobby":
                await room.push_lobby()

    # ---- disconnect ----
    if room and me:
        me.connected = False
        me.ws = None
        if room.state == "playing":
            # 進行中は席を残しつつ、待ち条件を再評価
            room._check_submit_done()
            room._check_vote_done()
        else:
            room.remove_player(me.id)
            if not room.humans():
                rooms.pop(room.code, None)
            else:
                await room.push_lobby()
    return ws


# ================= static =================

async def index(request):
    return web.FileResponse(PUBLIC / "index.html")


async def asset(request):
    name = request.match_info["name"]
    path = PUBLIC / name
    if not path.is_file() or path.parent != PUBLIC:
        raise web.HTTPNotFound()
    ctype = "text/javascript" if name.endswith(".js") else \
            "text/css" if name.endswith(".css") else "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": ctype + "; charset=utf-8"})


def make_app():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index)
    app.router.add_get("/{name}", asset)
    return app


if __name__ == "__main__":
    raw_port = os.environ.get("PORT") or "8080"
    try:
        port = int(str(raw_port).strip())
    except ValueError:
        port = 8080
    print(f"なぞかけマニア！ サーバー起動 (PORT={raw_port} → bind 0.0.0.0:{port})",
          flush=True)
    web.run_app(make_app(), host="0.0.0.0", port=port)
