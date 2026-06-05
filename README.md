# yan-flash-watch

[妖精乱舞 攻略ページ](https://yan-flash.com/ultimate/yosei-ranbu) の「更新履歴」セクションを
監視し、新規/変更があれば Discord に通知する。

## 仕組み

- GitHub Actions が **20分ごと**（`.github/workflows/watch.yml` の cron）にページを取得。
- 更新履歴は素の `<time>/<p>` タグとして HTML に含まれるため、`check.py` が `urllib` で取得・パース。
- 直前のスナップショット `state/latest.json` と比較し、`(日付, 本文)` が変わった項目を Discord webhook に送信。
- スナップショットは Action が repo に commit するので、**git history がそのまま更新ログ**になる。

> GitHub の scheduled workflow は混雑時に数分〜十数分遅延・スキップされ得る。厳密な 20 分保証ではない。

## セットアップ

1. Discord サーバーで webhook を作成（チャンネル設定 → 連携サービス → ウェブフック → URL をコピー）。
2. その URL を repo の Secret に登録:
   ```sh
   gh secret set DISCORD_WEBHOOK --repo leafylion/yan-flash-watch
   ```
3. 動作確認は Actions タブの **Run workflow**（`workflow_dispatch`）から手動実行。

## ローカル実行

```sh
DISCORD_WEBHOOK="https://discord.com/api/webhooks/..." python3 check.py
```

webhook 未設定でも実行可（通知だけスキップ）。初回は `state/latest.json` を作るだけで通知しない。
