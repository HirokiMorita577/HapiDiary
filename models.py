# -*- coding: utf-8 -*-
"""ハピディアリー データモデル（仕様書5章）

プロトタイプ段階なので sqlite3 標準ライブラリを直接使う（仕様書7.2）。
テーブルが増えて複雑になったら Flask-SQLAlchemy への移行を検討する。

設計原則（仕様書2章）との対応:
- 完全匿名     : users はハンドルネーム・実名・アイコン列を一切持たない。anon_id は乱数。
- 数値非表示   : リアクションは reactions / notifications に保存するが、集計値の見せ方は
                 テンプレート側で制御する（他者投稿では出さない）。
- 到達の平等   : post_reach で「1投稿が届いた人数」を固定上限まで記録する。
- データを渡さない: 属性×ジャンル×時系列の集計は分析画面の本人向け表示にのみ使う。
"""
import hashlib
import hmac
import json
import os
import sqlite3
import secrets
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash


def _default_db_path() -> str:
    # Fly.io など本番では HAPIDIARY_DB=/data/happidiary.db を指定する（仕様書7.5）
    env = os.environ.get("HAPIDIARY_DB")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "happidiary.db")


DB_PATH = _default_db_path()

# 到達の平等: 1投稿が届く人数の固定上限（仕様書3.5）。プロトタイプ用に小さめ。
REACH_LIMIT = 5
# 共感タイムラインの1日あたり表示件数の目安（仕様書3.3）
TIMELINE_DAILY_LIMIT = 10

REACTION_KINDS = ["共感", "発見", "今はちがう", "比べてしまう"]
READ_STRENGTHS = ["よく見たい", "普通", "少なめ", "見ない"]
STRENGTH_WEIGHT = {"よく見たい": 3.0, "普通": 1.0, "少なめ": 0.3, "見ない": 0.0}

# ---- ログイン（任意機能） -------------------------------------------------
# 仕様書の「完全匿名」を保つため、ログインはアカウント復旧・複数端末用にとどめる。
# ・ユーザー名は平文で保存しない。サーバー鍵付きハッシュ(HMAC-SHA256)にして保存し、
#   ログイン時は同じHMACで突き合わせる（不可逆・鍵が無ければ総当たり不可）。
# ・パスワードは werkzeug の scrypt でソルト付き一方向ハッシュ（復元不可）。
# ・鍵は HAPIDIARY_LOGIN_KEY。未設定時は SECRET_KEY から導出（開発用フォールバック）。
# 半角英数字と _ . - に加えて、日本語（ひらがな・カタカナ・漢字・長音符・々・ー）も許可。
_JP = r"ぁ-ゖゝ-ゟァ-ヿ々㐀-䶿一-鿿豈-﫿ｦ-ﾟ"
USERNAME_RE = r"^[A-Za-z0-9_.\-" + _JP + r"]{2,32}$"
PASSWORD_MIN_LEN = 8


def _login_key() -> bytes:
    key = os.environ.get("HAPIDIARY_LOGIN_KEY")
    if not key:
        key = "devfallback::" + os.environ.get("HAPIDIARY_SECRET_KEY", "happidiary-dev-secret-change-me")
    return hashlib.sha256(key.encode("utf-8")).digest()


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def username_digest(raw: str) -> str:
    """ログイン名を鍵付きハッシュ化（DBにはこの値だけを保存する）。"""
    return hmac.new(_login_key(), normalize_username(raw).encode("utf-8"), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    anon_id       TEXT NOT NULL UNIQUE,          -- 匿名ID。ハンドルネーム等は持たない
    attributes    TEXT NOT NULL DEFAULT '{}',    -- 属性タグ(JSON)。匿名のまま保持
    onboarded     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    -- ログイン（任意）。未ログインの匿名ユーザーは NULL のまま。
    username_hash TEXT UNIQUE,                   -- ログイン名の鍵付きハッシュ（平文は保存しない）
    password_hash TEXT,                          -- scrypt によるパスワードの一方向ハッシュ
    login_at      TEXT                           -- 最終ログイン日時
);

-- 読みたい設定（ジャンル単位。詳細設定でサブジャンルまで想定）
CREATE TABLE IF NOT EXISTS user_genre_prefs (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL,
    strength  TEXT NOT NULL DEFAULT '普通',      -- よく見たい/普通/少なめ/見ない
    PRIMARY KEY (user_id, genre_id)
);

