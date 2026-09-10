# -*- coding: utf-8 -*-
"""分類体系の150サブジャンルを1回だけベクトル化して genres_embeddings.json に保存する。

埋め込み近傍検索方式（classify.py の classify_embed）が使う「お手本ベクトル」を作る。
プロバイダは環境変数で自動選択（--provider で明示指定も可）:
  - GEMINI_API_KEY があれば Gemini（無料枠が広い・カード登録不要）
  - VOYAGE_API_KEY があれば Voyage AI（voyage-4-lite）

    # Gemini（推奨・無料枠）
    pip install google-genai
    $env:GEMINI_API_KEY="..."          # https://aistudio.google.com/apikey
    python genres_build_embeddings.py

    # Voyage
    pip install voyageai
    $env:VOYAGE_API_KEY="pa-..."
    python genres_build_embeddings.py --provider voyage

出力 genres_embeddings.json はリポジトリにコミットしてよい。分類体系(genres.json)を変えたら作り直す。
実行時も同じプロバイダのキー（GEMINI_API_KEY / VOYAGE_API_KEY）が必要。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "gemini": {"model": "gemini-embedding-001", "dim": 768},
    "voyage": {"model": "voyage-4-lite", "dim": 512},
}


def _doc_text(major: str, genre: str, sub: dict) -> str:
    kws = "・".join(sub.get("keywords", []))
    return (
        f"大分類:{major}｜ジャンル:{genre}｜サブジャンル:{sub['name']}。"
        f"{sub.get('description', '')}。"
        f"たとえばこんな投稿:{sub.get('example', '')}。"
        f"関連語:{kws}"
    )


def _pick_provider(arg: str | None) -> str:
    if arg:
        return arg
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("VOYAGE_API_KEY"):
        return "voyage"
    return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["gemini", "voyage"])
    ap.add_argument("--model")
    ap.add_argument("--dim", type=int)
    args = ap.parse_args(argv)

    provider = _pick_provider(args.provider)
    if not provider:
        print("GEMINI_API_KEY か VOYAGE_API_KEY を設定してください。")
        print("  Gemini: https://aistudio.google.com/apikey （無料・カード不要）")
        return 1
    model = args.model or DEFAULTS[provider]["model"]
    dim = args.dim or DEFAULTS[provider]["dim"]

    # classify.py の embed_texts を再利用（プロバイダ実装の重複を避ける）
    sys.path.insert(0, _HERE)
    from classify import embed_texts

    with open(os.path.join(_HERE, "genres.json"), encoding="utf-8") as f:
        tax = json.load(f)

    index: list[dict] = []
    texts: list[str] = []
    for g in tax["genres"]:
        for s in g["subgenres"]:
            index.append({
                "major": g["major"], "genre_id": g["id"], "genre_name": g["name"],
                "subgenre_name": s["name"], "mute_theme": s.get("mute_theme"),
            })
            texts.append(_doc_text(g["major"], g["name"], s))

    print(f"embedding {len(texts)} subgenres  provider={provider} model={model} dim={dim} …")
    vectors = embed_texts(texts, provider=provider, model=model, dim=dim, is_query=False)
    assert len(vectors) == len(index), (len(vectors), len(index))
    vectors = [[round(x, 6) for x in v] for v in vectors]

    out = {"provider": provider, "model": model, "dim": dim,
           "count": len(vectors), "index": index, "vectors": vectors}
    dest = os.path.join(_HERE, "genres_embeddings.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"wrote {dest}  ({os.path.getsize(dest) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
