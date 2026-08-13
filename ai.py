"""AI対戦相手のなぞかけ生成と、成立チェック（ハイブリッド判定の参考表示）。

バックエンド: Google Gemini API（無料枠あり）。
aiohttp で REST を直接叩くので、追加ライブラリは不要。

環境変数:
  GEMINI_API_KEY   … Google AI Studio (https://aistudio.google.com/apikey) のキー
  NAZOKAKE_MODEL   … 既定 gemini-2.0-flash（無料枠・高速）

キーが無い / 失敗した場合は、アプリが落ちないよう簡易フォールバックに切替。
"""

import os
import json
import random
import asyncio

import aiohttp

# 第一候補モデル。使えない場合は、キーで使えるモデルから自動選択する。
MODEL = os.environ.get("NAZOKAKE_MODEL", "gemini-3.6-flash")
_resolved_model = None  # 実際に使うモデル（起動後に自動決定してキャッシュ）

# --- スタッガー: 呼び出し開始の最小間隔を空けて 429 を避ける（サーバー全体で共有）---
_STAGGER = float(os.environ.get("NAZOKAKE_STAGGER", "0.7"))  # 秒
_rate_lock = asyncio.Lock()
_last_call = 0.0


async def _rate_gate():
    """直前の呼び出しから _STAGGER 秒あくまで待つ。開始時刻を記録したら即解放。"""
    global _last_call
    async with _rate_lock:
        loop = asyncio.get_running_loop()
        wait = _last_call + _STAGGER - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = loop.time()

# AIキャラの性格（口調・作風）。server 側の AI_NAMES と対応。
PERSONAS = {
    "蒼太": "王道で分かりやすい、少し真面目な作風。同音のダジャレを素直に決める。",
    "あかね": "勢い重視で少し強引。攻めた比喩や時事ネタを混ぜる大喜利タイプ。",
    "こまち": "情緒的で余韻のある落ち。季節感や人情を効かせる。",
    "げん": "理屈っぽく捻る。意外な共通点を突いてくる分析派。",
}

_warned = False


def _api_key():
    global _warned
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key and not _warned:
        print("[ai] GEMINI_API_KEY 未設定。AIはフォールバック（簡易生成）で動きます。")
        _warned = True
    return key


async def _resolve_model(session, key):
    """このキーで実際に使えるモデルを決める（1回だけ問い合わせてキャッシュ）。

    第一候補 MODEL が使えればそれを、ダメなら generateContent 対応の
    flash 系モデルを自動選択する。Google 側のモデル入れ替えに追随できる。
    """
    global _resolved_model
    if _resolved_model:
        return _resolved_model
    chosen = MODEL
    try:
        async with session.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key}) as r:
            data = await r.json()
        avail = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        if MODEL and MODEL in avail:
            chosen = MODEL
        elif avail:
            def score(n):
                s = 0
                if "flash" in n:
                    s += 10
                if "preview" in n or "-exp" in n or "experimental" in n:
                    s -= 6
                if "lite" in n:
                    s -= 1  # フルflashを軽く優先（liteでも可）
                return s
            chosen = sorted(avail, key=score, reverse=True)[0]
        print(f"[ai] 使用モデル: {chosen}  （利用可能 {len(avail)} 件）")
    except Exception as e:
        print(f"[ai] モデル自動選択に失敗（{e}）→ {MODEL} を使用")
    _resolved_model = chosen or MODEL
    return _resolved_model


