"use strict";
(function () {
  const stage = document.getElementById("stage");
  const roomTag = document.getElementById("roomTag");

  let ws = null;
  let me = { id: null, isHost: false };
  let roomCode = null;
  let totalRounds = 5;
  let timerId = null;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
  function stopTimer() { if (timerId) { clearInterval(timerId); timerId = null; } }

  function connect(then) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => then && then();
    ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (_) {} };
    ws.onclose = () => { stopTimer(); showDisconnected(); };
  }

  function avatar(p) {
    const color = p.isAI ? "var(--ai)" : "var(--indigo)";
    return `<span class="avatar" style="background:${color}">${esc(p.initial || "?")}</span>`;
  }

  // ---------------- screens ----------------

  function showTitle(err) {
    stopTimer();
    roomTag.hidden = true;
    stage.innerHTML = `
      <div class="panel">
        <p class="eyebrow">4人オンライン対戦</p>
        <h1 class="title">なぞかけ<span class="maru">マニア</span>！</h1>
        <p class="lead">お題に「なぞかけ」で挑む大喜利ゲーム。部屋を立てて友達を招くか、部屋コードで参加。空き枠はAIが埋めます。</p>
        <div class="field" style="max-width:22rem;margin:22px auto 0">
          <label>あなたの名前</label>
          <input id="nm" maxlength="12" placeholder="例：やまだ" autocomplete="off">
        </div>
        <div class="btn-row">
          <button class="btn" id="createBtn" type="button">部屋を立てる</button>
        </div>
        <div style="text-align:center;margin:18px 0 6px;color:var(--ink-soft);font-size:.85rem">— または 部屋コードで参加 —</div>
        <div class="field-inline" style="max-width:22rem;margin:0 auto;justify-content:center">
          <input id="code" maxlength="4" placeholder="ABCD" autocomplete="off"
            style="text-transform:uppercase;font-family:var(--mono);letter-spacing:.3em;text-align:center;max-width:9rem">
          <button class="btn ghost small" id="joinBtn" type="button">参加</button>
        </div>
        <div class="err" id="err">${err ? esc(err) : ""}</div>
      </div>`;
    const nm = document.getElementById("nm");
    nm.focus();
    document.getElementById("createBtn").onclick = () => {
      connect(() => send({ type: "create", name: nm.value.trim() }));
    };
    document.getElementById("joinBtn").onclick = () => {
      const code = document.getElementById("code").value.trim().toUpperCase();
      if (!code) return;
      connect(() => send({ type: "join", code, name: nm.value.trim() }));
    };
  }

  function showDisconnected() {
    stage.innerHTML = `
      <div class="panel" style="text-align:center">
        <p class="eyebrow">接続が切れました</p>
        <h2 style="font-family:var(--serif);font-weight:600">高座を降りました</h2>
        <p class="lead">サーバーとの接続が切れました。</p>
        <div class="btn-row"><button class="btn" id="reBtn" type="button">タイトルへ</button></div>
      </div>`;
    document.getElementById("reBtn").onclick = () => location.reload();
  }

  function lanterns(active) {
    let h = `<div class="rounds">`;
    for (let i = 0; i < totalRounds; i++) {
      const cls = i < active ? "done" : (i === active ? "active" : "");
      h += `<span class="lantern ${cls}"></span>`;
    }
    return h + `</div>`;
  }

  function renderLobby(m) {
    stopTimer();
    totalRounds = m.totalRounds || totalRounds;
    roomCode = m.code;
    roomTag.hidden = false;
    roomTag.textContent = "部屋 " + m.code;
    const rows = m.players.map(p => {
      let tag = "";
      if (p.isHost) tag = `<span class="tag host">親</span>`;
      else if (p.isAI) tag = `<span class="tag ai">AI</span>`;
      else if (!p.connected) tag = `<span class="tag off">オフライン</span>`;
      return `<div class="pl-row">${avatar(p)}<span class="who">${esc(p.name)}</span>${tag}</div>`;
    }).join("");
    const isHost = me.isHost;
    stage.innerHTML = `
      <div class="panel">
        <p class="eyebrow">待合室</p>
        <h2 style="font-family:var(--serif);font-weight:600;text-align:center;margin:2px 0 4px">
          部屋コード <span style="color:var(--ai);font-family:var(--mono);letter-spacing:.2em">${esc(m.code)}</span></h2>
        <p class="hint">このコードを友達に伝えて参加してもらおう（最大4人／全${totalRounds}席）</p>
        <div class="players-list">${rows}</div>
        <div class="btn-row">
          ${isHost && m.canAddAI ? `<button class="btn ghost small" id="aiBtn" type="button">＋ AIを追加</button>` : ""}
          ${isHost ? `<button class="btn" id="startBtn" type="button" ${m.canStart ? "" : "disabled"}>はじめる</button>`
            : `<span class="hint" style="margin:0">親がはじめるのを待っています…</span>`}
        </div>
        <div class="err" id="err"></div>
      </div>`;
    if (isHost) {
      const ai = document.getElementById("aiBtn");
      if (ai) ai.onclick = () => send({ type: "addAI" });
      const st = document.getElementById("startBtn");
      if (st) st.onclick = () => send({ type: "start" });
    }
  }

  function renderRound(m) {
    stopTimer();
    stage.innerHTML = lanterns(m.roundIndex) + `
      <div class="panel">
        <div class="roundbar">
          <span class="lbl">第 ${m.roundIndex + 1} 席 ／ 全 ${m.totalRounds} 席</span>
          <span class="timer" id="timer">${m.seconds}<span class="s">秒</span></span>
        </div>
        <div class="odai"><div class="cap">お 題</div><div class="word">${esc(m.odai)}</div></div>
        <p class="prompt-line">「<b>${esc(m.odai)}</b>」 とかけまして——</p>
        <div class="field">
          <label>… 何 と解く？</label>
          <div class="field-inline">
            <input id="inB" maxlength="24" placeholder="例：入学式" autocomplete="off">
            <span class="fixed">と解く</span>
          </div>
        </div>
        <div class="field">
          <label>その心は？</label>
          <div class="field-inline">
            <span class="fixed">その心は</span>
            <input id="inC" maxlength="40" placeholder="例：どちらも新しい門出" autocomplete="off">
          </div>
        </div>
        <div class="btn-row"><button class="btn" id="subBtn" type="button" disabled>発表する</button></div>
        <p class="hint">全員が発表するか、時間切れで締め切ります。</p>
      </div>`;
    const inB = document.getElementById("inB"), inC = document.getElementById("inC");
    const sub = document.getElementById("subBtn");
    const chk = () => { sub.disabled = !(inB.value.trim() && inC.value.trim()); };
    inB.oninput = chk; inC.oninput = chk; inB.focus();
    inC.addEventListener("keydown", e => { if (e.key === "Enter" && !sub.disabled) doSubmit(); });
    sub.onclick = doSubmit;
    function doSubmit() {
      send({ type: "submit", b: inB.value.trim(), c: inC.value.trim() });
      showWaiting("あなたの発表を受け付けました", "他のプレイヤーの発表を待っています…", m.roundIndex);
    }
    startCountdown(m.seconds);
  }

  function startCountdown(secs) {
    stopTimer();
    let t = secs;
    const el = () => document.getElementById("timer");
    timerId = setInterval(() => {
      t--;
      const e = el();
      if (!e) { stopTimer(); return; }
      e.innerHTML = `${Math.max(0, t)}<span class="s">秒</span>`;
      if (t <= 15) e.classList.add("warn");
      if (t <= 0) stopTimer();
    }, 1000);
  }

  function showWaiting(title, sub, roundIndex) {
    stopTimer();
    stage.innerHTML = lanterns(roundIndex) + `
      <div class="panel" style="text-align:center">
        <p class="eyebrow">${esc(title)}</p>
        <h2 style="font-family:var(--serif);font-weight:600;margin:8px 0 14px">${esc(sub)} <span class="spin"></span></h2>
        <p class="hint">まもなく次のフェーズに進みます。</p>
      </div>`;
  }

  function renderVote(m) {
    stopTimer();
    const cards = m.answers.map((a, i) => `
      <button class="answer ${a.isYou ? "locked" : ""}" data-aid="${a.aid}" data-you="${a.isYou ? 1 : 0}"
        style="animation-delay:${i * 70}ms" type="button">
        <div>「<span class="kake">とかけまして</span> <span class="b">${esc(a.b)}</span> <span class="kake">と解く。</span><br>
        <span class="kake">その心は…</span> <span class="c">${esc(a.c)}</span></div>
        ${a.isYou ? `<span class="mine-tag">あなたの解答</span>` : ""}
      </button>`).join("");
    stage.innerHTML = `
      <div class="panel">
        <div class="roundbar">
          <span class="lbl">発表 ＆ 投票</span>
          <span class="timer" id="timer">${m.seconds}<span class="s">秒</span></span>
        </div>
        <div class="phase-head">
          <h2>一番おもしろいのは？</h2>
          <p>作者は伏せてあります。自分の解答（金枠）には投票できません。</p>
        </div>
        <div class="answers" id="answers">${cards || '<p class="hint">今席は発表がありませんでした。</p>'}</div>
        <div class="btn-row"><button class="btn" id="voteBtn" type="button" disabled>この人に一票</button></div>
      </div>`;
    let picked = null;
    const voteBtn = document.getElementById("voteBtn");
    stage.querySelectorAll(".answer").forEach(el => {
      el.onclick = () => {
        if (el.dataset.you === "1") return;
        stage.querySelectorAll(".answer").forEach(a => a.classList.remove("selected"));
        el.classList.add("selected");
        picked = el.dataset.aid;
        voteBtn.disabled = false;
      };
    });
    voteBtn.onclick = () => {
      if (!picked) return;
      send({ type: "vote", aid: picked });
      showWaiting("投票しました", "みんなの投票を待っています…", m.roundIndex ?? 0);
    };
    startCountdown(m.seconds);
  }

  function badge(v) {
    if (v === "good") return `<span class="badge good">✓ 成立</span>`;
    if (v === "warn") return `<span class="badge warn">△ やや強引</span>`;
    if (v === "bad") return `<span class="badge bad">✕ 不成立</span>`;
    return "";
  }

  function renderReveal(m) {
    stopTimer();
    const cards = m.results.map(r => `
      <div class="answer locked ${r.winner ? "winner" : ""}">
        <div>「<b>${esc(m.odai)}</b> <span class="kake">とかけまして</span> <span class="b">${esc(r.b)}</span>
        <span class="kake">と解く。</span><br><span class="kake">その心は…</span> <span class="c">${esc(r.c)}</span></div>
        <div class="reveal-row">
          <span class="author">${avatar(r)}${esc(r.name)}</span>
          ${badge(r.verdict)}
          <span class="votes-pill">${r.votes} 票</span>
        </div>
        ${r.voters && r.voters.length ? `<div class="hint" style="text-align:left;margin-top:8px">投票：${r.voters.map(esc).join("・")}</div>` : ""}
        ${r.reason ? `<div class="hint" style="text-align:left;margin-top:2px;opacity:.8">判定：${esc(r.reason)}</div>` : ""}
      </div>`).join("");
    const passed = (m.passed || []).map(p =>
      `<div class="hint" style="text-align:left">${esc(p.name)} … パス（時間切れ）</div>`).join("");
    const stand = m.standings.map((s, i) => `
      <div class="stand-row ${s.id === me.id ? "you" : ""} ${s.rank === 1 && s.score > 0 ? "top" : ""}"
        style="animation-delay:${i * 60}ms">
        <span class="rank">${s.rank}</span>${avatar(s)}<span>${esc(s.name)}</span>
        <span class="stand-pts">${s.score}<span class="u">点</span></span>
      </div>`).join("");
    stage.innerHTML = lanterns(m.roundIndex) + `
      <div class="panel">
        <div class="phase-head">
          <p class="eyebrow">開票</p>
          <h2>第 ${m.roundIndex + 1} 席 結果</h2>
          <p>成立チェック（AIの参考判定）も表示します。</p>
        </div>
        <div class="answers">${cards}</div>
        ${passed ? `<div style="margin-top:10px">${passed}</div>` : ""}
        <div class="phase-head" style="margin:26px 0 10px"><h2>暫定ランキング</h2></div>
        <div class="standings">${stand}</div>
        <div class="btn-row">
          ${me.isHost
            ? `<button class="btn" id="nextBtn" type="button">${m.isFinal ? "最終結果へ" : "次の席へ"}</button>`
            : `<span class="hint" style="margin:0">親が進めるのを待っています…</span>`}
        </div>
      </div>`;
    if (me.isHost) {
      const n = document.getElementById("nextBtn");
      if (n) n.onclick = () => { n.disabled = true; send({ type: "next" }); };
    }
  }

  function renderFinal(m) {
    stopTimer();
    const top = m.ranking.filter(r => r.rank === 1 && r.score > 0);
    const champ = top.length ? top.map(r => r.name).join("・") : "引き分け";
    const rows = m.ranking.map((s, i) => `
      <div class="stand-row ${s.id === me.id ? "you" : ""} ${s.rank === 1 && s.score > 0 ? "top" : ""}"
        style="animation-delay:${i * 70}ms;text-align:left">
        <span class="rank">${s.rank}</span>${avatar(s)}<span>${esc(s.name)}</span>
        <span class="stand-pts">${s.score}<span class="u">点</span></span>
      </div>`).join("");
    stage.innerHTML = `
      <div class="panel final-hero">
        <div class="crown">🏮</div>
        <p class="eyebrow">全 ${totalRounds} 席 終了 — 大団円</p>
        <div class="champ">今席の主役は <span class="nm">${esc(champ)}</span></div>
        <div class="standings" style="margin-top:22px;text-align:left">${rows}</div>
        <div class="btn-row">
          ${me.isHost
            ? `<button class="btn" id="againBtn" type="button">もう一席（待合室へ）</button>`
            : `<span class="hint" style="margin:0">お疲れさまでした！</span>`}
        </div>
      </div>`;
    if (me.isHost) {
      const a = document.getElementById("againBtn");
      if (a) a.onclick = () => send({ type: "again" });
    }
  }

  // ---------------- dispatch ----------------
  function handle(m) {
    switch (m.type) {
      case "lobby":
        if (m.you) me = { id: m.you.id, isHost: m.you.isHost };
        renderLobby(m); break;
      case "round": renderRound(m); break;
      case "vote": renderVote(m); break;
      case "reveal": renderReveal(m); break;
      case "final": renderFinal(m); break;
      case "error": {
        const el = document.getElementById("err");
        if (el) el.textContent = m.message;
        else showTitle(m.message);
        break;
      }
    }
  }

  showTitle();
})();
