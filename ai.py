"""AI対戦相手のなぞかけ生成と、成立チェック（ハイブリッド判定の参考表示）。

仕様書:
 - AIの役割 = 対戦相手（なぞかけを作る）
 - 成立判定 = ハイブリッド（AIが参考表示、得点は人の投票）

Claude API を呼ぶ。ANTHROPIC_API_KEY が無い / 失敗した場合は、
アプリが落ちないようフォールバックの簡易生成に切り替える。

モデルは環境変数 NAZOKAKE_MODEL で変更可（既定 claude-opus-5）。
速度・コスト重視なら claude-haiku-4-5 や claude-sonnet-5 を推奨。
"""

import os
import json
import random
import asyncio

MODEL = os.environ.get("NAZOKAKE_MODEL", "claude-opus-5")
EFFORT = os.environ.get("NAZOKAKE_EFFORT", "low")  # low で軽快に

# AIキャラの性格（口調・作風）。名前は server 側の AI_NAMES と対応。
PERSONAS = {
    "蒼太": "王道で分かりやすい、少し真面目な作風。同音のダジャレを素直に決める。",
    "あかね": "勢い重視で少し強引。攻めた比喩や時事ネタを混ぜる大喜利タイプ。",
    "こまち": "情緒的で余韻のある落ち。季節感や人情を効かせる。",
    "げん": "理屈っぽく捻る。意外な共通点を突いてくる分析派。",
}

_client = None
_client_checked = False


def _get_client():
    """AsyncAnthropic クライアントを遅延生成。キーが無ければ None。"""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ai] ANTHROPIC_API_KEY が未設定です。AIはフォールバック（簡易生成）で動きます。")
        _client = None
        return None
    try:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
        print(f"[ai] Claude API 有効。model={MODEL}")
    except Exception as e:  # SDK未インストールなど
        print(f"[ai] Anthropic SDK を初期化できませんでした（{e}）。フォールバックで動きます。")
        _client = None
    return _client


# ---------- なぞかけ生成 ----------

_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "b": {"type": "string", "description": "「〜と解く」の掛ける対象。短い名詞句。"},
        "c": {"type": "string", "description": "「その心は〜」の落ち。同音や二重の意味でAとBをつなぐ一文。"},
    },
    "required": ["b", "c"],
    "additionalProperties": False,
}


async def generate_nazokake(odai, persona_name):
    """お題 A に対する なぞかけ（B, C）を生成して dict {"b","c"} を返す。"""
    client = _get_client()
    persona = PERSONAS.get(persona_name, "軽妙で分かりやすい作風。")
    if client is None:
        return _fallback_generate(odai)

    system = (
        "あなたは日本語のなぞかけ（掛け言葉）の名手です。"
        "お題Aに対して『AとかけてBと解く、その心はC』の形で一句作ります。"
        "Bは掛ける対象（短い名詞句）、Cは『どちらも〜』の形でAとBをつなぐ落ち。"
        "同音（ダジャレ）か二重の意味で必ずつなげること。短く、キレよく。"
        f"あなたの作風: {persona}"
    )
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": _GEN_SCHEMA},
                           "effort": EFFORT},
            messages=[{"role": "user", "content": f"お題は「{odai}」。なぞかけを一句どうぞ。"}],
        )
        if resp.stop_reason == "refusal":
            return _fallback_generate(odai)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        b = str(data.get("b", "")).strip()
        c = str(data.get("c", "")).strip()
        if not b or not c:
            return _fallback_generate(odai)
        return {"b": b, "c": c}
    except Exception as e:
        print(f"[ai] 生成失敗（{odai}）: {e}")
        return _fallback_generate(odai)


# ---------- 成立チェック（参考表示） ----------

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["good", "warn", "bad"],
                    "description": "good=きれいに成立 / warn=やや強引 / bad=つながっていない"},
        "reason": {"type": "string", "description": "10〜25字程度の短い理由"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


async def check_nazokake(odai, b, c):
    """『AとかけてBと解く、その心はC』が同音・二重の意味で成立しているかの参考判定。"""
    client = _get_client()
    if client is None or not b or not c:
        return {"verdict": "good", "reason": ""}
    system = (
        "あなたはなぞかけの審査員です。"
        "『AとかけてBと解く、その心はC』が、同音（ダジャレ）または二重の意味で"
        "AとBをちゃんとつないでいるかを判定します。"
        "笑える強引さは許容し、まったく無関係なときだけ bad にします。"
    )
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": _CHECK_SCHEMA},
                           "effort": "low"},
            messages=[{"role": "user",
                       "content": f"A=「{odai}」 B=「{b}」 C=「{c}」。成立していますか？"}],
        )
        if resp.stop_reason == "refusal":
            return {"verdict": "good", "reason": ""}
        text = next((bl.text for bl in resp.content if bl.type == "text"), "")
        data = json.loads(text)
        v = data.get("verdict", "good")
        if v not in ("good", "warn", "bad"):
            v = "good"
        return {"verdict": v, "reason": str(data.get("reason", "")).strip()}
    except Exception as e:
        print(f"[ai] 判定失敗（{odai}）: {e}")
        return {"verdict": "good", "reason": ""}


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