-- ミュート設定（分類とは別軸のセンシティブテーマ単位）
CREATE TABLE IF NOT EXISTS user_mutes (
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    theme    TEXT NOT NULL,
    PRIMARY KEY (user_id, theme)
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body        TEXT NOT NULL,
    attachment  TEXT,                            -- 画像・メモ・音楽URL等（プロトタイプはURL文字列）
    sensitive   INTEGER NOT NULL DEFAULT 0,      -- センシティブ／ミュート候補フラグ
    created_at  TEXT NOT NULL                    -- 投稿日時（再浮上で過去日付が入ることがある）
);

-- ジャンルタグ（大分類／ジャンル／サブジャンル、複数付与可）
CREATE TABLE IF NOT EXISTS post_tags (
    post_id       INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    major         TEXT NOT NULL,
    genre_id      INTEGER NOT NULL,
    genre_name    TEXT NOT NULL,
    subgenre_name TEXT,
    confidence    REAL NOT NULL DEFAULT 0,
    mute_theme    TEXT
);

-- 到達済みユーザーリスト（到達の平等の実装に必要）
CREATE TABLE IF NOT EXISTS post_reach (
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reached_at TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);

CREATE TABLE IF NOT EXISTS reactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                    -- 共感／発見／今はちがう／比べてしまう
    created_at TEXT NOT NULL,
    UNIQUE (post_id, user_id, kind)
);

-- リアクションは「投稿者への通知として軽く伝える」（仕様書3.4）。数値化しない。
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 受け取る人（＝投稿者）
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 0
);

-- 未来の幸せ提案ログ（仕様書3.7 / 5章 Suggestion）
CREATE TABLE IF NOT EXISTS suggestions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_profile TEXT NOT NULL DEFAULT '{}',   -- 提案元の属性・ジャンル傾向（匿名の集計）
    based_on_genre_id INTEGER,
    content        TEXT NOT NULL,                -- 提案内容（テキスト）
    feedback       TEXT,                         -- 気になる／興味なし
    created_at     TEXT NOT NULL
);

