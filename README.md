# ハピディアリー（プロトタイプ）

完全匿名・マウントの生まれない、幸せの記録・共有SNS。
世界観は「灯と流れ（灯籠流し）」——投稿は小さな灯を川に流すこと、受け取りは流れてきた灯をすくうこと。

技術仕様書（`ハピディアリー_技術仕様書.md`）のプロトタイプ版に沿った実装です。

## 何ができるか（仕様書3章・6章）

| 画面 | 内容 |
|---|---|
| はじめに `/welcome` | 共感したいジャンルの選択、属性タグ（匿名）、ミュートテーマの設定 |
| ホーム `/` | 「わたしの幸せ」＝自分の投稿ログ、「どこかの誰かの幸せ」＝共感ジャンルに合う匿名投稿。過去の自分の灯の再浮上つき |
| 書く `/post` | テキスト入力 → AIがジャンルをリアルタイム推定 → センシティブ判定トグル → 「幸せを流す」 |
| 傾向 `/analysis` | 「10年前／5年前／いま」でジャンル別内訳を横棒グラフ表示。似た傾向の人の「未来の幸せ提案」＋「気になる／興味なし」 |
| アーカイブ `/archive` | 匿名アイコン、ジャンル絞り込み、過去投稿のグリッド |
| 設定 `/settings` | 読みたい設定（よく見たい／普通／少なめ／見ない）、ミュート設定、属性タグ、アカウント（任意） |
| ログイン `/login` `/signup` | 任意。別端末から同じ記録に戻るためのアカウント。ユーザー名は画面非表示 |

### ログイン（任意機能・追加）
仕様書の「完全匿名」を保つため、ログインは**アカウント復旧・複数端末用**にとどめています。

- `/signup` … いま使っている匿名ユーザーにログイン情報を紐づける（投稿履歴を引き継ぐ）
- `/login` `/logout` … 別端末から同じ記録に戻る／匿名に戻る
- `/account/password` … パスワード変更（設定画面から）
- **保存方式**：ユーザー名は平文で保存せず、サーバー鍵付きハッシュ（HMAC-SHA256、鍵 `HAPIDIARY_LOGIN_KEY`）にして保存・照合に使う（不可逆・大文字小文字は無視）。パスワードは werkzeug の scrypt で一方向ハッシュ。
- ユーザー名は他ユーザーに一切表示されず、投稿にも結びつきません。匿名のままでも使えます。
- 全 POST フォームに CSRF トークン検証あり（`/api/classify` のみ除外）。
- `HAPIDIARY_LOGIN_KEY` は本番では必ずランダム値に。**実ユーザー登録後に変更すると既存アカウントのユーザー名照合ができなくなります。**
- 簡易ブルートフォース対策（失敗時 0.3 秒待機）のみ。本格的なレート制限は今後の課題。

### 守っている設計原則（仕様書2章）
- **完全匿名**：ログインは任意。未ログインなら訪問ごとにセッションへ匿名IDを発行するだけ。ハンドルネーム・アイコン・実名は持たず、ログイン名もDB上はハッシュのみで画面には出さない。
- **数値非表示**：他者投稿にリアクション数を出さない。自分の投稿だけ「共感／発見」を控えめに表示（仕様書6.1）。フォロワー数の概念なし。
- **到達の平等**：タイムライン表示時に `post_reach` へ記録し、1投稿が届く人数を `REACH_LIMIT`（既定5）で固定。拡散を強化する操作を用意しない。
- **1日の表示件数を固定＋ランダム**：日付固定シードで10件前後を選ぶ。無限スクロールなし。
- **見るだけの利用も許容**：投稿していないユーザーでも共感タイムラインを閲覧・リアクションできる。

## セットアップ（ローカル・Windows）

前提：Python 3.11

```powershell
# プロジェクトフォルダで
python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt

# （任意）AI分類にLLM APIを使う場合。未設定ならキーワードマッチングで動く
copy .env.example .env    # .env を編集して ANTHROPIC_API_KEY を設定

# DB初期化（デモユーザー・デモ投稿を投入）
python init_db.py

# 開発サーバー起動
python app.py
#   → http://127.0.0.1:5000
```

macOS / Linux は `source venv/bin/activate`、`flask run`（`FLASK_APP=app.py`）でも可。

### DBを作り直す
```
python init_db.py --reset       # 既存DBを消して再作成＋デモ投入
python init_db.py --no-seed      # スキーマだけ
```

### 動作確認のヒント
- 右上「設定 → このブラウザの匿名の灯をリセットする」で、新しい匿名ユーザーとしてやり直せます（`/reset-me`）。
- 「傾向」の 5年前／10年前 は、デモデータの日付分布によっては空になります。