async def _call_gemini(system_text, user_text, schema, max_tokens=None):
    """Gemini generateContent を呼び、JSON(dict)を返す。失敗時 None。"""
    key = _api_key()
    if not key:
        return None
    await _rate_gate()  # 呼び出しをずらして 429 を避ける
    gen_config = {
        "responseMimeType": "application/json",
        "responseSchema": schema,
        "temperature": 1.0,
    }
    if max_tokens:
        gen_config["maxOutputTokens"] = max_tokens
    body = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": gen_config,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        # APIキーはヘッダーで渡す。新形式(AQ.)/旧形式(AIza)どちらもこれでOK。
        headers = {"x-goog-api-key": key}
        async with aiohttp.ClientSession(timeout=timeout) as s:
            model = await _resolve_model(s, key)
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent")
            # レート上限(429)や一時的なサーバーエラーは少し待って再試行
            for attempt in range(3):
                async with s.post(url, headers=headers, json=body) as r:
                    if r.status == 200:
                        data = await r.json()
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        return json.loads(text)
                    txt = await r.text()
                    if r.status in (429, 500, 503) and attempt < 2:
                        await asyncio.sleep(1.2 * (attempt + 1) + random.random())
                        continue
                    print(f"[ai] Gemini HTTP {r.status}: {txt[:160]}")
                    if r.status == 404:
                        globals()["_resolved_model"] = None
                    return None
        return None
    except Exception as e:
        print(f"[ai] Gemini 呼び出し失敗: {e}")
        return None


# ---------- なぞかけ生成 ----------

_GEN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "b": {"type": "STRING"},
        "c": {"type": "STRING"},
    },
    "required": ["b", "c"],
}


async def generate_nazokake(odai, persona_name):
    """お題 A に対する なぞかけ（B, C）を生成して dict {"b","c"} を返す。"""
    persona = PERSONAS.get(persona_name, "軽妙で分かりやすい作風。")
    system = (
        "あなたは日本語のなぞかけ（掛け言葉）の名手です。"
        "お題Aに対して『AとかけてBと解く、その心はC』の形で一句作ります。"
        "Bは掛ける対象（短い名詞句）、Cは『どちらも〜』の形でAとBをつなぐ落ち。"
        "同音（ダジャレ）か二重の意味で必ずつなげること。短く、キレよく。"
        f"あなたの作風: {persona}"
        " 出力はJSONで、b と c の2フィールドのみ。"
    )
    data = await _call_gemini(system, f"お題は「{odai}」。なぞかけを一句どうぞ。", _GEN_SCHEMA)
    if not data:
        return _fallback_generate(odai)
    b = str(data.get("b", "")).strip()
    c = str(data.get("c", "")).strip()
    if not b or not c:
        return _fallback_generate(odai)
    return {"b": b, "c": c}


# ---------- 成立チェック（参考表示） ----------

_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING"},   # good / warn / bad
        "reason": {"type": "STRING"},
    },
    "required": ["verdict", "reason"],
}


async def check_nazokake(odai, b, c):
    """『AとかけてBと解く、その心はC』が同音・二重の意味で成立しているかの参考判定。"""
    if not b or not c:
        return {"verdict": "good", "reason": ""}
    system = (
        "あなたはなぞかけの審査員です。"
        "『AとかけてBと解く、その心はC』が、同音（ダジャレ）または二重の意味で"
        "AとBをちゃんとつないでいるかを判定します。"
        "笑える強引さは許容し、まったく無関係なときだけ bad にします。"
        " verdict は good（きれいに成立）/ warn（やや強引）/ bad（不成立）のいずれか。"
        " reason は10〜25字程度の短い理由。出力はJSONのみ。"
    )
    data = await _call_gemini(
        system, f"A=「{odai}」 B=「{b}」 C=「{c}」。成立していますか？", _CHECK_SCHEMA)
    if not data:
        return {"verdict": "good", "reason": ""}
    v = data.get("verdict", "good")
    if v not in ("good", "warn", "bad"):
        v = "good"
    return {"verdict": v, "reason": str(data.get("reason", "")).strip()}


# ---------- バッチ生成（全お題×全AIを1回で） ----------

_BATCH_GEN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "rounds": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "answers": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "name": {"type": "STRING"},
                                "b": {"type": "STRING"},
                                "c": {"type": "STRING"},
                            },
                            "required": ["name", "b", "c"],
                        },
                    },
                },
                "required": ["answers"],
            },
        },
    },
    "required": ["rounds"],
}