-- 内部用の小さなキー・バリュー（シード投入済みフラグなど）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 「比べてしまう」を選んだユーザー同士をつなぐための記録（仕様書3.4）
CREATE TABLE IF NOT EXISTS compare_cohort (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_author   ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_tags_post      ON post_tags(post_id);
CREATE INDEX IF NOT EXISTS idx_tags_genre     ON post_tags(genre_id);
CREATE INDEX IF NOT EXISTS idx_reach_user     ON post_reach(user_id);
CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id);
CREATE INDEX IF NOT EXISTS idx_notif_user     ON notifications(user_id, seen);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """既存DB（本番ボリューム上など）に不足カラムを足す。破壊的変更はしない。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for name, ddl in (
        ("username_hash", "ALTER TABLE users ADD COLUMN username_hash TEXT"),
        ("password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
        ("login_at", "ALTER TABLE users ADD COLUMN login_at TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username_hash) WHERE username_hash IS NOT NULL"
    )


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

def create_user(conn: sqlite3.Connection, attributes: dict | None = None) -> sqlite3.Row:
    anon_id = "anon_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO users (anon_id, attributes, created_at) VALUES (?, ?, ?)",
        (anon_id, json.dumps(attributes or {}, ensure_ascii=False), now_iso()),
    )
    conn.commit()
    return get_user_by_anon(conn, anon_id)


def get_user_by_anon(conn: sqlite3.Connection, anon_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE anon_id = ?", (anon_id,)).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def set_attributes(conn: sqlite3.Connection, user_id: int, attributes: dict) -> None:
    conn.execute(
        "UPDATE users SET attributes = ? WHERE id = ?",
        (json.dumps(attributes, ensure_ascii=False), user_id),
    )
    conn.commit()


def mark_onboarded(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET onboarded = 1 WHERE id = ?", (user_id,))
    conn.commit()


# ---- ログイン --------------------------------------------------------------

def has_login(user: sqlite3.Row | None) -> bool:
    return bool(user and user["username_hash"] and user["password_hash"])


def username_taken(conn: sqlite3.Connection, username_raw: str) -> bool:
    d = username_digest(username_raw)
    return conn.execute("SELECT 1 FROM users WHERE username_hash = ?", (d,)).fetchone() is not None


def attach_login(conn: sqlite3.Connection, user_id: int, username_raw: str, password: str) -> None:
    """いま使っている匿名ユーザーにログイン情報を紐づける（投稿履歴を引き継ぐ）。"""
    conn.execute(
        "UPDATE users SET username_hash = ?, password_hash = ?, login_at = ? WHERE id = ?",
        (username_digest(username_raw), generate_password_hash(password), now_iso(), user_id),
    )
    conn.commit()


def get_user_by_username(conn: sqlite3.Connection, username_raw: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username_hash = ?", (username_digest(username_raw),)
    ).fetchone()


def verify_login(conn: sqlite3.Connection, username_raw: str, password: str) -> sqlite3.Row | None:
    user = get_user_by_username(conn, username_raw)
    if not user or not user["password_hash"]:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    conn.execute("UPDATE users SET login_at = ? WHERE id = ?", (now_iso(), user["id"]))
    conn.commit()
    return user


def change_password(conn: sqlite3.Connection, user_id: int, new_password: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    conn.commit()


def user_has_content(conn: sqlite3.Connection, user_id: int) -> bool:
    """その匿名ユーザーが投稿・リアクション等を持っているか（ログイン時の引き継ぎ判定用）。"""
    for sql in (
        "SELECT 1 FROM posts WHERE author_id = ? LIMIT 1",
        "SELECT 1 FROM reactions WHERE user_id = ? LIMIT 1",
    ):
        if conn.execute(sql, (user_id,)).fetchone():
            return True
    return False


def merge_user(conn: sqlite3.Connection, from_id: int, into_id: int) -> None:
    """匿名で貯めた投稿・リアクションを、ログインしたアカウントに移し替えて from_id を削除する。
    ブラウザを閉じて入り直したときに「投稿が消えた」ように見えるのを防ぐ。"""
    if from_id == into_id:
        return
    c = conn.cursor()
    # 単純に付け替えられるもの
    c.execute("UPDATE posts SET author_id = ? WHERE author_id = ?", (into_id, from_id))
    c.execute("UPDATE notifications SET user_id = ? WHERE user_id = ?", (into_id, from_id))
    c.execute("UPDATE suggestions SET user_id = ? WHERE user_id = ?", (into_id, from_id))
    # 一意制約があるもの: 衝突する行は捨てる（OR IGNORE → 残りを削除）
    for tbl in ("reactions", "post_reach", "compare_cohort"):
        c.execute(f"UPDATE OR IGNORE {tbl} SET user_id = ? WHERE user_id = ?", (into_id, from_id))
        c.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (from_id,))
    # 読みたい設定は、アカウント側が未設定のときだけ引き継ぐ
    have = c.execute("SELECT 1 FROM user_genre_prefs WHERE user_id = ? LIMIT 1", (into_id,)).fetchone()
    if have:
        c.execute("DELETE FROM user_genre_prefs WHERE user_id = ?", (from_id,))
    else:
        c.execute("UPDATE OR IGNORE user_genre_prefs SET user_id = ? WHERE user_id = ?", (into_id, from_id))
        c.execute("DELETE FROM user_genre_prefs WHERE user_id = ?", (from_id,))
    have_m = c.execute("SELECT 1 FROM user_mutes WHERE user_id = ? LIMIT 1", (into_id,)).fetchone()
    if have_m:
        c.execute("DELETE FROM user_mutes WHERE user_id = ?", (from_id,))
    else:
        c.execute("UPDATE OR IGNORE user_mutes SET user_id = ? WHERE user_id = ?", (into_id, from_id))
        c.execute("DELETE FROM user_mutes WHERE user_id = ?", (from_id,))
    # どちらかがオンボーディング済みなら、アカウントも済み扱い
    c.execute(
        "UPDATE users SET onboarded = 1 WHERE id = ? AND EXISTS "
        "(SELECT 1 FROM users f WHERE f.id = ? AND f.onboarded = 1)",
        (into_id, from_id),
    )
    c.execute("DELETE FROM users WHERE id = ?", (from_id,))
    conn.commit()


# ---- meta -----------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def bump_meta_int(conn: sqlite3.Connection, key: str, delta: int = 1) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(meta.value AS INTEGER) + ? AS TEXT)",
        (key, str(delta), delta),
    )
    conn.commit()


def all_meta(conn: sqlite3.Connection, prefix: str = "") -> dict[str, str]:
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key", (prefix + "%",)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_genre_prefs(conn: sqlite3.Connection, user_id: int, prefs: dict[int, str]) -> None:
    """prefs: {genre_id: strength}. 指定されなかったジャンルは削除（未選択＝表示しない）。"""
    conn.execute("DELETE FROM user_genre_prefs WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO user_genre_prefs (user_id, genre_id, strength) VALUES (?, ?, ?)",
        [(user_id, int(gid), s) for gid, s in prefs.items() if s in STRENGTH_WEIGHT],
    )
    conn.commit()


def get_genre_prefs(conn: sqlite3.Connection, user_id: int) -> dict[int, str]:
    rows = conn.execute(
        "SELECT genre_id, strength FROM user_genre_prefs WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["genre_id"]: r["strength"] for r in rows}


def set_mutes(conn: sqlite3.Connection, user_id: int, themes: list[str]) -> None:
    conn.execute("DELETE FROM user_mutes WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO user_mutes (user_id, theme) VALUES (?, ?)",
        [(user_id, t) for t in themes],
    )
    conn.commit()


def get_mutes(conn: sqlite3.Connection, user_id: int) -> list[str]:
    rows = conn.execute("SELECT theme FROM user_mutes WHERE user_id = ?", (user_id,)).fetchall()
    return [r["theme"] for r in rows]


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------

def create_post(
    conn: sqlite3.Connection,
    author_id: int,
    body: str,
    tags: list[dict],
    attachment: str | None = None,
    sensitive: bool = False,
    created_at: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO posts (author_id, body, attachment, sensitive, created_at) VALUES (?, ?, ?, ?, ?)",
        (author_id, body, attachment, 1 if sensitive else 0, created_at or now_iso()),
    )
    post_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO post_tags (post_id, major, genre_id, genre_name, subgenre_name, confidence, mute_theme)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                post_id,
                t.get("major", ""),
                int(t.get("genre_id", 0)),
                t.get("genre_name", ""),
                t.get("subgenre_name"),
                float(t.get("confidence", 0)),
                t.get("mute_theme"),
            )
            for t in tags
        ],
    )
    conn.commit()
    return post_id


def get_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def get_tags(conn: sqlite3.Connection, post_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM post_tags WHERE post_id = ? ORDER BY confidence DESC", (post_id,)
    ).fetchall()


def user_post_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE author_id = ?", (user_id,)
    ).fetchone()["c"]


def own_posts(conn: sqlite3.Connection, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE author_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def delete_post(conn: sqlite3.Connection, post_id: int, author_id: int) -> bool:
    """自分の投稿を削除する。関連（タグ・到達・リアクション・通知・比べてしまう）は
    スキーマの ON DELETE CASCADE で一緒に消える。他人の投稿は消せない。"""
    cur = conn.execute(
        "DELETE FROM posts WHERE id = ? AND author_id = ?", (post_id, author_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# 到達の平等
# ---------------------------------------------------------------------------

def reach_count(conn: sqlite3.Connection, post_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM post_reach WHERE post_id = ?", (post_id,)
    ).fetchone()["c"]


def mark_reached(conn: sqlite3.Connection, post_id: int, user_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO post_reach (post_id, user_id, reached_at) VALUES (?, ?, ?)",
        (post_id, user_id, now_iso()),
    )
    conn.commit()


def unmark_reached(conn: sqlite3.Connection, post_id: int, user_id: int) -> None:
    """差し替えを「元に戻す」ときに、差し替えで消費した到達枠を返す。"""
    conn.execute("DELETE FROM post_reach WHERE post_id = ? AND user_id = ?", (post_id, user_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Reaction / Notification
# ---------------------------------------------------------------------------

def single_reaction(conn: sqlite3.Connection, post_id: int, user_id: int) -> str | None:
    """その投稿にこのユーザーが付けているリアクション（1投稿1つ）。無ければ None。"""
    row = conn.execute(
        "SELECT kind FROM reactions WHERE post_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1",
        (post_id, user_id),
    ).fetchone()
    return row["kind"] if row else None


def _drop_reaction_rows(conn: sqlite3.Connection, post_id: int, user_id: int) -> None:
    """このユーザーのこの投稿へのリアクションと、それに紐づく未読通知・比べてしまうコホートを消す。"""
    kinds = [r["kind"] for r in conn.execute(
        "SELECT kind FROM reactions WHERE post_id = ? AND user_id = ?", (post_id, user_id))]
    if not kinds:
        return
    conn.execute("DELETE FROM reactions WHERE post_id = ? AND user_id = ?", (post_id, user_id))
    conn.execute("DELETE FROM compare_cohort WHERE post_id = ? AND user_id = ?", (post_id, user_id))
    post = get_post(conn, post_id)
    if post:
        qs = ",".join("?" * len(kinds))
        conn.execute(
            f"DELETE FROM notifications WHERE post_id = ? AND user_id = ? AND seen = 0 AND kind IN ({qs})",
            (post_id, post["author_id"], *kinds),
        )


def clear_reaction(conn: sqlite3.Connection, post_id: int, user_id: int) -> str | None:
    """リアクションを取り消す。取り消した種別を返す（無ければ None）。"""
    prev = single_reaction(conn, post_id, user_id)
    _drop_reaction_rows(conn, post_id, user_id)
    conn.commit()
    return prev


def set_reaction(conn: sqlite3.Connection, post_id: int, user_id: int, kind: str) -> str | None:
    """リアクションを付け替える（1投稿1つ）。付け替え前の種別を返す（無ければ None）。
    同じ種別なら何もしない。"""
    if kind not in REACTION_KINDS:
        raise ValueError(f"unknown reaction kind: {kind}")
    prev = single_reaction(conn, post_id, user_id)
    if prev == kind:
        return prev
    _drop_reaction_rows(conn, post_id, user_id)
    conn.execute(
        "INSERT INTO reactions (post_id, user_id, kind, created_at) VALUES (?, ?, ?, ?)",
        (post_id, user_id, kind, now_iso()),
    )
    post = get_post(conn, post_id)
    # 通知は前向きなもの（共感・発見）だけ。今はちがう／比べてしまうは投稿者に伝えない。
    if post and post["author_id"] != user_id and kind in ("共感", "発見"):
        conn.execute(
            "INSERT INTO notifications (user_id, post_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (post["author_id"], post_id, kind, now_iso()),
        )
    if kind == "比べてしまう":
        conn.execute(
            "INSERT OR IGNORE INTO compare_cohort (user_id, post_id, created_at) VALUES (?, ?, ?)",
            (user_id, post_id, now_iso()),
        )
    conn.commit()
    return prev


def add_reaction(conn: sqlite3.Connection, post_id: int, user_id: int, kind: str) -> bool:
    """後方互換: 未リアクションのときだけ付ける。"""
    if single_reaction(conn, post_id, user_id):
        return False
    set_reaction(conn, post_id, user_id, kind)
    return True


# ---- 「比べてしまう」による棲み分け（仕様書3.4） --------------------------

def compare_blocked_authors(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """user_id と「比べてしまう」で相互ブロックになっている相手（投稿者）の集合。
    ・自分が相手の投稿に「比べてしまう」を押した → 相手を表示しない
    ・相手が自分の投稿に「比べてしまう」を押した → その相手を表示しない
    """
    rows = conn.execute(
        """
        SELECT DISTINCT p.author_id AS uid
        FROM compare_cohort cc JOIN posts p ON p.id = cc.post_id
        WHERE cc.user_id = ? AND p.author_id != ?
        UNION
        SELECT DISTINCT cc.user_id AS uid
        FROM compare_cohort cc JOIN posts p ON p.id = cc.post_id
        WHERE p.author_id = ? AND cc.user_id != ?
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    return {r["uid"] for r in rows}


