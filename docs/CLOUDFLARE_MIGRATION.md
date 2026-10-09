# 荒川区bot：Cloudflare起動への切替

Cloudflare Workersで定期的にGitHub Actionsを起動します。取得・予約・Googleカレンダー参照・Gmail通知はGitHubに残します。都立botは変更しません。

## 現在の状態

この変更は設定とコードの準備です。Cloudflareへのデプロイ、トークン登録、実サイトでの取得・予約は未実施です。単体テストでは外部アクセス・予約・メール送信をモックしています。

CloudflareはUTCの `7,37 22-23,0-15 * * *` で、日本時間07:07〜翌00:37に30分ごとに起動します。初期設定は `ENABLED=false`、`BOOKING_ENABLED=false` です。

## 1. GitHub Secretsを用意する

[リポジトリのActions Secrets](https://github.com/seirantomo1999-afk/arakawa-bot/settings/secrets/actions)に以下を登録します。

| Secret | 内容 |
|---|---|
| `ARAKAWA_USER_ID` | 区の予約サイトの利用者ID |
| `ARAKAWA_PASSWORD` | 区の予約サイトのパスワード |
| `GMAIL_CREDENTIALS_JSON` | 既存のGoogle OAuthクライアントJSON |
| `GMAIL_TOKEN_JSON` | 既存のGoogle OAuthトークンJSON。Gmail送信・Calendar読取の両方が必要 |

予約サイトの認証情報が公開コードに含まれていたため、パスワードを変更した値を登録してください。コードから削除しても過去のコミットの値は消えません。値をチャットやIssue、PR本文に貼る必要はありません。

既存のGoogle Secretsは再登録不要です。ただし権限不足・失効の場合は `docs/TOKEN_JSON.md` に従ってローカルで再認証します。CI上では対話認証を開始しません。

Secretsを登録してからPRをmainへマージします。登録前にマージすると資格情報検証で停止します。

## 2. 予約をしない手動確認

GitHubのActions → `arakawa_park_notifier` → Run workflowで、ブランチmain、`source=manual`、`dry_run=true` を選びます。

- 区のサイトへのログイン・空き取得・Googleカレンダー参照を確認します。
- `dry_run=true` でも空き通知・エラー通知メールは送信します。予約ボタンは操作しません。
- 取得・カレンダー・通知に失敗するとActionsも失敗表示になります。
- カレンダーは従来と同じ `primary` を参照します。
- 予約候補は要件定義どおり、土日祝・実行日から4日以上先・指定開始時刻の2時間枠です。

このPRでは、元のデバッグ設定で許可されていた平日・任意時間枠の自動予約を無効にします。実サイトの予約完了画面はまだ検証していません。予約確認ダイアログには「予約」「よろしい」の両方が必要で、異なる場合は停止します。

## 3. Cloudflareへデプロイする

リポジトリをPCに取得し、ターミナルで `cloudflare` フォルダへ移動します。Node.js 22以上を使用します。

```powershell
cd cloudflare
npm ci
npx wrangler login
npm run check
npm run deploy
```

`npm run check` はビルド確認で、デプロイしません。`npm run deploy` は初期設定のままなら定期起動が来てもGitHubを起動しません。公開HTTPエンドポイントも作りません。

[GitHubのFine-grained token作成画面](https://github.com/settings/personal-access-tokens/new)で、対象をこの `arakawa-bot` だけに限定し、Repository permissionsの **Actions: Read and write** を付与したトークンを作成します。有効期限を設定し、期限切れ前に更新します。

そのトークンをCloudflare Secretへ登録します。

```powershell
npx wrangler secret put GITHUB_TOKEN
```

入力プロンプトにトークンを貼り付けます。トークンを `wrangler.jsonc` のvarsやソースへ書かないでください。秘密情報はCloudflareの起動トークンとGitHubの予約・Google資格情報に分けて管理します。

## 4. 起動元を切り替える

1. GitHubの[Actions Variables](https://github.com/seirantomo1999-afk/arakawa-bot/settings/variables/actions)に `SCHEDULER_MODE=cloudflare` を設定します。
2. `cloudflare/wrangler.jsonc` の `ENABLED` を `true` に変更します。`BOOKING_ENABLED` は `false` のままにします。
3. `npm run deploy` を実行します。設定の反映には最大15分ほどかかる場合があります。
4. 定期実行がGitHubに `workflow_dispatch` として現れ、`Run notifier` が成功することを確認します。
5. 30分間隔で複数回起動されていることを確認したら、`BOOKING_ENABLED=true` に変更して再度デプロイします。

`SCHEDULER_MODE=cloudflare` では、GitHubのschedule由来のジョブはスキップされます。Cloudflareのdispatchと手動実行を受け付けます。Cloudflareへ切り替えてもGitHubの実行環境や区のサイトの障害は残ります。

Cloudflareの `accepted` ログは「GitHubが起動要求を受け付けた」ことを示すだけです。取得や予約の成功はGitHubの実行ログ・通知で確認します。queued/in_progressなどの実行が残っている場合は `busy` として起動要求を見送ります。予約中の実行を新規実行が強制キャンセルすることはありません。

GitHub起動APIへのPOSTは自動リトライしません。通信エラーでも起動済みの場合があるためです。Cloudflareのログに失敗が出た場合はActions履歴も確認してください。

## 5. 確認と元に戻す方法

確認項目：

- CloudflareのCron Past Events/ログで約30分ごとに起動を確認。
- GitHubの実行履歴で `event=workflow_dispatch` と時刻を確認。
- 同じ時間にscheduleとdispatchの両方の予約処理が動いていないことを確認。
- エラーでActionsが失敗表示になり、予約途中の場合は予約一覧を確認。

戻す場合は、Cloudflareの `ENABLED=false` にしてデプロイしたあと、GitHubの `SCHEDULER_MODE=github` に戻します。GitHubの元の30分スケジュールを使用しますが、以前の数時間の起動遅延が再発する可能性は残ります。

## テスト

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --test cloudflare/worker.test.js
```

参照：

- [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Cloudflare Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)
- [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
