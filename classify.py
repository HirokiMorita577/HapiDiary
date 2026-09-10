# -*- coding: utf-8 -*-
"""AIによる幸せの自動ジャンル分類（仕様書3.2 / 3.8 / 7.3）

方式（上から順に試し、失敗したら下へフォールバックする）:
  (a) 埋め込み近傍検索（既定・推奨） : Voyage AI(voyage-4-lite)で投稿文をベクトル化し、
      事前計算した150サブジャンルのベクトル（genres_embeddings.json）とコサイン類似度を取り、
      上位を分類結果にする。分類体系を毎回送らないので LLM 方式より桁違いに安い。
  (b) LLM API方式 : ANTHROPIC_API_KEY があれば Anthropic Claude API で分類（精度最優先モード）。
  (c) キーワード方式（完全オフライン） : genres.json の keywords ＋ MeCab(fugashi)の原形化で
      スコアリング。APIキー不要。(a)(b)が使えない/失敗したときの最終フォールバック。

環境変数:
  HAPIDIARY_CLASSIFY_METHOD : "auto"(既定) / "embed" / "llm" / "keyword" のいずれかで方式を固定
  GEMINI_API_KEY            : (a) Gemini 埋め込み用（無料枠・カード不要）
  VOYAGE_API_KEY            : (a) Voyage 埋め込み用。どちらか一方あればよい。無ければ (b)→(c) へ
  ANTHROPIC_API_KEY         : (b)を使うためのキー
  HAPIDIARY_EMBED_MODEL     : (a)のモデル（既定は genres_embeddings.json に記録されたもの）
  HAPIDIARY_CLASSIFY_MODEL  : (b)投稿確定時のモデル（既定 claude-haiku-4-5）
  HAPIDIARY_PREVIEW_MODEL   : (b)書きながらのプレビュー用モデル（未設定なら上と同じ）
"""
from __future__ import annotations

import json
import os
import re
import functools

_HERE = os.path.dirname(os.path.abspath(__file__))
GENRES_PATH = os.path.join(_HERE, "genres.json")

# ミュート候補テーマ（分類とは別軸。仕様書4.1）の判定用キーワード
MUTE_THEME_KEYWORDS = {
    "妊娠・出産": ["妊娠", "出産", "つわり", "妊婦", "産休", "陣痛", "エコー写真", "赤ちゃんが生まれ", "予定日"],
    "恋愛・結婚": ["結婚", "婚約", "プロポーズ", "入籍", "彼氏", "彼女", "恋人", "婚活", "デート", "パートナー"],
    "病気・介護": ["入院", "手術", "通院", "介護", "診断", "抗がん", "リハビリ", "看病", "持病", "余命", "病室"],
    "仕事・昇進": ["昇進", "昇給", "出世", "内定", "ボーナス", "賞与", "昇格", "評価面談", "転職成功"],
}


@functools.lru_cache(maxsize=1)
def load_taxonomy() -> dict:
    with open(GENRES_PATH, encoding="utf-8") as f:
        return json.load(f)


@functools.lru_cache(maxsize=1)
def _flat_subgenres() -> list[dict]:
    """[{major, genre_id, genre_name, subgenre_name, sensitive, mute_theme, keywords}] を返す。"""
    out = []
    for g in load_taxonomy()["genres"]:
        for s in g["subgenres"]:
            out.append({
                "major": g["major"],
                "genre_id": g["id"],
                "genre_name": g["name"],
                "subgenre_name": s["name"],
                "sensitive": bool(s.get("sensitive")),
                "mute_theme": s.get("mute_theme"),
                "keywords": s.get("keywords", []),
                "description": s.get("description", ""),
            })
    return out


def genre_index() -> dict[int, dict]:
    return {g["id"]: g for g in load_taxonomy()["genres"]}


# ---------------------------------------------------------------------------
# (c) キーワード方式（MeCab/fugashi の原形化つき・完全オフライン）
# ---------------------------------------------------------------------------