def compare_user_ids(conn: sqlite3.Connection) -> set[int]:
    """一度でも「比べてしまう」を押したことのあるユーザー（＝マウント傾向のプール）。"""
    rows = conn.execute("SELECT DISTINCT user_id FROM compare_cohort").fetchall()
    return {r["user_id"] for r in rows}


def is_compare_user(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM compare_cohort WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone() is not None


# ---- 連続投稿日数（ストリーク） -----------------------------------------

def posting_stats(conn: sqlite3.Connection, user_id: int, tz_offset_hours: int = 9) -> dict:
    """投稿日から連続日数などを計算する。日付境界は既定で日本時間(UTC+9)。"""
    from datetime import date, timedelta

    rows = conn.execute(
        "SELECT created_at FROM posts WHERE author_id = ?", (user_id,)
    ).fetchall()
    days: set[date] = set()
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["created_at"])
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone.utc) + timedelta(hours=tz_offset_hours)
        days.add(local.date())

    if not days:
        return {"streak": 0, "best": 0, "total_days": 0, "posts_total": 0, "posted_today": False}

    today = (datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)).date()
    posted_today = today in days

    # 現在の連続日数：今日（未投稿なら昨日）から遡って連続している日数
    anchor = today if posted_today else today - timedelta(days=1)
    streak = 0
    d = anchor
    while d in days:
        streak += 1
        d -= timedelta(days=1)

    # 最長連続
    best = 0
    for d0 in days:
        if (d0 - timedelta(days=1)) in days:
            continue  # 連続の途中。始点だけ数える
        run, d = 0, d0
        while d in days:
            run += 1
            d += timedelta(days=1)
        best = max(best, run)

    return {
        "streak": streak,
        "best": best,
        "total_days": len(days),
        "posts_total": len(rows),
        "posted_today": posted_today,
    }


