# token.json について

荒川区テニスコート予約 bot で使用する OAuth 2.0 トークン（token.json）のドキュメントです。

---

## 概要

- **用途**: Google Calendar API・Gmail API への認証
- **取得方法**: 初回実行時にブラウザで認可すると自動作成される
- **場所**: プロジェクト直下 `token.json`
- **スコープ**: `calendar.readonly` と `gmail.send` の両方を含む

---

## 取得手順（再発行時）

1. 既存の `token.json` を削除（またはバックアップ）
2. `credentials.json` をプロジェクト直下に配置
3. `python arakawa_gmail.py` または `python arakawa_selenium_check.py` を実行
4. ブラウザが開いたら Google アカウントでログインし、カレンダー・Gmail の権限を許可
5. プロジェクト直下に新しい `token.json` が作成される

---

## JSON ファイルの置き場所

`token.json` と `credentials.json` は **プロジェクト直下** に置く。

```
arakawa-bot/
├── arakawa_calendar.py
├── arakawa_gmail.py
├── arakawa_selenium_check.py
├── credentials.json   ← ここ
├── token.json         ← ここ（初回認証後に自動作成）
└── ...
```

---

## 構造（例）

```json
{
  "token": "xxx",
  "refresh_token": "xxx",
  "token_uri": "https://oauth2.googleapis.com/token",
  "client_id": "xxx.apps.googleusercontent.com",
  "client_secret": "xxx",
  "scopes": [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.send"
  ],
  "expiry": "2026-02-19T12:00:00.000000Z"
}
```

---

## GitHub Actions での利用

GitHub の Secrets に `GMAIL_TOKEN_JSON` を登録し、`token.json` の中身を**そのまま**貼り付けてください。

1. リポジトリ → Settings → Secrets and variables → Actions
2. New repository secret
3. Name: `GMAIL_TOKEN_JSON`
4. Value: `token.json` を開き、中身をすべてコピー＆ペースト

---

## 中身を置く場所（手動設定用）

以下のブロックに、`token.json` の中身を貼り付けてください。  
（ローカルでの復元や GitHub Secrets 登録の控えとして使用）

<!-- 以下に token.json の中身を貼り付け -->

```json

```

<!-- ⚠️ 本番トークンを貼り付けた場合、このファイルは .gitignore に追加するか、リポジトリにコミットしないでください。 -->
