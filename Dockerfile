FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV PYTHONUNBUFFERED=1
# Fly Volume を /data にマウントすればデータは永続化される。未マウントでも
# コンテナ内 /data に置くだけなので起動はする（再デプロイでデータは消える・仕様書7.5）
RUN mkdir -p /data
ENV HAPIDIARY_DB=/data/happidiary.db
EXPOSE 8080

# 起動時に DB を初期化（空ならデモデータ投入）してから gunicorn を起動。
# MeCab(fugashi)の辞書や埋め込み・各SDKでメモリを食うため worker は1本、
# 長め timeout（Gemini埋め込みの往復に数秒かかることがある）。
CMD ["sh", "-c", "python init_db.py && gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 --timeout 60 app:app"]
