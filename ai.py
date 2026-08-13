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

import aiohttp

MODEL = os.environ.get("NAZOKAKE_MODEL", "gemini-2.0-flash")

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


async def _call_gemini(system_text, user_text, schema):
    """Gemini generateContent を呼び、JSON(dict)を返す。失敗時 None。"""
    key = _api_key()
    if not key:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 1.0,
        },
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, params={"key": key}, json=body) as r:
                if r.status != 200:
                    txt = await r.text()
                    print(f"[ai] Gemini HTTP {r.status}: {txt[:200]}")
                    return None
                data = await r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
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