_KANJI_RE = re.compile(r"[一-鿿]")
_STOP_KANJI = set("私日今朝夜時間人事気")  # ありふれすぎて手がかりにならない字


def _kanji(s: str) -> set[str]:
    return set(_KANJI_RE.findall(s or "")) - _STOP_KANJI


@functools.lru_cache(maxsize=1)
def _tagger():
    """fugashi(MeCab) の Tagger。

    辞書のロードはメモリを食う（小さいマシンだと OOM しうる）ので、既定では使わず
    HAPIDIARY_USE_MECAB=1 のときだけ有効にする。未導入・無効時は None を返し、
    classify_keywords は部分一致＋漢字重なりのみで動く。
    """
    if os.environ.get("HAPIDIARY_USE_MECAB", "").strip() not in ("1", "true", "yes", "on"):
        return None
    try:
        import fugashi
        return fugashi.Tagger()
    except Exception:
        return None


# 手がかりにならない語（助詞・助動詞・ごく一般的な動詞名詞）は原形集合から除く
_STOP_LEMMA = set("するなるいるあるれるられること事もの物今日昨日日今朝夜時人")


def _lemmas(text: str) -> set[str]:
    """文を MeCab で分かち、内容語の原形（＋表層）を集合で返す。MeCab が無ければ空集合。"""
    tg = _tagger()
    if tg is None or not text:
        return set()
    out: set[str] = set()
    for w in tg(text):
        feat = getattr(w, "feature", None)
        pos = getattr(feat, "pos1", "") if feat else ""
        if pos in ("助詞", "助動詞", "記号", "補助記号", "接続詞", "フィラー", ""):
            continue
        lemma = (getattr(feat, "lemma", None) or w.surface) if feat else w.surface
        for form in (lemma, w.surface):
            if form and len(form) >= 2 and form not in _STOP_LEMMA:
                out.add(form)
    return out


@functools.lru_cache(maxsize=1)
def _subgenre_lemma_sets() -> list[set[str]]:
    """_flat_subgenres() と同じ並びで、各サブジャンルの keyword 原形集合を返す。"""
    sets = []
    for sub in _flat_subgenres():
        s: set[str] = set()
        for kw in sub["keywords"]:
            s |= _lemmas(kw) or {kw}
        s |= _lemmas(sub["subgenre_name"] or "")
        sets.append(s)
    return sets


def classify_keywords(text: str, max_tags: int = 3) -> dict:
    t = (text or "").lower()
    text_lemmas = _lemmas(text)
    lemma_sets = _subgenre_lemma_sets() if text_lemmas else None
    scored = []
    for i, sub in enumerate(_flat_subgenres()):
        score = 0
        for kw in sub["keywords"]:
            if kw and kw.lower() in t:
                score += 2
        # ジャンル名・大分類名そのものが含まれていれば弱く加点
        if sub["genre_name"] and sub["genre_name"] in text:
            score += 1
        # MeCab 原形の重なり（活用の揺れ「歩いて⇔歩いた」等を吸収）
        if lemma_sets is not None:
            score += 2 * len(text_lemmas & lemma_sets[i])
        if score:
            scored.append((score, sub))

    # 完全一致で何も拾えなかったときは、キーワード／ジャンル名の漢字と
    # 投稿文の漢字の重なりで“いちばん近い”ものを推定する（活用の揺れに強い）。
    if not scored:
        text_kanji = _kanji(text)
        if text_kanji:
            fuzzy = []
            for sub in _flat_subgenres():
                ref = _kanji(sub["genre_name"]) | _kanji(sub["subgenre_name"] or "")
                for kw in sub["keywords"]:
                    ref |= _kanji(kw)
                overlap = len(text_kanji & ref)
                if overlap:
                    fuzzy.append((overlap, sub))
            fuzzy.sort(key=lambda x: x[0], reverse=True)
            scored = fuzzy[:max_tags]

    scored.sort(key=lambda x: x[0], reverse=True)
    picked = scored[:max_tags]

    tags = []
    max_score = picked[0][0] if picked else 0
    for score, sub in picked:
        tags.append({
            "major": sub["major"],
            "genre_id": sub["genre_id"],
            "genre_name": sub["genre_name"],
            "subgenre_name": sub["subgenre_name"],
            "confidence": round(score / max_score, 2) if max_score else 0.0,
            "mute_theme": sub["mute_theme"],
        })

    mute_candidates = _detect_mute_themes(text)
    for tg in tags:
        if tg["mute_theme"]:
            mute_candidates.add(tg["mute_theme"])
    sensitive = bool(mute_candidates) or any(
        s["sensitive"] for sc, s in picked
    )
    return {
        "tags": tags,
        "sensitive": sensitive,
        "mute_candidates": sorted(mute_candidates),
        "method": "keyword",
    }


