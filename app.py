# -*- coding: utf-8 -*-
"""ハピディアリー Flaskアプリ本体・ルーティング（仕様書6章 / 7章）

設計原則（仕様書2章）:
  1. 完全匿名   : ログインもハンドルネームも無い。訪問ごとに匿名IDをセッションに発行するだけ。
  2. 数値非表示 : 他者投稿にはリアクション数を出さない。自分の投稿のみ件数を控えめに表示（仕様書6.1）。
  3. 到達の平等 : タイムライン表示時に post_reach を記録し、到達人数を REACH_LIMIT で固定。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import models
from classify import classify, load_taxonomy

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("HAPIDIARY_SECRET_KEY", "happidiary-dev-secret-change-me")
app.config["JSON_AS_ASCII"] = False
# 匿名IDのセッションCookieはブラウザを閉じても消えないようにする（＝同じ端末なら同じ記録に戻れる）。
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("HAPIDIARY_HTTPS_COOKIE") == "1":  # 本番(HTTPS)でのみ有効化
    app.config["SESSION_COOKIE_SECURE"] = True
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # 巨大POSTを弾く（本文は数百字の想定）

# 投稿本文の上限・URL/リンク検出（スパムや誘導リンク対策）
BODY_MAX_LEN = 500
POST_WINDOW_SEC = 120   # この秒数内に
POST_WINDOW_MAX = 4     # これ以上は投稿させない（連投・量産対策）
_URL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)+\.(?:com|net|org|jp|io|co|dev|app|xyz|info|biz|me|tv|link|shop|site|online|ai|gg)\b"
    r"|\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
    re.IGNORECASE,
)


def contains_link(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))


# スキーマが無い状態で起動しても落ちないよう、import時に用意する（CREATE TABLE IF NOT EXISTS）。
# デモデータの投入は init_db.py の役割。
with models.get_db() as _boot:
    models.init_schema(_boot)
_boot.close()


# ---------------------------------------------------------------------------
# DB接続（リクエストごと）
# ---------------------------------------------------------------------------

@app.before_request
def _open_db():
    g.db = models.get_db()


@app.before_request
def _persist_session():
    # どのページから入っても Cookie を永続化する（/login に直接来た場合など）。
    session.permanent = True


@app.teardown_request
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# セキュリティレスポンスヘッダ（XSS・クリックジャッキング・MIMEスニッフィング対策）
# ---------------------------------------------------------------------------

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# ---------------------------------------------------------------------------
# CSRF対策（追加依存なしの最小実装）。全 POST フォームで検証する。
# ---------------------------------------------------------------------------

CSRF_EXEMPT = {"/api/classify"}  # 副作用のない同一オリジンのJSON取得のみ除外


def _csrf_secret() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def _csrf_protect():
    if request.method != "POST" or request.path in CSRF_EXEMPT:
        return
    sent = request.form.get("csrf_token", "")
    if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "フォームの有効期限が切れました。もう一度お試しください。")


# ---------------------------------------------------------------------------
# 閲覧数トラッキング（デバッグモードで見る用・meta テーブルに集計）
# ---------------------------------------------------------------------------

PAGE_PATHS = {"/", "/welcome", "/post", "/analysis", "/archive", "/settings", "/login", "/signup"}


def _today() -> str:
    return f"{datetime.now(timezone.utc):%Y-%m-%d}"


@app.before_request
def _count_pageview():
    if request.method != "GET":
        return
    p = request.path
    if (p.startswith("/static/") or p.startswith("/api/") or p.startswith("/debug")
            or p in ("/healthz", "/favicon.ico", "/reset-me")):
        return
    try:
        today = _today()
        models.bump_meta_int(g.db, "pv:total", 1)
        models.bump_meta_int(g.db, f"pv:day:{today}:views", 1)
        models.bump_meta_int(g.db, "pv:path:" + (p if p in PAGE_PATHS else "/other"), 1)
        if session.get("pv_day") != today:      # 1セッション＝その日1回だけ訪問者カウント
            session["pv_day"] = today
            models.bump_meta_int(g.db, f"pv:day:{today}:visitors", 1)
            models.bump_meta_int(g.db, "pv:visitors_total", 1)
    except Exception:
        pass  # 集計失敗でページ表示は止めない


def current_user():
    """セッションの匿名IDからユーザーを取得。無ければ発行する（完全匿名）。"""
    anon = session.get("anon_id")
    user = models.get_user_by_anon(g.db, anon) if anon else None
    if user is None:
        user = models.create_user(g.db)
        session["anon_id"] = user["anon_id"]
        session.permanent = True
    return user


def login_session(user) -> None:
    """セッション固定化対策としてトークンを入れ替えてからログイン状態にする。"""
    keep_csrf = session.get("_csrf")
    session.clear()
    session["anon_id"] = user["anon_id"]
    session["_csrf"] = keep_csrf or secrets.token_urlsafe(32)
    session.permanent = True


# ---------------------------------------------------------------------------
# テンプレート用ヘルパ
# ---------------------------------------------------------------------------

TAXONOMY = load_taxonomy()
GENRES = TAXONOMY["genres"]
GENRE_BY_ID = {g["id"]: g for g in GENRES}
MAJORS = TAXONOMY["major_categories"]
MUTE_THEMES = TAXONOMY["mute_themes"]


@app.context_processor
def _inject():
    logged_in = False
    unseen_notif = 0
    anon = session.get("anon_id")
    if anon and getattr(g, "db", None) is not None:
        u = models.get_user_by_anon(g.db, anon)
        if u is not None:
            logged_in = models.has_login(u)
            unseen_notif = models.unseen_notif_count(g.db, u["id"])
    return {"APP_NAME": "ハピディアリー", "csrf_token": _csrf_secret(),
            "logged_in": logged_in, "unseen_notif": unseen_notif}


@app.template_filter("since")
def _since(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days <= 0:
        h = delta.seconds // 3600
        if h <= 0:
            return "さっき"
        return f"{h}時間前"
    if days == 1:
        return "きのう"
    if days < 30:
        return f"{days}日前"
    if days < 365:
        return f"{days // 30}か月前"
    y = days // 365
    return f"およそ{y}年前"


def _day_seed(user_id: int, salt: str = "") -> int:
    key = f"{user_id}:{datetime.now(timezone.utc):%Y-%m-%d}:{salt}"
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)


def post_view(row, *, viewer_id: int, own: bool) -> dict:
    """1投稿をテンプレート表示用の辞書にまとめる。"""
    tags = models.get_tags(g.db, row["id"])
    counts = models.reaction_counts(g.db, row["id"])
    mine = models.my_reactions(g.db, row["id"], viewer_id)
    return {
        "id": row["id"],
        "body": row["body"],
        "attachment": row["attachment"],
        "sensitive": bool(row["sensitive"]),
        "created_at": row["created_at"],
        "own": own,
        "tags": [
            {
                "major": t["major"],
                "genre_id": t["genre_id"],
                "genre_name": t["genre_name"],
                "subgenre_name": t["subgenre_name"],
            }
            for t in tags
        ],
        # 数値非表示の原則: 自分の投稿だけ「共感／発見」を控えめに、他者投稿では出さない
        "own_counts": {"共感": counts["共感"], "発見": counts["発見"]} if own else None,
        "my_reactions": sorted(mine),
    }


# ---------------------------------------------------------------------------
# オンボーディング（仕様書3.3: 初期設定で共感したいジャンルを選ぶ）
# ---------------------------------------------------------------------------

@app.route("/welcome", methods=["GET", "POST"])
def welcome():
    user = current_user()
    if request.method == "POST":
        picked = request.form.getlist("genre")
        prefs = {int(gid): "普通" for gid in picked}
        if not prefs:  # 最低限のデフォルト（何も選ばなければ「暮らし・日常」中心）
            prefs = {6: "普通", 9: "普通", 10: "普通", 16: "普通", 30: "普通"}
        models.set_genre_prefs(g.db, user["id"], prefs)
        attrs = {
            "age_band": request.form.get("age_band", "") or "未設定",
            "household": request.form.get("household", "") or "未設定",
        }
        models.set_attributes(g.db, user["id"], attrs)
        muted = request.form.getlist("mute")
        models.set_mutes(g.db, user["id"], muted)
        models.mark_onboarded(g.db, user["id"])
        flash("ようこそ。いつでも幸せを記録できます。", "ok")
        return redirect(url_for("home"))
    return render_template(
        "welcome.html", genres=GENRES, majors=MAJORS, mute_themes=MUTE_THEMES,
        age_bands=["10代", "20代", "30代", "40代", "50代", "60代以上", "未設定"],
        households=["ひとり暮らし", "パートナーと二人", "子どもがいる", "実家暮らし", "その他", "未設定"],
    )


def _ensure_onboarded(user):
    if not user["onboarded"]:
        return redirect(url_for("welcome"))
    return None


# ---------------------------------------------------------------------------
# Home / タイムライン（仕様書3.1, 3.3, 3.4, 3.6, 6.1）
# ---------------------------------------------------------------------------

def _timeline_candidates(user, *, exclude_reached_today: bool = False):
    """タイムラインに出せる候補を [(weight, row), ...] で返す。selected も返す。"""
    prefs = models.get_genre_prefs(g.db, user["id"])
    selected = {gid: s for gid, s in prefs.items() if models.STRENGTH_WEIGHT.get(s, 0) > 0}
    if not selected:
        return [], selected
    muted = set(models.get_mutes(g.db, user["id"]))
    today = f"{datetime.now(timezone.utc):%Y-%m-%d}"

    # 「比べてしまう」による棲み分け（仕様書3.4）
    blocked = models.compare_blocked_authors(g.db, user["id"])
    compare_pool = models.compare_user_ids(g.db)
    viewer_is_compare = user["id"] in compare_pool

    rows = g.db.execute(
        """
        SELECT p.*, GROUP_CONCAT(DISTINCT t.genre_id) AS gids,
                    GROUP_CONCAT(DISTINCT IFNULL(t.mute_theme,'')) AS mutes
        FROM posts p
        JOIN post_tags t ON t.post_id = p.id
        WHERE p.author_id != ?
        GROUP BY p.id
        """,
        (user["id"],),
    ).fetchall()

    candidates = []
    for r in rows:
        if r["author_id"] in blocked:
            continue  # 「比べてしまう」で相互ブロックした相手は表示しない
        gids = {int(x) for x in (r["gids"] or "").split(",") if x}
        match = gids & set(selected)
        if not match:
            continue
        post_mutes = {m for m in (r["mutes"] or "").split(",") if m}
        if post_mutes & muted:
            continue  # ミュートテーマに触れる投稿は表示しない
        got_today = g.db.execute(
            "SELECT 1 FROM post_reach WHERE post_id=? AND user_id=? AND substr(reached_at,1,10)=?",
            (r["id"], user["id"], today),
        ).fetchone()
        # 到達の平等: すでに固定人数に届いた投稿は新たに流さない（今日受け取り済みは継続表示）
        if models.reach_count(g.db, r["id"]) >= models.REACH_LIMIT and not got_today:
            continue
        # 過去に（今日より前に）受け取った投稿はもう流さない
        reached_before = g.db.execute(
            "SELECT 1 FROM post_reach WHERE post_id=? AND user_id=? AND substr(reached_at,1,10) < ?",
            (r["id"], user["id"], today),
        ).fetchone()
        if reached_before:
            continue
        # 差し替え（C案）では、今日すでに画面に出したものは候補から外す
        if exclude_reached_today and got_today:
            continue
        weight = max(models.STRENGTH_WEIGHT[selected[gid]] for gid in match)
        # マウント同士は見せ合い、一般ユーザーの流れからは遠ざける（棲み分け・仕様書3.4）
        author_is_compare = r["author_id"] in compare_pool
        if viewer_is_compare and author_is_compare:
            weight *= 2.0
        elif not viewer_is_compare and author_is_compare:
            weight *= 0.35
        candidates.append((weight, r))
    return candidates, selected


def build_timeline(user) -> list[dict]:
    candidates, selected = _timeline_candidates(user)
    if not selected:
        return []

    # バズを避けるため、1日分は日付固定シードでランダムに10件程度選ぶ（仕様書3.3）
    rng = random.Random(_day_seed(user["id"], "timeline"))
    rng.shuffle(candidates)
    candidates.sort(key=lambda x: x[0] + rng.random(), reverse=True)
    chosen = [r for _, r in candidates[: models.TIMELINE_DAILY_LIMIT]]

    views = []
    for r in chosen:
        models.mark_reached(g.db, r["id"], user["id"])  # 到達を記録
        views.append(post_view(r, viewer_id=user["id"], own=False))
    return views


def resurfaced_post(user):
    """過去の自分の投稿をランダムに1件再浮上させる（仕様書3.6）。"""
    rows = models.own_posts(g.db, user["id"], limit=500)
    old = [r for r in rows if (datetime.now(timezone.utc) - datetime.fromisoformat(r["created_at"]).replace(tzinfo=timezone.utc)).days >= 120]
    if not old:
        return None
    rng = random.Random(_day_seed(user["id"], "resurface"))
    return post_view(rng.choice(old), viewer_id=user["id"], own=True)


@app.route("/")
def home():
    # ヘルスチェック等の HEAD リクエストで匿名ユーザーを増やさない
    if request.method == "HEAD":
        return ""
    user = current_user()
    r = _ensure_onboarded(user)
    if r:
        return r

    own = [post_view(p, viewer_id=user["id"], own=True) for p in models.own_posts(g.db, user["id"], limit=30)]
    timeline = build_timeline(user)

    return render_template(
        "home.html",
        own_posts=own,
        timeline=timeline,
        resurfaced=resurfaced_post(user),
        reach_limit=models.REACH_LIMIT,
        stats=models.posting_stats(g.db, user["id"]),
        just_posted=request.args.get("posted") == "1",
    )


@app.route("/notifications")
def notifications():
    user = current_user()
    r = _ensure_onboarded(user)
    if r:
        return r
    feed = models.notification_feed(g.db, user["id"])
    models.mark_notifications_seen(g.db, user["id"])  # 開いたら既読
    return render_template("notifications.html", feed=feed)


# ---------------------------------------------------------------------------
# 投稿画面（仕様書3.8, 6.2）
# ---------------------------------------------------------------------------

@app.route("/post", methods=["GET", "POST"])
def post():
    user = current_user()
    r = _ensure_onboarded(user)
    if r:
        return r

    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if not body:
            flash("幸せを一言だけでも書いてみてください。", "warn")
            return redirect(url_for("post"))
        if len(body) > BODY_MAX_LEN:
            flash(f"本文は{BODY_MAX_LEN}文字までにしてください。", "warn")
            return render_template("post.html", classify_mode=_classify_mode_label(), body=body)
        if contains_link(body):
            flash("URL・リンクは投稿できません。今日の小さな幸せを言葉で書いてみてください。", "warn")
            return render_template("post.html", classify_mode=_classify_mode_label(),
                                   body=_URL_RE.sub("", body).strip())

        # 連投・重複対策（AIや自動化での量産を防ぐ）
        recent = models.own_posts(g.db, user["id"], limit=5)
        if any((r["body"] or "").strip() == body for r in recent[:3]):
            flash("さっきと同じ内容です。少し言葉を変えて書いてみてください。", "warn")
            return render_template("post.html", classify_mode=_classify_mode_label(), body=body)
        since = (datetime.now(timezone.utc) - timedelta(seconds=POST_WINDOW_SEC)).isoformat(timespec="seconds")
        if sum(1 for r in recent if (r["created_at"] or "") > since) >= POST_WINDOW_MAX:
            flash("投稿が続いています。少し時間をおいてからにしましょう。", "warn")
            return render_template("post.html", classify_mode=_classify_mode_label(), body=body)

        # 投稿画面でユーザーが確認・取捨選択したタグがあればそれを使う（3.8）。
        # 本文がプレビュー時から変わっていたら信用せず、サーバー側で分類し直す。
        picked = None
        if (request.form.get("ai_body") or "").strip() == body:
            picked = _resolve_picked_tags(request.form.get("tags_json") or "")

        if picked is not None:
            tags = picked
        else:
            result = classify(body)
            _record_classify(result)
            tags = result["tags"]

        post_id = models.create_post(
            g.db, author_id=user["id"], body=body, tags=tags,
        )
        models.mark_reached(g.db, post_id, user["id"])  # 自分には必ず届いている
        st = models.posting_stats(g.db, user["id"])
        if st["streak"] >= 2:
            flash(f"投稿しました。{st['streak']}日連続で記録中です。", "ok")
        else:
            flash("投稿しました。だれかのところへ、ゆっくり届きます。", "ok")
        return redirect(url_for("home", posted=1))

    return render_template("post.html", classify_mode=_classify_mode_label())


@app.route("/post/<int:post_id>/delete", methods=["POST"])
def delete_post(post_id):
    user = current_user()
    ok = models.delete_post(g.db, post_id, user["id"])
    flash("投稿を削除しました。" if ok else "その投稿は削除できません。", "ok" if ok else "warn")
    dest = request.form.get("next", "")
    if not (dest.startswith("/") and not dest.startswith("//")):
        dest = url_for("home")
    return redirect(dest)


def _resolve_picked_tags(tags_json: str):
    """投稿画面で選ばれた [{genre_id, subgenre_name}] を分類マスタで検証してタグ辞書にする。
    有効なデータが1つも無ければ None（→ サーバーで分類し直す）。空リストは「全部外した」意図。"""
    if not tags_json:
        return None
    try:
        raw = json.loads(tags_json)
        if not isinstance(raw, list):
            return None
    except (ValueError, TypeError):
        return None
    out = []
    for item in raw[:3]:
        try:
            gid = int(item.get("genre_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        gr = GENRE_BY_ID.get(gid)
        if not gr:
            continue
        sub_name = item.get("subgenre_name") or None
        sub_meta = next((s for s in gr["subgenres"] if s["name"] == sub_name), None)
        out.append({
            "major": gr["major"],
            "genre_id": gid,
            "genre_name": gr["name"],
            "subgenre_name": sub_meta["name"] if sub_meta else None,
            "confidence": 1.0,  # 本人が確認したタグ
            "mute_theme": (sub_meta or {}).get("mute_theme"),
        })
    return out  # [] も有効（全部外した）


def _classify_mode_label() -> str:
    from classify import available_mode, _preview_model
    m = available_mode()
    if m == "embed":
        return "AI（埋め込み検索）"
    if m == "llm":
        return f"AI・{_preview_model()}"
    return "キーワード"


def _record_classify(result: dict) -> None:
    """デバッグモード用に分類の利用量を meta テーブルへ記録する。"""
    try:
        db = g.db
        method = (result.get("method") or "none").split("(")[0].strip() or "none"
        today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
        toks = int(result.get("tokens_est") or 0)
        calls = int(result.get("api_calls") or 0)
        models.bump_meta_int(db, "dbg:calls:total", 1)
        models.bump_meta_int(db, f"dbg:calls:{method}", 1)
        models.bump_meta_int(db, "dbg:api_calls:total", calls)
        models.bump_meta_int(db, "dbg:tokens_est:total", toks)
        models.bump_meta_int(db, f"dbg:day:{today}:calls", 1)
        models.bump_meta_int(db, f"dbg:day:{today}:api_calls", calls)
        models.bump_meta_int(db, f"dbg:day:{today}:tokens_est", toks)
        models.set_meta(db, "dbg:last", json.dumps({
            "at": now_iso_(), "method": result.get("method"), "model": result.get("model"),
            "tags": len(result.get("tags") or []), "tokens_est": toks,
            "latency_ms": result.get("latency_ms"), "top_score": result.get("top_score"),
            "error": result.get("error"),
        }, ensure_ascii=False))
        if result.get("error"):
            models.set_meta(db, "dbg:last_error", json.dumps({
                "at": now_iso_(), "method": result.get("method"), "error": result.get("error"),
            }, ensure_ascii=False))
    except Exception:
        pass  # 記録失敗で分類・投稿を止めない


def now_iso_() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# /api/classify は未ログインでも叩けて外部AIを呼ぶので、セッション単位で薄くレート制限する
# （worker=1 前提の簡易な in-process 実装）。
_CLASSIFY_HITS: dict[str, list[float]] = {}
_CLASSIFY_WINDOW = 60.0
_CLASSIFY_MAX = 25


def _classify_rate_ok(key: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _CLASSIFY_HITS.get(key, []) if now - t < _CLASSIFY_WINDOW]
    if len(_CLASSIFY_HITS) > 2000:            # メモリ暴走の保険
        _CLASSIFY_HITS.clear()
    hits.append(now)
    _CLASSIFY_HITS[key] = hits
    return len(hits) <= _CLASSIFY_MAX


@app.route("/api/classify", methods=["POST"])
def api_classify():
    """投稿画面のリアルタイム分類プレビュー（仕様書3.8）。"""
    user = current_user()
    if not _classify_rate_ok(session.get("anon_id") or request.remote_addr or "?"):
        return jsonify({"tags": [], "sensitive": False, "mute_candidates": [],
                        "method": "none", "error": "rate_limited"}), 429
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()[:BODY_MAX_LEN]  # 過大入力で外部AIコストを膨らませない
    if len(text) < 2:
        return jsonify({"tags": [], "sensitive": False, "mute_candidates": [], "method": "none"})
    result = classify(text, preview=True)
    _record_classify(result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# リアクション（仕様書3.4: 4種のみ / 数値化しない）
# ---------------------------------------------------------------------------

REACTION_EFFECT = {
    "共感": +1,      # 同様のジャンルの表示を強化
    "今はちがう": -1,  # 類似投稿の表示を減らす
}
_STRENGTH_ORDER = ["見ない", "少なめ", "普通", "よく見たい"]


def _nudge_strength(user_id: int, genre_id: int, direction: int):
    prefs = models.get_genre_prefs(g.db, user_id)
    cur = prefs.get(genre_id, "普通")
    idx = _STRENGTH_ORDER.index(cur) if cur in _STRENGTH_ORDER else 2
    idx = max(1, min(len(_STRENGTH_ORDER) - 1, idx + direction))  # 「見ない」までは自動で下げない
    prefs[genre_id] = _STRENGTH_ORDER[idx]
    models.set_genre_prefs(g.db, user_id, prefs)


REACTION_MESSAGES = {
    "共感": "そっと共感を伝えました。",
    "発見": "あたらしい気づきを受け取りました。",
    "今はちがう": "受け取りました。似た投稿は少し控えめにします。",
    "比べてしまう": "受け取りました。同じ気持ちの人とつながれるようにします。",
}


def _wants_json() -> bool:
    return (request.headers.get("X-Requested-With") == "fetch"
            or "application/json" in request.headers.get("Accept", ""))


# 「今はちがう」「比べてしまう」を押した投稿は、その枠に新しい候補を1件差し込む（C案）
REPLACE_KINDS = {"今はちがう", "比べてしまう"}


def _apply_nudge(user_id: int, post_id: int, kind: str, sign: int = 1):
    """kind に応じてジャンルの表示強度を寄せる。sign=-1 で取り消し（元に戻す）。"""
    if kind not in REACTION_EFFECT:
        return
    for t in models.get_tags(g.db, post_id):
        _nudge_strength(user_id, t["genre_id"], sign * REACTION_EFFECT[kind])


def _pick_replacement(user):
    """タイムラインの差し替え用に、まだ今日出していない候補を重みつきランダムで1件選ぶ。
    無ければ None。選んだものは to_reach として表示済み扱いにする。
    返り値: (レンダリング済みHTML, その投稿ID) または None。"""
    candidates, selected = _timeline_candidates(user, exclude_reached_today=True)
    if not selected or not candidates:
        return None
    rng = random.Random()  # 差し替えは都度なので固定シードにしない
    total = sum(w for w, _ in candidates) or 1.0
    pick, acc = rng.random() * total, 0.0
    chosen = candidates[-1][1]
    for w, r in candidates:
        acc += w
        if acc >= pick:
            chosen = r
            break
    models.mark_reached(g.db, chosen["id"], user["id"])
    v = post_view(chosen, viewer_id=user["id"], own=False)
    return render_template("_one_card.html", p=v), chosen["id"]


@app.route("/react/<int:post_id>", methods=["POST"])
def react(post_id):
    user = current_user()
    anchor = f"{url_for('home')}#p{post_id}"

    def done(ok: bool, message: str, status: int = 200,
             redirect_to: str | None = None, **extra):
        if _wants_json():
            return jsonify({"ok": ok, "message": message, **extra}), status
        if message:
            flash(message, "ok" if ok else "warn")
        return redirect(redirect_to or anchor)

    kind = request.form.get("kind", "")
    if kind not in models.REACTION_KINDS:
        return done(False, "そのリアクションは選べません。", 400)

    post_row = models.get_post(g.db, post_id)
    if not post_row or post_row["author_id"] == user["id"]:
        return done(False, "", 404)

    prev = models.single_reaction(g.db, post_id, user["id"])

    # 同じものをもう一度 → 取り消し（途中で「なし」に変えられる）
    if prev == kind:
        models.clear_reaction(g.db, post_id, user["id"])
        _apply_nudge(user["id"], post_id, prev, sign=-1)
        return done(True, "リアクションを取り消しました。", replacement=None, cleared=True)

    # 付け替え（別のリアクションに変更）または新規
    models.set_reaction(g.db, post_id, user["id"], kind)
    if prev:
        _apply_nudge(user["id"], post_id, prev, sign=-1)  # 前のぶんを打ち消す
    _apply_nudge(user["id"], post_id, kind, sign=1)

    replacement, replacement_id = None, None
    if kind in REPLACE_KINDS and _wants_json():
        picked = _pick_replacement(user)  # set_reaction 後なので棲み分けブロックも反映済み
        if picked:
            replacement, replacement_id = picked

    return done(
        True, REACTION_MESSAGES.get(kind, "受け取りました。"),
        replacement=replacement,
        undoable=(kind in REPLACE_KINDS),
        undo_url=(url_for("react_undo", post_id=post_id) if kind in REPLACE_KINDS else None),
        replacement_id=replacement_id,
        kind=kind,
    )


@app.route("/react/<int:post_id>/undo", methods=["POST"])
def react_undo(post_id):
    """「今はちがう」「比べてしまう」を押した直後に元に戻す（数秒間だけ画面に出るボタン）。"""
    user = current_user()
    prev = models.single_reaction(g.db, post_id, user["id"])
    if prev not in REPLACE_KINDS:
        return jsonify({"ok": False, "message": "元に戻せませんでした。"}), 409
    models.clear_reaction(g.db, post_id, user["id"])
    _apply_nudge(user["id"], post_id, prev, sign=-1)
    try:
        rid = int(request.form.get("replacement_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    if rid:
        models.unmark_reached(g.db, rid, user["id"])  # 差し替えで消費した到達枠を返す
    row = models.get_post(g.db, post_id)
    card = None
    if row:
        card = render_template(
            "_one_card.html", p=post_view(row, viewer_id=user["id"], own=False))
    return jsonify({"ok": True, "message": "元に戻しました。", "card": card})


# ---------------------------------------------------------------------------
# 分析・傾向 + 未来の幸せ提案（仕様書3.7, 6.3）
# ---------------------------------------------------------------------------

PERIODS = {
    "now": ("いま", 0, 365),
    "5y": ("5年前", 365, 365 * 6),
    "10y": ("10年前", 365 * 6, 100000),
}


def genre_breakdown(user_id: int, min_days: int, max_days: int) -> list[dict]:
    rows = g.db.execute(
        """
        SELECT t.genre_id, t.genre_name, t.major,
               julianday('now') - julianday(p.created_at) AS age_days
        FROM posts p JOIN post_tags t ON t.post_id = p.id
        WHERE p.author_id = ?
        """,
        (user_id,),
    ).fetchall()
    counter = Counter()
    for r in rows:
        if min_days <= (r["age_days"] or 0) < max_days:
            counter[(r["genre_id"], r["genre_name"], r["major"])] += 1
    total = sum(counter.values())
    if not total:
        return []
    out = [
        {"genre_id": gid, "genre_name": name, "major": major,
         "count": c, "pct": round(c * 100 / total)}
        for (gid, name, major), c in counter.most_common()
    ]
    return out


def similar_user_trend(user, exclude_genre: int | None = None):
    """属性・幸福ジャンルの傾向が似たユーザーの、いま感じている幸せの傾向（仕様書3.7）。
    個人は特定しない。属性の傾向と匿名集計のみを使う。exclude_genre はそのジャンルを候補から外す。"""
    my = genre_breakdown(user["id"], 0, 100000)
    my_genres = {row["genre_id"] for row in my[:6]}
    my_attr = _attrs(user)

    peers = g.db.execute("SELECT id, attributes FROM users WHERE id != ?", (user["id"],)).fetchall()
    peer_ids = []
    for p in peers:
        pg = {r["genre_id"] for r in genre_breakdown(p["id"], 0, 100000)}
        import json as _json
        pa = _json.loads(p["attributes"] or "{}")
        shared_genre = len(my_genres & pg)
        shared_attr = sum(1 for k in ("age_band", "household") if my_attr.get(k) and my_attr.get(k) == pa.get(k))
        if shared_genre >= 1 or shared_attr >= 1:
            peer_ids.append(p["id"])

    trend = Counter()
    for pid in peer_ids:
        for r in genre_breakdown(pid, 0, 365):
            trend[(r["genre_id"], r["genre_name"])] += r["count"]

    # 自分がいま少ない / 経験していないジャンルで、似た人がいま感じているもの
    my_now = {row["genre_id"]: row["count"] for row in genre_breakdown(user["id"], 0, 365)}
    ranked = [kv for kv in sorted(trend.items(), key=lambda kv: kv[1], reverse=True)
              if kv[0][0] != exclude_genre]
    for (gid, name), _cnt in ranked:
        if my_now.get(gid, 0) == 0:
            return gid, name, len(peer_ids)
    if ranked:
        (gid, name), _ = ranked[0]
        return gid, name, len(peer_ids)
    return None, None, len(peer_ids)


def _attrs(user) -> dict:
    import json as _json
    return _json.loads(user["attributes"] or "{}")


# 提案は日替わりにせず「すっと置いておく」。次のどれかで新しくする:
#  ・「興味なし」→ そのジャンルを避けて差し替え
#  ・提案されたジャンルで実際に投稿した（達成）→ 次の提案へ
#  ・7日以上たった → 直前と同じジャンルを避けて更新
SUGGESTION_MAX_AGE_DAYS = 7


def _jst_date():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def _make_suggestion(user, exclude_genre: int | None = None):
    gid, gname, n_peers = similar_user_trend(user, exclude_genre=exclude_genre)
    if not gid:
        return None  # まだ提案できるほどデータがない
    genre = GENRE_BY_ID[gid]
    rng = random.Random(_day_seed(user["id"], f"sugg:{gid}"))
    sub = rng.choice(genre["subgenres"])
    content = f"「{genre['name']}」の幸せ、たとえば“{sub['name']}”。{sub['description']}。"
    models.add_suggestion(
        g.db, user["id"], content=content, based_on_genre_id=gid,
        source_profile={"attr": _attrs(user), "peers": n_peers, "genre": genre["name"]},
    )
    return models.latest_suggestion(g.db, user["id"])


def get_suggestion(user):
    latest = models.latest_suggestion(g.db, user["id"])
    if latest is None:
        return _make_suggestion(user)

    gid = latest["based_on_genre_id"]
    created = datetime.fromisoformat(latest["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (_jst_date() - (created + timedelta(hours=9)).date()).days

    if latest["feedback"] == "興味なし":
        return _make_suggestion(user, exclude_genre=gid) or latest
    if gid and models.posted_in_genre_since(g.db, user["id"], gid, latest["created_at"]):
        flash(f"「{GENRE_BY_ID[gid]['name']}」の幸せ、ちゃんと感じられましたね。次の提案です。", "ok")
        return _make_suggestion(user, exclude_genre=gid) or latest
    if age_days >= SUGGESTION_MAX_AGE_DAYS:
        return _make_suggestion(user, exclude_genre=gid) or latest
    return latest  # それ以外は同じ提案をそのまま置いておく


@app.route("/analysis")
def analysis():
    user = current_user()
    r = _ensure_onboarded(user)
    if r:
        return r
    period = request.args.get("period", "now")
    if period not in PERIODS:
        period = "now"
    label, lo, hi = PERIODS[period]
    breakdown = genre_breakdown(user["id"], lo, hi)
    suggestion = get_suggestion(user)
    return render_template(
        "analysis.html",
        period=period, period_label=label, periods=PERIODS,
        breakdown=breakdown,
        suggestion=suggestion,
    )


@app.route("/suggestion/feedback", methods=["POST"])
def suggestion_feedback():
    user = current_user()
    sid = request.form.get("suggestion_id", type=int)
    fb = request.form.get("feedback", "")
    if fb in ("気になる", "興味なし") and sid:
        row = g.db.execute("SELECT * FROM suggestions WHERE id=? AND user_id=?", (sid, user["id"])).fetchone()
        if row:
            models.set_suggestion_feedback(g.db, sid, fb)
            flash("「気になる」で受け取りました。しばらくこの提案を置いておきます。" if fb == "気になる"
                  else "受け取りました。別の提案に変えます。", "ok")
    return redirect(url_for("analysis"))


# ---------------------------------------------------------------------------
# 記録アーカイブ（仕様書6.4）
# ---------------------------------------------------------------------------

@app.route("/archive")
def archive():
    user = current_user()
    r = _ensure_onboarded(user)
    if r:
        return r
    genre_id = request.args.get("genre", type=int)
    rows = models.own_posts(g.db, user["id"], limit=500)
    views = [post_view(p, viewer_id=user["id"], own=True) for p in rows]
    if genre_id:
        views = [v for v in views if any(t["genre_id"] == genre_id for t in v["tags"])]
    used_ids = sorted({t["genre_id"] for v in
                       [post_view(p, viewer_id=user["id"], own=True) for p in rows]
                       for t in v["tags"]})
    used_genres = [GENRE_BY_ID[i] for i in used_ids if i in GENRE_BY_ID]
    return render_template(
        "archive.html", posts=views, used_genres=used_genres, active_genre=genre_id,
    )


# ---------------------------------------------------------------------------
# 設定（読みたい設定 / ミュート設定 / 属性タグ。仕様書4.1, 5章）
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    user = current_user()
    if request.method == "POST":
        prefs = {}
        for gr in GENRES:
            val = request.form.get(f"strength_{gr['id']}", "見ない")
            if val in models.STRENGTH_WEIGHT and val != "見ない":
                prefs[gr["id"]] = val
        models.set_genre_prefs(g.db, user["id"], prefs)
        models.set_mutes(g.db, user["id"], request.form.getlist("mute"))
        models.set_attributes(g.db, user["id"], {
            "age_band": request.form.get("age_band", "未設定"),
            "household": request.form.get("household", "未設定"),
        })
        if not user["onboarded"]:
            models.mark_onboarded(g.db, user["id"])
        flash("設定を保存しました。", "ok")
        return redirect(url_for("settings"))

    prefs = models.get_genre_prefs(g.db, user["id"])
    return render_template(
        "settings.html",
        genres=GENRES, majors=MAJORS, mute_themes=MUTE_THEMES,
        prefs=prefs, mutes=set(models.get_mutes(g.db, user["id"])),
        attrs=_attrs(user), strengths=models.READ_STRENGTHS,
        has_account=models.has_login(user),
        age_bands=["10代", "20代", "30代", "40代", "50代", "60代以上", "未設定"],
        households=["ひとり暮らし", "パートナーと二人", "子どもがいる", "実家暮らし", "その他", "未設定"],
    )


# ---------------------------------------------------------------------------
# ログイン（任意機能）
#   仕様書の「完全匿名」を保つため、ログインは復旧・複数端末用にとどめる。
#   ユーザー名は画面に一切出さず、DBには鍵付きハッシュのみ保存（models参照）。
# ---------------------------------------------------------------------------

def _validate_credentials(username: str, password: str, password2: str | None = None) -> str | None:
    if not re.match(models.USERNAME_RE, username or ""):
        return "ユーザー名は日本語（ひらがな・カタカナ・漢字）または半角英数字と _ . - で、2〜32文字にしてください。"
    if len(password or "") < models.PASSWORD_MIN_LEN:
        return f"パスワードは{models.PASSWORD_MIN_LEN}文字以上にしてください。"
    if password2 is not None and password != password2:
        return "確認用パスワードが一致しません。"
    return None


@app.route("/signup", methods=["GET", "POST"])
def signup():
    user = current_user()
    if models.has_login(user):
        flash("このブラウザはすでにアカウントにログインしています。", "ok")
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        err = _validate_credentials(username, password, password2)
        if not err and models.username_taken(g.db, username):
            err = "そのユーザー名は使われています。"
        if err:
            flash(err, "warn")
            return render_template("signup.html")
        # いま使っている匿名ユーザーにログイン情報を紐づける（投稿履歴を引き継ぐ）
        models.attach_login(g.db, user["id"], username, password)
        flash("アカウントを作成しました。次からは別の端末でもログインできます。", "ok")
        return redirect(url_for("home"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        time.sleep(0.3)  # 総当たりを鈍らせる簡易対策（本格的なレート制限は今後の課題）
        user = models.verify_login(g.db, username, password)
        if not user:
            if username and not models.username_taken(g.db, username):
                flash("そのユーザー名のアカウントは存在しません。下の「アカウントを作成」から登録してください。", "warn")
            else:
                flash("パスワードが違います。", "warn")
            return render_template("login.html", username=username)
        # ログイン直前まで匿名で書いていた投稿・リアクションを、このアカウントに引き継ぐ
        pre = current_user()
        merged = False
        if pre["id"] != user["id"] and not models.has_login(pre) and models.user_has_content(g.db, pre["id"]):
            models.merge_user(g.db, pre["id"], user["id"])
            merged = True
        login_session(user)
        flash("おかえりなさい。この端末で書いた記録も引き継ぎました。" if merged
              else "おかえりなさい。あなたの記録に戻りました。", "ok")
        n = models.unseen_notif_count(g.db, user["id"])
        if n:
            flash(f"あなたの投稿に、新しい共感・発見が{n}件あります。「通知」から見られます。", "ok")
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("ログアウトしました。この端末では新しい匿名アカウントとして続けられます。", "ok")
    return redirect(url_for("home"))


@app.route("/account/password", methods=["POST"])
def change_password():
    user = current_user()
    if not models.has_login(user):
        abort(404)
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    new2 = request.form.get("new_password2", "")
    if not models.check_password_hash(user["password_hash"], current):
        flash("現在のパスワードが違います。", "warn")
    elif len(new) < models.PASSWORD_MIN_LEN or new != new2:
        flash(f"新しいパスワードは{models.PASSWORD_MIN_LEN}文字以上で、確認用と一致させてください。", "warn")
    else:
        models.change_password(g.db, user["id"], new)
        flash("パスワードを変更しました。", "ok")
    return redirect(url_for("settings"))


@app.route("/reset-me")
def reset_me():
    """このブラウザの匿名IDを捨てて新しい体験を始める（プロトタイプの動作確認用）。
    ログイン中でもアカウント自体は消えず、再ログインで戻れる。"""
    session.clear()
    flash("新しい匿名アカウントとして始めます。", "ok")
    return redirect(url_for("welcome"))


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# デバッグモード（パスワードで入る）— 分類の利用量・設定・トークン概算を見る
#   HAPIDIARY_DEBUG_PASSWORD を設定したときだけ有効。未設定なら 404。
# ---------------------------------------------------------------------------

def _debug_password() -> str:
    return os.environ.get("HAPIDIARY_DEBUG_PASSWORD", "")


def _debug_snapshot() -> dict:
    from classify import (available_mode, embeddings_available, EMBEDDINGS_PATH,
                          _classify_model, _preview_model, _embed_min_score, _embed_keep_gap)
    emb = None
    if os.path.exists(EMBEDDINGS_PATH):
        try:
            with open(EMBEDDINGS_PATH, encoding="utf-8") as f:
                d = __import__("json").load(f)
            emb = {"provider": d.get("provider"), "model": d.get("model"),
                   "dim": d.get("dim"), "count": d.get("count")}
        except Exception as e:
            emb = {"error": str(e)}

    m = models.all_meta(g.db, "dbg:")
    today = f"{datetime.now(timezone.utc):%Y-%m-%d}"

    def gi(k):
        try:
            return int(m.get(k, "0"))
        except ValueError:
            return 0

    days = sorted({k.split(":")[2] for k in m if k.startswith("dbg:day:")}, reverse=True)[:14]
    day_rows = [{
        "date": dstr,
        "calls": gi(f"dbg:day:{dstr}:calls"),
        "api_calls": gi(f"dbg:day:{dstr}:api_calls"),
        "tokens_est": gi(f"dbg:day:{dstr}:tokens_est"),
    } for dstr in days]

    last = None
    try:
        last = __import__("json").loads(m["dbg:last"]) if "dbg:last" in m else None
    except Exception:
        pass
    last_error = None
    try:
        last_error = __import__("json").loads(m["dbg:last_error"]) if "dbg:last_error" in m else None
    except Exception:
        pass

    # --- 閲覧数 ---
    pv_days = sorted({k.split(":")[2] for k in m if k.startswith("pv:day:")}, reverse=True)[:14]
    pv_rows = [{"date": d, "views": gi(f"pv:day:{d}:views"), "visitors": gi(f"pv:day:{d}:visitors")}
               for d in pv_days]
    users_total = g.db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    pageviews = {
        "total": gi("pv:total"),
        "visitors_total": gi("pv:visitors_total"),
        "users_total": users_total,
        "today_views": gi(f"pv:day:{today}:views"),
        "today_visitors": gi(f"pv:day:{today}:visitors"),
        "by_path": {k[len("pv:path:"):]: gi(k) for k in m if k.startswith("pv:path:")},
        "days": pv_rows,
    }

    return {
        "mode": available_mode(),
        "api_keys": {  # 'keys' は dict のメソッド名と衝突して Jinja で参照できないので避ける
            "GEMINI": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
            "VOYAGE": bool(os.environ.get("VOYAGE_API_KEY")),
            "ANTHROPIC": bool(os.environ.get("ANTHROPIC_API_KEY")),
        },
        "embeddings_file": emb,
        "embeddings_active": embeddings_available(),
        "models": {"classify": _classify_model(), "preview": _preview_model()},
        "embed_thresholds": {"min_score": _embed_min_score(), "keep_gap": _embed_keep_gap()},
        "totals": {
            "calls": gi("dbg:calls:total"),
            "api_calls": gi("dbg:api_calls:total"),
            "tokens_est": gi("dbg:tokens_est:total"),
            "by_method": {k.split(":")[2]: gi(k) for k in m
                          if k.startswith("dbg:calls:") and not k.endswith(":total")},
        },
        "today": {
            "date": today,
            "calls": gi(f"dbg:day:{today}:calls"),
            "api_calls": gi(f"dbg:day:{today}:api_calls"),
            "tokens_est": gi(f"dbg:day:{today}:tokens_est"),
        },
        "days": day_rows,
        "last": last,
        "last_error": last_error,
        "pageviews": pageviews,
        # Google はリアルタイムの残量をAPI公開していない。無料枠の目安のみ。
        "free_tier_note": {
            "gemini_embed": "無料枠の目安: 約100リクエスト/分・約1,000リクエスト/日（gemini-embedding-001）。"
                            "正確な残量は Google 側ダッシュボードで確認",
            "usage_dashboard": "https://aistudio.google.com/  /  https://ai.dev/rate-limit",
        },
    }


@app.route("/debug", methods=["GET", "POST"])
def debug_page():
    if not _debug_password():
        abort(404)
    if request.method == "POST" and not session.get("debug_ok"):
        if hmac.compare_digest(request.form.get("password", ""), _debug_password()):
            session["debug_ok"] = True
        else:
            flash("パスワードが違います。", "warn")
            return render_template("debug.html", authed=False, snap=None)
    if not session.get("debug_ok"):
        return render_template("debug.html", authed=False, snap=None)

    tested = None
    if request.method == "POST" and request.form.get("test_text"):
        txt = request.form.get("test_text", "")[:400]
        tested = classify(txt)
        _record_classify(tested)
    return render_template("debug.html", authed=True, snap=_debug_snapshot(), tested=tested)


@app.route("/debug/logout", methods=["POST"])
def debug_logout():
    session.pop("debug_ok", None)
    return redirect(url_for("debug_page"))


if __name__ == "__main__":
    # 開発用。SCHEMA が無ければ作る（init_db.py を先に実行するのが本筋）
    conn = models.get_db()
    models.init_schema(conn)
    conn.close()
    app.run(host="127.0.0.1", port=5000, debug=True)
