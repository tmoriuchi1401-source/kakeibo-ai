# 銀行収入recurring接続（write既定OFF）

## 接続先と現在の状態

銀行専用 `bank-pdf-recurring.yml` と統合親 `kakeibo-production.yml` は、
同じ `app.cli bank-pdf-recurring` → `run_bank_pdf_recurring` を使用する。
新しいschedulerは作らない。両入口の最上位は共通 `kakeibo-production` concurrency、
main限定。統合親は旧入口停止Variableを必要とし、二重起動を許可しない。

銀行専用の旧入口は停止Variableで無効化する。統合親の06:47 JST
`drive_bank` scheduleは銀行applyを選択する。手動実行は引き続き
`mode=apply` と `bank_apply=true` が揃った場合だけ銀行applyを選択する。
収入フラグをtrueにしてもpreviewをapplyへ昇格させない。
統合親のvalidated main SHA guard、共通排他、durable stateは維持する。

## 保存と再開

収入フラグOFFでは収入を書かない。PDFのprocessed移動前には、
確認済み入金の既存取込行と収入行をread-backし、未反映分があればInboxに残す。
ONかつ新しい明示的収入scopeがある場合は、既存parser/daily previewと
`BankIncomePipeline` の確認済みルール・銀行取引種別判定を再利用する。

1. InboxのPDFを既存 `max_files` 上限内で再検出し、保存済み取込行も読む。
   checkpointを通過した未archive PDFも対象にする。新規writeはauthorityの日時窓内に限る。
2. 正の銀行入金を既存A:LへRAW保存する計画を作る。確認済みは `bank_income`、
   要確認は `needs_review`、非収入は `bank_non_expense`。判定理由を備考に保存する。
   統合先支出ID欄は空欄のまま。既存行の状態・リンク・備考を上書きしない。
3. parse問題・ID衝突のPDFはInboxに保留する。従来のファイルreviewガードを維持し、
   保留ファイルの確定候補もreview状態で保存する。支出の保留を解除しない。
4. 新規保存入金・未反映収入・新規支出のsource IDの和集合を、
   **従来のrun上限 `max_rows`（最大100）** 内に収める。
   一つの入金の保存＋収入反映は一つのsource IDとして数える。
   収入用に別の100件枠を増設しない。上限超過ではprocessed更新も含めてGoogle write前に停止する。
5. 入金をappendし全列read-back後、保存行から `BankIncomePipeline.apply` で収入A:Jへ差分appendする。
   支出は従来のauthority・writer・条件をそのまま使用する。

新着0件でも保存済み未反映収入を計画に含める。
processed PDFも取得した期間内では入金を再確認し、旧処理で保存されなかった入金を拾える。
その場合、支出を再実行せず既存processed markerも更新しない。
期間外の旧PDFはread-onlyで再照合し、追加writeが不要と確認できた場合だけ移動する。
未保存の過去分には別のauthorityまたは個別調査が必要。processed更新は入金保存・収入read-back成功後なので、
後続の支出失敗でも取込行から再開できる。

Payrollを収入へ加算せず、transfer・返金・借入・未確認入金を収入にしない。
同日同額の別IDは維持し、同じIDの内容衝突を拒否する。
結果の件数は入金保存・収入作成・支出作成・reviewを分け、親summaryにも収入件数を渡す。

## 中断と成否不明

新DBは作らず、既存 `bank-recurring.sqlite3` のappend-only `recurring_runs` に
append直前の対象・全列と、read-back成功の記録を残す。既存Drive state validatorと形式は互換。
どちらのappendも、記録前に書込みを開始しない。

- 次回、未完了記録の全列が一意に存在すればread-backで成功を確認し、再appendしない。
- 行の欠落・一部だけ存在・内容不一致・重複は成否不明として停止する。推測による再追加やrollbackはしない。
- 取込append後の中断は保存行から収入へ進める。収入append後の中断は既存収入をskipする。
- 親のDurableState/ProductionLedgerがpendingの場合は、従来のread-only照合と明示的復旧手順が先。
  この接続は親のpendingを自動解除しない。履歴のない空のstateで収入writeは拒否する。
  旧入口のcacheを失った場合も無断初期化で復旧しない。
- 収入処理の失敗時は当該runのcheckpointを進めず、残りの支出/processed更新を止める。

詳細なappend証跡は既存の非公開state内にのみ保持し、Actionsに金額やID一覧を出さない。

## 本番有効化に必要な別承認

固定backfill承認は将来入金に流用しない。既存の保護された
`BANK_PDF_RECURRING_AUTHORITY_JSON` に、次の明示的scopeを追加する承認が必要。

| 項目 | 将来承認する値・範囲 |
|---|---|
| `income_enabled` | boolean `true` |
| `income_worksheet` | `収入明細` |
| `income_accounts` | 既存3銀行sourceと承認済み口座aliasの正確な組。ワイルドカード不可 |
| 保存・分類範囲 | 上記口座の入金保存、既存確認済み収入のみappend、全列read-back。review/非収入は保存のみ |
| 対象・上限・期間 | 既存固定Spreadsheet、既存 `max_rows` 共用、既存ファイル/期間/有効期限 |
| Repository Variable | `BANK_INCOME_WRITE_ENABLED=true`。未設定時はfalse |
| 正規入口 | 検証済みmain＋共通排他＋本番audit key。ローカルの収入applyは拒否 |

現在のauthorityはexpense-onlyのまま。新しいscopeなしでは、フラグだけtrueでも書込み前に拒否する。
候補ごとの手動approvalファイルを新設しない。

銀行の支出applyと収入applyは別scopeである。06:47の銀行支出applyは、
収入フラグOFFのままでも有効にできるが、収入未反映PDFをprocessedへ移動しない。
収入scopeの追加は別承認が必要。旧新両入口を同時に有効にしない。

## 検証

合成parser結果を実daily previewへ渡し、fake Sheets/Driveと実SQLiteを接続する。
収入writerは実 `BankIncomePipeline.apply`。混在テストの支出transportはfakeで、
既存支出writerの回帰は既存テスト群が検証する。
収入だけ・混在・要確認・部分失敗・新着0件・processed再読取り・重複期間・別ID・
default OFF・3銀行/口座・authority不一致・共通上限・RAW/read-back・状態形式・CLI/親境界を検証する。
実データはコネクタgetと保存snapshotのpreviewだけ。詳細結果はGit除外 `.private` に保存する。

停止位置：**銀行収入recurring接続・検証完了、収入writeはOFF、本番有効化承認待ち**。