async def generate_nazokake_batch(odai_list, personas):
    """全お題×全AIのなぞかけを1回のAPI呼び出しで生成。

    戻り値: {round_index: {persona_name: {"b","c"}}}。
    失敗・欠けはダミーで必ず埋めるので、呼び出し側は常に全スロット揃う。
    """
    result = {i: {} for i in range(len(odai_list))}

    def fill_dummy():
        for i, o in enumerate(odai_list):
            for name in personas:
                result[i].setdefault(name, _fallback_generate(o))
        return result

    if not personas:
        return result
    persona_lines = "\n".join(f"- {n}: {PERSONAS.get(n, '軽妙な作風')}" for n in personas)
    odai_lines = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(odai_list))
    system = (
        "あなたは日本語のなぞかけ（掛け言葉）の名手です。"
        "複数のお題それぞれに対して、指定した作者ごとに"
        "『AとかけてBと解く、その心はC』を作ります。"
        "Bは掛ける対象（短い名詞句）、Cは『どちらも〜』でAとBを同音か二重の意味で"
        "つなぐ落ち。短くキレよく。各作者の作風を反映すること。"
    )
    user = (f"お題一覧:\n{odai_lines}\n\n作者と作風:\n{persona_lines}\n\n"
            f"各お題について、{'、'.join(personas)} の順で1句ずつ。"
            f"rounds はお題の順、その中の answers は作者の順で返す。")
    data = await _call_gemini(system, user, _BATCH_GEN_SCHEMA, max_tokens=4096)
    if not data:
        return fill_dummy()
    rounds = data.get("rounds", [])
    for i in range(len(odai_list)):
        answers = rounds[i].get("answers", []) if i < len(rounds) else []
        by_name = {a.get("name"): a for a in answers if a.get("name")}
        for j, name in enumerate(personas):
            a = by_name.get(name) or (answers[j] if j < len(answers) else None)
            b = str(a.get("b", "")).strip() if a else ""
            c = str(a.get("c", "")).strip() if a else ""
            result[i][name] = {"b": b, "c": c} if (b and c) else _fallback_generate(odai_list[i])
    return result


# ---------- バッチ判定（各席の全解答を1回で） ----------

_BATCH_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "results": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "verdict": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                },
                "required": ["id", "verdict", "reason"],
            },
        },
    },
    "required": ["results"],
}


async def check_nazokake_batch(odai, answers):
    """answers: [{"id","b","c"}] を1回でまとめて判定。戻り: {id: {"verdict","reason"}}"""
    out = {a["id"]: {"verdict": "good", "reason": ""} for a in answers}
    if not answers:
        return out
    lines = "\n".join(f'- id={a["id"]}: B「{a["b"]}」 C「{a["c"]}」' for a in answers)
    system = (
        "あなたはなぞかけの審査員です。各解答について"
        "『AとかけてBと解く、その心はC』が、同音（ダジャレ）または二重の意味で"
        "AとBをつないでいるか判定します。笑える強引さは許容し、"
        "まったく無関係なときだけ bad。"
        " verdict は good（成立）/warn（やや強引）/bad（不成立）、reason は10〜25字。"
    )
    user = f"お題A=「{odai}」。次の各解答を id ごとに判定:\n{lines}"
    data = await _call_gemini(system, user, _BATCH_CHECK_SCHEMA, max_tokens=2048)
    if not data:
        return out
    for r in data.get("results", []):
        rid = r.get("id")
        if rid in out:
            v = r.get("verdict", "good")
            if v not in ("good", "warn", "bad"):
                v = "good"
            out[rid] = {"verdict": v, "reason": str(r.get("reason", "")).strip()}
    return out


# ---------- フォールバック（キー無し時のダミー） ----------

_FALLBACK = [
    ("二度寝", "どちらも一度じゃ止まらない"),
    ("連休明け", "どちらも急にやる気が出ない"),
    ("恋のはじまり", "どちらも胸がざわつく"),
    ("締め切り前", "どちらも時間が足りない"),
    ("落語のオチ", "どちらも最後が肝心"),
]


def _fallback_generate(odai):
    b, c = random.choice(_FALLBACK)
    return {"b": b, "c": c}