def _detect_mute_themes(text: str) -> set[str]:
    found = set()
    for theme, kws in MUTE_THEME_KEYWORDS.items():
        if any(kw in text for kw in kws):
            found.add(theme)
    return found


# ---------------------------------------------------------------------------
# (a) LLM API方式
# ---------------------------------------------------------------------------

_PROMPT_TAXONOMY_CACHE: str | None = None


def _taxonomy_for_prompt() -> str:
    global _PROMPT_TAXONOMY_CACHE
    if _PROMPT_TAXONOMY_CACHE is not None:
        return _PROMPT_TAXONOMY_CACHE
    lines = []
    for g in load_taxonomy()["genres"]:
        subs = " / ".join(s["name"] for s in g["subgenres"])
        lines.append(f'{g["id"]}. [{g["major"]}] {g["name"]} : {subs}')
    _PROMPT_TAXONOMY_CACHE = "\n".join(lines)
    return _PROMPT_TAXONOMY_CACHE


DEFAULT_MODEL = "claude-haiku-4-5"

SYSTEM_INSTRUCTIONS = """あなたは日記SNS「ハピディアリー」の投稿分類エンジンです。
ユーザーが書いた「今日の小さな幸せ」の投稿文を読み、下の分類体系（6大分類・30ジャンル・150サブジャンル）に照らして分類します。

方針:
- マウントや比較をあおらず、投稿者本人が「何によって幸せを感じたか」で分類する。
- tags は確信度の高い順に最大3個。1件しか当てはまらなければ1件でよい。
- genre_id は分類体系の番号。subgenre_name はそのジャンルのサブジャンル一覧の語をそのまま使う。
- mute_candidates: 投稿が「妊娠・出産」「恋愛・結婚」「病気・介護」「仕事・昇進」のいずれかに触れていればその語を入れる。無ければ空配列。
- sensitive: mute_candidates が空でなければ true。"""


@functools.lru_cache(maxsize=1)
def _system_text() -> str:
    return (
        SYSTEM_INSTRUCTIONS
        + "\n\n# 分類体系（ジャンル番号. [大分類] ジャンル名 : サブジャンル / …）\n"
        + _taxonomy_for_prompt()
    )


_MUTE_ENUM = list(MUTE_THEME_KEYWORDS.keys())

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "genre_id": {"type": "integer", "minimum": 1, "maximum": 30},
                    "subgenre_name": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["genre_id", "subgenre_name", "confidence"],
                "additionalProperties": False,
            },
        },
        "sensitive": {"type": "boolean"},
        "mute_candidates": {
            "type": "array",
            "items": {"type": "string", "enum": _MUTE_ENUM},
        },
    },
    "required": ["tags", "sensitive", "mute_candidates"],
    "additionalProperties": False,
}


def _classify_model() -> str:
    return os.environ.get("HAPIDIARY_CLASSIFY_MODEL", DEFAULT_MODEL)


def _preview_model() -> str:
    return os.environ.get("HAPIDIARY_PREVIEW_MODEL") or _classify_model()