def reaction_counts(conn: sqlite3.Connection, post_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS c FROM reactions WHERE post_id = ? GROUP BY kind", (post_id,)
    ).fetchall()
    counts = {k: 0 for k in REACTION_KINDS}
    for r in rows:
        counts[r["kind"]] = r["c"]
    return counts


def my_reactions(conn: sqlite3.Connection, post_id: int, user_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT kind FROM reactions WHERE post_id = ? AND user_id = ?", (post_id, user_id)
    ).fetchall()
    return {r["kind"] for r in rows}


def unseen_notifications(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? AND seen = 0 ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()


def unseen_notif_count(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND seen = 0", (user_id,)
    ).fetchone()["c"]


def notification_feed(conn: sqlite3.Connection, user_id: int, limit: int = 60) -> list[dict]:
    """自分の投稿に届いた共感・発見を、投稿ごとにまとめて新しい順で返す。"""
    rows = conn.execute(
        """
        SELECT p.id AS post_id, p.body AS body,
               SUM(CASE WHEN n.kind = '共感' THEN 1 ELSE 0 END) AS empathy,
               SUM(CASE WHEN n.kind = '発見' THEN 1 ELSE 0 END) AS discover,
               SUM(CASE WHEN n.seen = 0 THEN 1 ELSE 0 END) AS unseen,
               MAX(n.created_at) AS latest
        FROM notifications n JOIN posts p ON p.id = n.post_id
        WHERE n.user_id = ?
        GROUP BY p.id
        ORDER BY latest DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_seen(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE notifications SET seen = 1 WHERE user_id = ?", (user_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------

def add_suggestion(
    conn: sqlite3.Connection,
    user_id: int,
    content: str,
    based_on_genre_id: int | None,
    source_profile: dict,
) -> int:
    cur = conn.execute(
        """INSERT INTO suggestions (user_id, source_profile, based_on_genre_id, content, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            user_id,
            json.dumps(source_profile, ensure_ascii=False),
            based_on_genre_id,
            content,
            now_iso(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def latest_suggestion(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    # created_at は秒精度なので、同秒の取り違えを防ぐため id でもタイブレークする
    return conn.execute(
        "SELECT * FROM suggestions WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def set_suggestion_feedback(conn: sqlite3.Connection, suggestion_id: int, feedback: str) -> None:
    conn.execute("UPDATE suggestions SET feedback = ? WHERE id = ?", (feedback, suggestion_id))
    conn.commit()


def posted_in_genre_since(conn: sqlite3.Connection, user_id: int, genre_id: int, since_iso: str) -> bool:
    """since_iso 以降に、そのジャンルのタグが付いた投稿をしたか（提案の「達成」判定用）。"""
    row = conn.execute(
        """
        SELECT 1 FROM posts p JOIN post_tags t ON t.post_id = p.id
        WHERE p.author_id = ? AND t.genre_id = ? AND p.created_at > ?
        LIMIT 1
        """,
        (user_id, genre_id, since_iso),
    ).fetchone()
    return row is not None
