# -*- coding: utf-8 -*-
"""DB初期化 / シード投入スクリプト（仕様書7.4 / 7.6）

    python init_db.py              # スキーマ作成。DBが空なら全デモデータを投入
    python init_db.py --reset      # 既存DBを削除して作り直す（アカウントも消える）
    python init_db.py --no-seed    # スキーマだけ
    python init_db.py --add-corpus # 既存データを消さずに、seed_corpus のコーパスを追加
                                   #   （本番で投稿数を増やしたいとき。二重投入はしない）

本番（Fly.io）では環境変数 HAPIDIARY_DB=/data/happidiary.db を指定して実行する。
コーパス（seed_corpus.py の全450件）はここで posts / post_tags テーブルに保存される。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import models
from classify import load_taxonomy
from seed_corpus import iter_entries

CORPUS_VERSION = "1"

DEMO_ATTR_POOL = [
    {"age_band": "20代", "household": "ひとり暮らし"},
    {"age_band": "20代", "household": "実家暮らし"},
    {"age_band": "20代", "household": "パートナーと二人"},
    {"age_band": "30代", "household": "パートナーと二人"},
    {"age_band": "30代", "household": "子どもがいる"},
    {"age_band": "30代", "household": "ひとり暮らし"},
    {"age_band": "40代", "household": "子どもがいる"},
    {"age_band": "40代", "household": "ひとり暮らし"},
    {"age_band": "40代", "household": "パートナーと二人"},
    {"age_band": "50代", "household": "パートナーと二人"},
    {"age_band": "50代", "household": "子どもがいる"},
    {"age_band": "10代", "household": "実家暮らし"},
    {"age_band": "60代以上", "household": "パートナーと二人"},
    {"age_band": "未設定", "household": "その他"},
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _tag_from_sub(g: dict, s: dict, conf: float) -> dict:
    return {
        "major": g["major"],
        "genre_id": g["id"],
        "genre_name": g["name"],
        "subgenre_name": s["name"],
        "confidence": round(conf, 2),
        "mute_theme": s.get("mute_theme"),
    }


def ensure_demo_users(conn, rnd) -> list:
    """ログインを持たない匿名デモユーザーを確保する（無ければ作る）。"""
    existing = conn.execute(
        "SELECT * FROM users WHERE username_hash IS NULL ORDER BY id"
    ).fetchall()
    demo_users = list(existing)
    for attr in DEMO_ATTR_POOL[len(demo_users):]:
        u = models.create_user(conn, attributes=attr)
        models.mark_onboarded(conn, u["id"])
        picks = rnd.sample(range(1, 31), rnd.randint(8, 16))
        prefs = {gid: rnd.choice(["よく見たい", "普通", "普通", "少なめ"]) for gid in picks}
        models.set_genre_prefs(conn, u["id"], prefs)
        demo_users.append(models.get_user(conn, u["id"]))
    return demo_users


def _corpus_items(genre_by_id: dict, include_examples: bool) -> list[tuple[dict, dict, str]]:
    items: list[tuple[dict, dict, str]] = []
    for gid, sub_index, body in iter_entries():
        g = genre_by_id[gid]
        items.append((g, g["subgenres"][sub_index], body))
    if include_examples:
        for g in genre_by_id.values():
            for s in g["subgenres"]:
                items.append((g, s, s["example"]))
    return items


def insert_posts(conn, demo_users, items, rnd, genres) -> int:
    now = datetime.now(timezone.utc)
    rnd.shuffle(items)
    for (g, s, body) in items:
        author = rnd.choice(demo_users)
        # 直近ほど多め。過去の再浮上（3.6）と分析（5年前）も体験できるよう一部は数年前へ。
        bucket = rnd.choices(["recent", "months", "years"], weights=[6, 3, 2])[0]
        if bucket == "recent":
            days_ago = rnd.randint(0, 45)
        elif bucket == "months":
            days_ago = rnd.randint(46, 420)
        else:
            days_ago = rnd.randint(365, 365 * 5 + 60)
        created = now - timedelta(days=days_ago, hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59))

        tags = [_tag_from_sub(g, s, rnd.uniform(0.72, 0.97))]
        if rnd.random() < 0.22:  # 1投稿に複数サブジャンル可（仕様書3.2）
            g2 = rnd.choice(genres)
            if g2["id"] != g["id"]:
                s2 = rnd.choice(g2["subgenres"])
                tags.append(_tag_from_sub(g2, s2, rnd.uniform(0.4, 0.68)))

        models.create_post(
            conn, author_id=author["id"], body=body, tags=tags,
            created_at=_iso(created),
        )
    conn.commit()
    return len(items)


def seed_reactions(conn, demo_users, post_ids, rnd) -> None:
    """数値は見せないが、傾向の土台として控えめにリアクションを入れておく。
    到達の平等（REACH_LIMIT）を新規ユーザーが体験できるよう mark_reached は控えめに。"""
    for pid in post_ids:
        post = models.get_post(conn, pid)
        reactors = [u for u in demo_users if u["id"] != post["author_id"]]
        for u in rnd.sample(reactors, rnd.randint(0, 2)):
            # 「比べてしまう」は棲み分けを起こす強い信号。デモでは撒かず、実ユーザーの
            # 操作だけで発生させる（weights の末尾を 0 に）。
            kind = rnd.choices(models.REACTION_KINDS, weights=[6, 3, 2, 0])[0]
            models.add_reaction(conn, pid, u["id"], kind)
            if rnd.random() < 0.5:
                models.mark_reached(conn, pid, u["id"])


def _counts(conn) -> str:
    u = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    p = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    r = conn.execute("SELECT COUNT(*) c FROM reactions").fetchone()["c"]
    return f"users={u} posts={p} reactions={r}"


def seed(conn) -> None:
    """空のDBに、デモユーザー＋コーパス＋分類例＋リアクションを投入する。"""
    genre_by_id = {g["id"]: g for g in load_taxonomy()["genres"]}
    genres = list(genre_by_id.values())
    rnd = random.Random(20260910)

    demo_users = ensure_demo_users(conn, rnd)
    items = _corpus_items(genre_by_id, include_examples=True)
    insert_posts(conn, demo_users, items, rnd, genres)
    post_ids = [r["id"] for r in conn.execute("SELECT id FROM posts").fetchall()]
    seed_reactions(conn, demo_users, post_ids, rnd)
    conn.commit()
    models.set_meta(conn, "corpus_loaded", CORPUS_VERSION)
    print(f"seeded: {_counts(conn)}")


def add_corpus(conn) -> None:
    """既存データを消さずにコーパスだけ追加する（本番向け）。二重投入は防ぐ。"""
    if models.get_meta(conn, "corpus_loaded") == CORPUS_VERSION:
        print("corpus already loaded — nothing to do")
        return
    genre_by_id = {g["id"]: g for g in load_taxonomy()["genres"]}
    genres = list(genre_by_id.values())
    rnd = random.Random()

    before = conn.execute("SELECT id FROM posts").fetchall()
    before_ids = {r["id"] for r in before}

    demo_users = ensure_demo_users(conn, rnd)
    items = _corpus_items(genre_by_id, include_examples=False)  # 分類例は入れず二重を避ける
    n = insert_posts(conn, demo_users, items, rnd, genres)

    new_ids = [r["id"] for r in conn.execute("SELECT id FROM posts").fetchall()
               if r["id"] not in before_ids]
    seed_reactions(conn, demo_users, new_ids, rnd)
    conn.commit()
    models.set_meta(conn, "corpus_loaded", CORPUS_VERSION)
    print(f"added {n} corpus posts. now: {_counts(conn)}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="ハピディアリー DB初期化 / シード")
    ap.add_argument("--reset", action="store_true", help="既存DBを削除して作り直す")
    ap.add_argument("--no-seed", action="store_true", help="デモデータを入れない")
    ap.add_argument("--add-corpus", action="store_true",
                    help="既存データを残したままコーパスを追加（本番向け・二重投入なし）")
    args = ap.parse_args(argv)

    db_path = models.DB_PATH
    if args.reset and os.path.exists(db_path):
        os.remove(db_path)
        print(f"removed {db_path}")

    conn = models.get_db()
    try:
        models.init_schema(conn)
        print(f"schema ready: {db_path}")
        if args.add_corpus:
            add_corpus(conn)
        elif args.no_seed:
            print("skip seeding (--no-seed)")
        elif conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
            print("skip seeding (users already present)")
        else:
            seed(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