def classify_llm(text: str, model: str | None = None) -> dict:
    import anthropic  # 遅延import（オフライン運用時に不要）

    model = model or _classify_model()
    client = anthropic.Anthropic()

    kwargs = dict(
        model=model,
        max_tokens=600,
        # 分類体系は毎回同じなのでキャッシュ対象にする（prompt caching）
        system=[{
            "type": "text",
            "text": _system_text(),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": f"次の投稿を分類してください。\n\n投稿文:\n{text.strip()}",
        }],
    )
    # 構造化出力でJSONの形を保証する。Haiku 4.5 は output_config.effort 非対応。
    output_config = {"format": {"type": "json_schema", "schema": RESULT_SCHEMA}}
    if "haiku" not in model.lower():
        output_config["effort"] = "low"
    kwargs["output_config"] = output_config

    try:
        resp = client.messages.create(**kwargs)
    except TypeError:
        # SDK が output_config を知らない場合はスキーマ無しで（_extract_json が受ける）
        kwargs.pop("output_config", None)
        resp = client.messages.create(**kwargs)
    except anthropic.BadRequestError as e:
        m = str(getattr(e, "message", e))
        if any(k in m for k in ("output_config", "format", "effort", "json_schema")):
            kwargs.pop("output_config", None)
            resp = client.messages.create(**kwargs)
        else:
            raise

    raw = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text")
    data = _extract_json(raw)

    gi = genre_index()
    tags = []
    for tg in data.get("tags", [])[:3]:
        gid = int(tg.get("genre_id", 0))
        g = gi.get(gid)
        if not g:
            continue
        sub_names = {s["name"] for s in g["subgenres"]}
        sub = tg.get("subgenre_name")
        if sub not in sub_names:
            sub = next(iter(sub_names)) if sub is None else sub
        sub_meta = next((s for s in g["subgenres"] if s["name"] == sub), None)
        tags.append({
            "major": g["major"],
            "genre_id": gid,
            "genre_name": g["name"],
            "subgenre_name": sub if sub in sub_names else None,
            "confidence": float(tg.get("confidence", 0.5)),
            "mute_theme": (sub_meta or {}).get("mute_theme"),
        })

    mute = set(data.get("mute_candidates", []) or [])
    mute |= _detect_mute_themes(text)
    for tg in tags:
        if tg["mute_theme"]:
            mute.add(tg["mute_theme"])
    sensitive = bool(data.get("sensitive")) or bool(mute)
    if not tags:  # LLMが何も返さなかった場合はオフライン方式で補完
        return classify_keywords(text)

    usage = getattr(resp, "usage", None)
    return {
        "tags": tags,
        "sensitive": sensitive,
        "mute_candidates": sorted(mute),
        "method": "llm",
        "model": model,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# (a) 埋め込み近傍検索方式
# ---------------------------------------------------------------------------

EMBEDDINGS_PATH = os.path.join(_HERE, "genres_embeddings.json")


def _embed_min_score() -> float:
    # 類似度がこれ未満なら「分類できなかった」扱い。実データを見て調整可。
    try:
        return float(os.environ.get("HAPIDIARY_EMBED_MIN_SCORE", "0.42"))
    except ValueError:
        return 0.42


def _embed_keep_gap() -> float:
    # 1位との差がこれ以内の候補だけを2〜3位として採用する。狭いほど余計な候補が減る。
    try:
        return float(os.environ.get("HAPIDIARY_EMBED_KEEP_GAP", "0.035"))
    except ValueError:
        return 0.035


def _norm(v: list[float]) -> list[float]:
    s = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / s for x in v]


# --- 埋め込みプロバイダ（Voyage / Gemini） ---------------------------------

def _provider_key(provider: str) -> str | None:
    if provider == "gemini":
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if provider == "voyage":
        return os.environ.get("VOYAGE_API_KEY")
    return None