## AI分類（仕様書7.3）

`classify.py` が投稿文を「大分類 → ジャンル → サブジャンル」でタグ付けします。

`HAPIDIARY_CLASSIFY_METHOD=auto`（既定）なら次の順で自動選択し、失敗したら下へフォールバックします。

- **(a) 埋め込み近傍検索（推奨・最安）**：`VOYAGE_API_KEY` があり `genres_embeddings.json` が存在するとき。
  Voyage AI（`voyage-4-lite`, $0.02/1Mトークン・200Mトークン無料）で投稿文をベクトル化し、
  事前計算した全サブジャンルのベクトルとコサイン類似度を取って上位を採用。
  分類体系を毎回送らないのでLLMより桁違いに安い（1投稿あたり実質ゼロ円）。
  - 準備：`pip install voyageai` → `VOYAGE_API_KEY` 設定 → `python genres_build_embeddings.py`（1回）
  - しきい値は `HAPIDIARY_EMBED_MIN_SCORE` / `HAPIDIARY_EMBED_KEEP_GAP` で調整可
- **(b) LLM方式（精度最優先モード・任意）**：`ANTHROPIC_API_KEY` があるとき。
  分類体系をシステムプロンプトに入れ **prompt caching**、**構造化出力**（`output_config.format`）で受け取る。
  `HAPIDIARY_CLASSIFY_MODEL`（確定時・既定 `claude-haiku-4-5`）と `HAPIDIARY_PREVIEW_MODEL`（書きながら）で分離。
- **(c) キーワード方式（完全オフライン）**：APIキーが無いとき。`genres.json` の `keywords` ＋
  **MeCab（fugashi）で原形化**してスコアリング（「歩いて⇔歩いた」等の揺れを吸収）。完全一致で拾えなければ漢字の重なりで推定。
- (a)(b) がエラー・低信頼のときは自動的に(c)へフォールバックします。

## 分類マスタ `genres.json`（仕様書4章）

6大分類・30ジャンル・約150サブジャンル（運用で随時追記）。各サブジャンルに「説明／幸せ投稿の例／センシティブ・ミュート候補フラグ／keywords」を持ちます。
`happiness_taxonomy_30x5.xlsx` が入手できなかったため、仕様書4章の記述に沿って `genres_build.py` で構築しています。

```
python genres_build.py   # genres.json を再生成
```
実データの xlsx が手に入ったら、同じ形（`major_categories` / `mute_themes` / `genres[].subgenres[]`）に変換して差し替えてください。

## ファイル構成（仕様書7.6）

```
HapiDiary/
├── app.py            # Flaskアプリ本体・ルーティング
├── models.py         # データモデル（User/Post/Reaction/Suggestion 等・sqlite3直叩き）
├── classify.py       # ジャンル分類（埋め込み / LLM / キーワード＋MeCab）
├── genres.json       # 分類体系データ（30ジャンル・150+サブジャンル）
├── genres_build.py   # genres.json の生成スクリプト
├── genres_build_embeddings.py  # 埋め込み方式の genres_embeddings.json を作る（要 VOYAGE_API_KEY）
├── genres_embeddings.json      # 全サブジャンルの事前ベクトル（生成後にコミット可）
├── seed_corpus.py    # 「小さな幸せ」オリジナル短文コーパス（450件）
├── init_db.py        # DB初期化＋デモデータ投入（--add-corpus で追加投入）
├── requirements.txt
├── Dockerfile        # Fly.io用
├── fly.toml          # Fly.io用
├── templates/        # Jinja2テンプレート（各画面）
├── static/           # style.css / script.js
└── happidiary.db     # SQLite（実行時に生成）
```

## デプロイ（Fly.io・仕様書7.5）

```bash
fly launch          # 既存の fly.toml / Dockerfile を使う
fly secrets set ANTHROPIC_API_KEY="sk-..."     # LLM方式を使う場合
# データを永続化するなら:
fly volumes create happidiary_data --region nrt --size 1
fly deploy
```

`fly.toml` は `HAPIDIARY_DB=/data/happidiary.db` を指定済み。ボリューム未作成でも起動はしますが、再デプロイでデータは消えます（プロトタイプなので可）。
起動時に `init_db.py` が走り、DBが空ならデモデータが入ります。

## 制限（プロトタイプ）

- 認証なし。1ブラウザ＝1匿名ユーザー（Cookieセッション）。
- 「過去の自分の再浮上」はランダム日付のみ（仕様書3.6の範囲）。
- 「未来の幸せ提案」は属性・ジャンル傾向の単純な突き合わせ。
- 画像アップロードは未対応（添付はURL文字列のみ）。
- スケーラビリティ・決済は対象外（仕様書0章）。
