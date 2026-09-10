// 投稿画面のリアルタイムAI分類プレビュー（仕様書3.8）
// 入力に合わせて AI が推定したジャンルタグを、書いている画面上に常に表示する。
(function () {
  var body = document.getElementById("body");
  if (!body) return;

  var tagList = document.getElementById("ai-tags");
  var hint = document.getElementById("ai-hint");
  var pickNote = document.getElementById("ai-pick-note");
  var tagsJson = document.getElementById("tags-json");
  var aiBody = document.getElementById("ai-body");
  var timer = null;
  var lastText = "";
  var reqId = 0;

  function syncSelected() {
    var picked = [];
    tagList.querySelectorAll(".tag-chip.is-on").forEach(function (c) {
      picked.push({ genre_id: +c.dataset.gid, subgenre_name: c.dataset.sub || null });
    });
    tagsJson.value = picked.length ? JSON.stringify(picked) : "";
  }

  function setHint(text) {
    hint.textContent = text;
    hint.hidden = false;
    if (pickNote) pickNote.hidden = true;
    tagList.innerHTML = "";
    tagsJson.value = "";
    aiBody.value = "";
  }

  function render(text, data) {
    var tags = data.tags || [];
    tagList.innerHTML = "";
    aiBody.value = text;
    if (!tags.length) {
      setHint("うまく分類できませんでした。少し具体的に書くと、AIが提案しやすくなります。");
      aiBody.value = text;
      return;
    }
    hint.hidden = true;
    if (pickNote) pickNote.hidden = false;
    tags.forEach(function (t) {
      var c = document.createElement("button");
      c.type = "button";
      c.className = "tag tag-chip is-on";
      c.dataset.gid = t.genre_id;
      c.dataset.sub = t.subgenre_name || "";
      c.innerHTML = '<span class="tag-g"></span><span class="tag-sub"></span><span class="tag-x">×</span>';
      c.querySelector(".tag-g").textContent = t.genre_name;
      c.querySelector(".tag-sub").textContent = t.subgenre_name ? " / " + t.subgenre_name : "";
      c.addEventListener("click", function () {
        var on = c.classList.toggle("is-on");
        c.classList.toggle("is-off", !on);
        c.querySelector(".tag-x").textContent = on ? "×" : "＋";
        syncSelected();
      });
      tagList.appendChild(c);
    });
    syncSelected();
  }

  function fetchTags() {
    var text = body.value.trim();
    if (text === lastText) return;
    lastText = text;
    if (text.length < 2) {
      setHint("書きはじめると、AIがこの投稿の幸せのジャンルを提案します。");
      return;
    }
    setHint("分類中…");
    var myId = ++reqId;
    fetch("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) { if (myId === reqId) render(text, data); })
      .catch(function () {
        if (myId === reqId) setHint("いまは分類できませんでした（投稿はそのままできます）。");
      });
  }

  body.addEventListener("input", function () {
    if (timer) clearTimeout(timer);
    timer = setTimeout(fetchTags, 500);
  });
  body.addEventListener("blur", fetchTags);

  // 投稿ボタン：送信時に「記録しました」へ切り替えて、少しだけ余韻を見せてから送る
  var form = document.getElementById("compose-form");
  var btn = form && form.querySelector(".btn-primary");
  var LINK_RE = /(https?:\/\/|ftp:\/\/|www\.)\S+|\b[\w.-]+\.(com|net|org|jp|io|co|dev|app|xyz|info|biz|me|tv|link|shop|site|online|ai|gg)\b|\S+@\S+\.\S+/i;
  if (form && btn) {
    form.addEventListener("submit", function (e) {
      if (btn.classList.contains("sent")) return;      // 二重送信ガード
      if (!body.value.trim()) return;                  // 空なら通常のバリデーションに任せる
      if (LINK_RE.test(body.value)) {                  // URL・リンクは投稿不可（サーバー側でも弾く）
        e.preventDefault();
        var hint = form.querySelector(".compose-hint");
        if (hint) { hint.textContent = "URL・リンクは投稿できません。削除してください。"; hint.style.color = "var(--cream-ink)"; }
        body.focus();
        return;
      }
      e.preventDefault();
      btn.classList.add("sent");
      form.classList.add("sending");
      setTimeout(function () { form.submit(); }, 480);
    });
  }
})();


// タイムラインのリアクション: ページをリロード（＝先頭へスクロール）せずに送信する。
// ・リアクションは1投稿1つ。あとから別のものに変えられる（同じものを再度押すと取り消し）。
// ・「今はちがう／比べてしまう」を押すと枠に新しい候補が差し替わり、約5秒だけ「元に戻す」が出る。
(function () {
  var UNDO_MS = 5000;

  document.querySelectorAll("form.react-row").forEach(bindReactForm);

  function bindReactForm(form) {
    if (!form || form.__bound) return;
    form.__bound = true;
    var clicked = null;

    form.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (b) clicked = b;
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = clicked;
      if (!btn) return;

      var card = form.closest(".post");
      var csrf = (form.querySelector("[name=csrf_token]") || {}).value || "";
      var fd = new FormData(form);
      fd.set("kind", btn.value);
      form.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      dropUndoBar(card);

      fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
        body: fd,
      })
        .then(function (r) {
          return r.json()
            .then(function (d) { return { ok: r.ok, d: d || {} }; })
            .catch(function () { return { parseError: true }; });
        })
        .then(function (res) {
          if (res.parseError) throw new Error("parse");
          var d = res.d;

          if (!res.ok || !d.ok) {           // 処理済みのエラー（多重送信など）はリロードしない
            enable(form);
            note(form, d.message || "うまくいきませんでした。");
            return;
          }

          // 差し替えあり（今はちがう／比べてしまう）
          if (d.replacement) {
            swapCard(card, d.replacement, function (newCard) {
              if (d.undoable && d.undo_url) {
                showUndoBar(newCard, d.message, d.undo_url, d.replacement_id, csrf);
              }
            });
            return;
          }

          // 選択状態を更新（変更・取り消しに対応）
          form.querySelectorAll(".react-btn").forEach(function (b) { b.classList.remove("done"); });
          if (!d.cleared) {
            btn.classList.add("done");
            btn.classList.add("pop");
            setTimeout(function () { btn.classList.remove("pop"); }, 350);
            if (btn.value === "共感") heartPop(btn);
          }
          form.classList.toggle("reacted", !d.cleared);
          enable(form);

          if (d.undoable && d.undo_url && !d.cleared) {
            showUndoBar(card, d.message, d.undo_url, d.replacement_id, csrf);
          } else {
            note(form, d.message || "受け取りました。");
          }
        })
        .catch(function () {
          // 通信エラー等：通常のフォーム送信にフォールバック
          var h = document.createElement("input");
          h.type = "hidden"; h.name = "kind"; h.value = btn.value;
          form.appendChild(h);
          form.submit();
        });
    });
  }

  function enable(form) {
    form.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
  }

  function swapCard(oldCard, html, onDone) {
    if (!oldCard) return;
    var tmp = document.createElement("div");
    tmp.innerHTML = (html || "").trim();
    var newCard = tmp.firstElementChild;
    if (!newCard) { oldCard.remove(); return; }
    newCard.classList.add("post-swap-in");
    oldCard.classList.add("post-swap-out");
    setTimeout(function () {
      oldCard.replaceWith(newCard);
      bindReactForm(newCard.querySelector("form.react-row"));
      if (onDone) onDone(newCard);
    }, 220);
  }

  // 「元に戻す」バー（約5秒）。押すと差し替え／リアクションを取り消して元のカードに戻す。
  function dropUndoBar(card) {
    var prev = card && card.previousElementSibling;
    if (prev && prev.classList.contains("undo-bar")) {
      if (prev.__t) clearTimeout(prev.__t);
      prev.remove();
    }
  }

  function showUndoBar(slotEl, msg, undoUrl, replacementId, csrf) {
    dropUndoBar(slotEl);
    var bar = document.createElement("div");
    bar.className = "undo-bar";
    var span = document.createElement("span");
    span.textContent = msg || "受け取りました。";
    var b = document.createElement("button");
    b.type = "button";
    b.className = "undo-btn";
    b.textContent = "元に戻す";
    bar.appendChild(span);
    bar.appendChild(b);
    slotEl.parentNode.insertBefore(bar, slotEl);
    bar.__t = setTimeout(function () { bar.remove(); }, UNDO_MS);

    b.addEventListener("click", function () {
      b.disabled = true;
      clearTimeout(bar.__t);
      var fd = new FormData();
      fd.set("csrf_token", csrf);
      if (replacementId) fd.set("replacement_id", replacementId);
      fetch(undoUrl, { method: "POST", headers: { "X-Requested-With": "fetch" }, body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) { b.disabled = false; return; }
          if (d.card) {
            var tmp = document.createElement("div");
            tmp.innerHTML = d.card.trim();
            var fresh = tmp.firstElementChild;
            if (fresh) {
              slotEl.replaceWith(fresh);
              bindReactForm(fresh.querySelector("form.react-row"));
            }
          } else {
            var f = slotEl.querySelector && slotEl.querySelector("form.react-row");
            if (f) {
              f.classList.remove("reacted");
              f.querySelectorAll(".react-btn").forEach(function (x) {
                x.classList.remove("done"); x.disabled = false;
              });
            }
          }
          bar.remove();
        })
        .catch(function () { b.disabled = false; });
    });
  }

  function note(form, msg) {
    var n = form.nextElementSibling;
    if (!n || !n.classList.contains("react-note")) {
      n = document.createElement("p");
      n.className = "react-note";
      form.parentNode.insertBefore(n, form.nextSibling);
    }
    n.textContent = msg;
  }

  function heartPop(btn) {
    var row = btn.closest(".react-row");
    if (!row) return;
    var span = document.createElement("span");
    span.className = "heart-pop";
    span.textContent = "♥";
    span.style.left = (btn.offsetLeft + btn.offsetWidth / 2) + "px";
    span.style.top = btn.offsetTop + "px";
    row.appendChild(span);
    setTimeout(function () { span.remove(); }, 1000);
  }
})();


// data-confirm 付きフォームは送信前に確認する（インラインJSはCSPで禁止のためここで）
(function () {
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches && form.matches("form[data-confirm]")) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    }
  });
})();