def _gemini_embed_once(client, model, contents, cfg):
    """1回の embed_content 呼び出し。429 は retryDelay ぶん待って数回だけ再試行する。"""
    import time
    from google.genai import errors as genai_errors
    for attempt in range(4):
        try:
            return client.models.embed_content(model=model, contents=contents, config=cfg)
        except genai_errors.ClientError as e:
            if getattr(e, "code", None) != 429 or attempt == 3:
                raise
            delay = 20.0
            try:  # サーバーが返す推奨待機時間を使う
                for d in e.details.get("error", {}).get("details", []):
                    if d.get("@type", "").endswith("RetryInfo"):
                        delay = float(str(d["retryDelay"]).rstrip("s")) + 1
            except Exception:
                pass
            time.sleep(min(delay, 30.0))
    raise RuntimeError("unreachable")


def embed_texts(texts: list[str], *, provider: str, model: str, dim: int,
                is_query: bool) -> list[list[float]]:
    """texts をベクトル化して返す（正規化済み）。プロバイダごとに実装を切り替える。"""
    if provider == "gemini":
        import time
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=_provider_key("gemini"))
        task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        cfg = types.EmbedContentConfig(task_type=task, output_dimensionality=dim)

        # まずバッチ（1リクエストで複数ベクトル）を試す。件数ぶんのベクトルが返れば採用。
        if len(texts) > 1:
            try:
                r = _gemini_embed_once(client, model,
                                       [t.strip() or " " for t in texts[:100]], cfg)
                if len(r.embeddings) == len(texts[:100]):
                    out = [_norm(list(e.values)) for e in r.embeddings]
                    for i in range(100, len(texts), 100):
                        time.sleep(1.0)
                        rr = _gemini_embed_once(client, model,
                                                [t.strip() or " " for t in texts[i:i + 100]], cfg)
                        out += [_norm(list(e.values)) for e in rr.embeddings]
                    return out
            except Exception:
                pass  # バッチ非対応 → 1件ずつへ

        out = []
        for n, t in enumerate(texts):
            if n:
                time.sleep(0.75)  # 無料枠のレート制限（~100/分）に収める
            r = _gemini_embed_once(client, model, t.strip() or " ", cfg)
            out.append(_norm(list(r.embeddings[0].values)))
        return out
    if provider == "voyage":
        import voyageai
        vo = voyageai.Client(api_key=_provider_key("voyage"))
        r = vo.embed(texts=[t.strip() or " " for t in texts], model=model,
                     input_type=("query" if is_query else "document"), output_dimension=dim)
        return [_norm(list(v)) for v in r.embeddings]
    raise ValueError(f"unknown embedding provider: {provider}")


@functools.lru_cache(maxsize=1)
def _load_embeddings():
    with open(EMBEDDINGS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("provider", "voyage")  # 旧フォーマット互換
    data["vectors"] = [_norm(v) for v in data["vectors"]]
    return data


def embeddings_available() -> bool:
    if not os.path.exists(EMBEDDINGS_PATH):
        return False
    try:
        provider = _load_embeddings().get("provider", "voyage")
    except Exception:
        return False
    return bool(_provider_key(provider))


def classify_embed(text: str, max_tags: int = 3) -> dict:
    data = _load_embeddings()
    provider = data.get("provider", "voyage")
    model = os.environ.get("HAPIDIARY_EMBED_MODEL", data["model"])
    q = embed_texts([text], provider=provider, model=model, dim=data["dim"], is_query=True)[0]

    scores = []
    for i, v in enumerate(data["vectors"]):
        scores.append((sum(a * b for a, b in zip(q, v)), i))
    scores.sort(reverse=True)

    min_score, keep_gap = _embed_min_score(), _embed_keep_gap()
    top = scores[0][0]
    if top < min_score:
        return {"tags": [], "sensitive": False, "mute_candidates": sorted(_detect_mute_themes(text)),
                "method": "embed", "model": model, "top_score": round(top, 3)}

    picked = [scores[0]]
    for sc, i in scores[1:max_tags]:
        if sc >= min_score and (top - sc) <= keep_gap:
            picked.append((sc, i))

    tags = []
    for sc, i in picked:
        meta = data["index"][i]
        tags.append({
            "major": meta["major"],
            "genre_id": meta["genre_id"],
            "genre_name": meta["genre_name"],
            "subgenre_name": meta["subgenre_name"],
            "confidence": round(float(sc), 3),
            "mute_theme": meta.get("mute_theme"),
        })

    mute = _detect_mute_themes(text)
    for tg in tags:
        if tg["mute_theme"]:
            mute.add(tg["mute_theme"])
    return {
        "tags": tags,
        "sensitive": bool(mute),
        "mute_candidates": sorted(mute),
        "method": "embed",
        "model": model,
        "top_score": round(top, 3),
    }


# ---------------------------------------------------------------------------
# ディスパッチ
# ---------------------------------------------------------------------------

def available_mode() -> str:
    """使う分類方式を返す: 'embed' / 'llm' / 'keyword'。"""
    forced = os.environ.get("HAPIDIARY_CLASSIFY_METHOD", "").lower()
    if not forced:
        forced = os.environ.get("HAPIDIARY_CLASSIFY_MODE", "auto").lower()  # 旧名の互換
    if forced in ("embed", "llm", "keyword"):
        return forced
    if embeddings_available():
        return "embed"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "llm"
    return "keyword"


def estimate_tokens(text: str) -> int:
    """日本語まじりの文のトークン数のざっくり見積り（Gemini/Voyage 共通の目安）。"""
    return max(1, round(len(text or "") / 1.8))


def classify(text: str, preview: bool = False) -> dict:
    """投稿文を分類して {tags, sensitive, mute_candidates, method, latency_ms, tokens_est, api_calls} を返す。

    preview=True は「書きながら」のリアルタイム分類。
    どの方式でも、失敗したら必ずキーワード方式へフォールバックする。
    """
    import time
    if not text or not text.strip():
        return {"tags": [], "sensitive": False, "mute_candidates": [], "method": "none",
                "latency_ms": 0, "tokens_est": 0, "api_calls": 0}

    mode = available_mode()
    t0 = time.perf_counter()
    result: dict
    try:
        if mode == "embed":
            res = classify_embed(text)
            res.setdefault("tokens_est", estimate_tokens(text))
            res.setdefault("api_calls", 1)
            if not res["tags"]:  # 類似度が低くて分類できなかった → キーワードで補完
                kw = classify_keywords(text)
                if kw["tags"]:
                    kw["method"] = "keyword(embed low-confidence)"
                    kw["tokens_est"] = res["tokens_est"]   # 埋め込み呼び出しは実行済み
                    kw["api_calls"] = res["api_calls"]
                    result = kw
                else:
                    result = res
            else:
                result = res
        elif mode == "llm":
            model = _preview_model() if preview else _classify_model()
            result = classify_llm(text, model)
            result.setdefault("tokens_est", estimate_tokens(text) + 400)  # +分類体系プロンプト概算
            result.setdefault("api_calls", 1)
        else:
            result = classify_keywords(text)
            result.setdefault("tokens_est", 0)
            result.setdefault("api_calls", 0)
    except Exception as e:  # API障害・キー不備・SDK非対応 → オフライン方式へ
        result = classify_keywords(text)
        result["method"] = f"keyword(fallback from {mode})"
        result["error"] = str(e)
        result.setdefault("tokens_est", 0)
        result.setdefault("api_calls", 0)

    result["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return result


if __name__ == "__main__":
    import sys
    sample = " ".join(sys.argv[1:]) or "朝ごはんに焼きたてのトーストを食べたら、バターがじゅわっと染みて幸せだった。"
    print(json.dumps(classify(sample), ensure_ascii=False, indent=2))
