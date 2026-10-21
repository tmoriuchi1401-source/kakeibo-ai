# 正式「確認」1タブの下書き受渡しadapter（合成・金融境界停止）

2026-10-22。独立ブランチ`codex/iphone-one-tab-draft-handoff`。
基点はDraft #154固定head `b752954ca15324bcd198d8c9cfa43bf544573540`。
mainは`d62e1e254692e676f4e4a72dc593524a80fbea08`。
mainのAGENTS.md／継続仕様全文、第10節、#127〜#154の依存・状態と関連実装を確認した。
正式1タブ・4タブ合成ブック・#153監査・#154は変更しない。

## 到達点と境界

合成Raw CellDataの入力→fresh検証→既存R1下書きCASへの取込→正確な再表示用snapshot→
本人の明示POST→既存nativeによる再導出・照合→**金融境界で停止**→保存済み結果照会を接続した。
`FINANCIAL_BOUNDARY_STOPPED`は照合した下書き／比較snapshotの保存であり、記帳成功ではない。
本経路のnative authority・qualification・共有金融claim・writer dispatch・金融writeは0。

`staging_shadow/iphone_sheet_handoff.py:SheetDraftHandoff`と
`app/iphone_sheet_handoff_host.py:make_isolated_sheet_handoff_application`を明示注入する。
既定OFF、isolated_synthetic、memory runtime／journal／R1 portsを必須にする。
既存の認証host factoryは無変更。新規UI、Google client、production登録、金融writerは追加しない。
既存画面からこのAPIを呼ぶ実運用の配線は今回行っていない。

| API（全て既存ホストの合成namespace内） | 用途・作用 |
|---|---|
| POST `/prepare/<case_id>/import_draft` | freshな二重読取りと検証、取込前の値・原本版・proofを返す。書込み0 |
| POST `/request`、`confirmed=false` | 既存journalへ試行CAS→既存R1の非金融save_draft→読戻し→取込結果保存 |
| POST `/prepare/<case_id>/confirm_handoff` | 取込済みsnapshotと現在のSheets／下書き／原本／native／master／ledgerを照合し、比較を返す。書込み0 |
| POST `/request`、`confirmed=true` | 画面の案件ID・snapshotとproofを照合、試行CAS→fresh再検証→金融境界停止の結果保存。writer呼出しなし |
| GET `/results/<request_id>` | 保存結果だけを返す。live Sheet／原本観測・CAS・再送0。不明ならUNKNOWN |

共通prefixは`/isolated-synthetic/iphone-sheet-handoff`。POST bodyはJSON。
prepareのbodyは空object、requestは`proof / case_id / sheet_snapshot / confirmed`だけ。
取込と確定を同じPOSTにしない。GETでfresh captureや状態更新をしない。
全APIに既存Google本人認証・owner・browser/session・600秒TTLを適用し、POSTはOrigin／CSRFも検証。
no-store／no-referrer、認証前の下書き読取り禁止、入力・医療値のURL／errorへの出力禁止を維持する。

## 再利用した実装

- #127/#128 `RawReader`：8案件・列schema・順序・固定欄・注記・規則・保護・原本identity・二重読取り。
- #130 `PersistentJournal / EncryptedDraftIO`：既存DRAFT_ONLY暗号化domain、strong CAS、履歴、exact receipt、read-back。
  新DB／CAS engineは作らない。既存optional `confirmation` slotに本adapterの非金融記録を保存する。
  旧#131のacknowledgement slotとは同時共有しない。他schemaがあれば停止し、自動移行しない。
- #144〜#146 `OneTabConfirmation._draft/_session/_prepare`、`DraftReaders.observe`、`image_inspect`、
  `prepare_inputs`：画像whole-sourceの明細・native baseline・独立Drive revision／native version・原本bytes/hash・
  privacy状態・カテゴリmaster・全ledger・planを再導出。取込には既存`post(save_draft)`だけを使う。
- `SyntheticDraftConfirmation`と既存guarded writerは無変更・新経路からの呼出し0。
  確定時に`OneTabConfirmation.post(post)`へ流さず、native qualification前で停止する。
- 既存save_draftの結果に`newly_saved`を追加し、新規CAS成功と保存済み値のread-only読戻しを区別する。
  同じ値を別操作が先に保存していても、今回の取込成功として採用しない。既存の金融POST処理は無変更。
- #131一般fixtureはPDF Unit契約であり、画像whole-sourceへ流用しない。
  #147〜#152の金融mandatory gate／drainは今回越えない。#153のNO-GOを解消したとは扱わない。

## identity・入力・保留

管理側の発行済みcontractと既存R1 contextから対応を検証し、行番号・注記・確認状態をauthorityにしない。
Sheetの原本ID/hash/Drive revisionと、独立nativeのsource ID/hash/versionをそれぞれ照合する。
同じversion文字列だから一致したとは扱わない。画像は1ページ・whole-sourceの境界を維持する。
並べ替え・追加／削除・重複ID・別kind・原本差替え・原本bytes不一致・版変更・規則変更・
draft／master／ledger変化は拒否する。取込後にSheetsが変われば、新しい取込と再確認が必要。

一般のreceipt単位カテゴリは明細へ配賦しない。元の欄から変更されていれば拒否し、既存明細別確認を要求する。
明細ID・カテゴリ・入力状態は既存下書きのものを保持する。未確認明細をSheetの状態だけでenteredにしない。
医療はSheetから本人が全項目を手入力する。部分入力を保存できるが、必須項目不足では確定できない。
AI／OCR補完・外部送信経路はない。

G103/H301は表示／保留のまま。G102/M102の旧Unit／部分下書きは画像R1 contextに推測対応させない。
PDF P201/P202は構造未確定として保留し、本adapterからの構造確定・金融接続はしない。
正常系の画像一般G101・医療M101とは別に、全8案件のraw入力と保留をjournalへ保持する。

## CAS・重複・UNKNOWN

取込／確定ごとに、既存journalへ`ATTEMPTING`をCAS保存し、その成功した新規試行だけを次へ進める。
同じ要求の保存済みreceipt読戻しは、新しい実行許可にしない。2／16インスタンスでも次処理は最大1回。
取込順はUUIDの辞書順に依存せず、CAS revisionで判定する。過去結果・途中入力は保持する。
金融境界停止済みの同一案件は再確定を拒否し、同じ要求の照会だけを許す。

応答喪失をcatchして再送するloopはない。例外後の同一adapterはread-onlyとなる。
永続ATTEMPTINGがある場合は別instance・再構築後も追加取込／確定を拒否し、GETはUNKNOWNを返す。
最後の結果CASが実際には完了していれば、GETだけでその保存結果を確認できる。
最初の試行CAS自体が未確認で保存記録も見つからない場合も、GETはUNKNOWNとし自動再送しない。
既存入力を上書きする修復・pending解除・自動再試行・自動的な新要求作成は実装しない。

Sheets、journal、既存R1下書きは単一transactionではない。
前後読取りで観測した競合を拒否するが、最後の読取り以後の任意編集まで排他したとは主張しない。
将来の金融実行には改めて全snapshot／native／ownershipのfresh検証と既存必須gateが必要。

## 検証と未確認

新規suiteは正常な一般・医療、部分保存・再入力・再構築・結果保持、行／案件／kind／原本／版／bytes／
page count／master／native draft変更、別owner／期限／CSRF／Origin、明細状態・カテゴリ非配賦、
2／16並列、journal意図保存前後・下書きCAS前後・結果CAS前後の応答喪失、UNKNOWN保持を検証する。
本経路のnative／shared／ledger／authorityポートとwriter入口はfail-on-callで遮断する。
実ホスト試験は既存RSA署名・owner/session/CSRF検証を通し、金融writeなしで往復する。
既存reader／journal／原本観測／明細／医療手入力／認証／workflow境界とsave_draftの回帰325件も実行する。
件数・最終headのUbuntu CI結果はPR checks／JUnitを参照。古いCI成功を今回の新規成功へ加算しない。

Windowsの固定bundleはrepo外かつ許可作業フォルダ内の一時領域で構築した。hash条件・旧ソースは無変更。
Google／Firestore／実Sheets／実OAuthへの接続検証ではない。合成memory API payloadをfreshに読み取る試験である。
iPhoneのSheetsアプリでのAPI呼出し配線・原本往復・日本語キーボード・倍率・再起動・通信再同期・再認証の
新しい実機受入は未確認。実機がなくても独立して進められるadapterと自動検証を完了対象にした。

## 本番までの残課題と承認

1. 正式1タブ／既存本人確認画面からの明示取込・比較・確定・結果照会の呼出し配線と実機受入。
2. 実Sheet・独立Drive revision bytes・native版・master／ledgerのread-only transport、実backendのCAS／ACL、
   domain専有・鍵管理・耐久性・保持期間・容量／UNKNOWNの運用手順。今回はlive transportを作らない。
3. 本番金融接続前に#153の未確認事項と旧writer競合排除等のNO-GO条件を、将来の個別承認に基づいて解決する。
   新R1だけを有効化して旧入口を迂回しない。PDF金融接続は別工程。
4. main merge、本番認証／接続、金融authority／write、限定canary、deployはそれぞれ別承認。

今回の本番変更・金融write（新経路はmemory金融writeも0）・原本移動・AI送信・実データ取得／転用・
Variables／Secrets／validated SHA更新・Cloud/IAM・workflow dispatch/re-run・main mergeは0。
4タブと正式1タブのDrive資源、PR #153/#154は変更しない。従来4処理と新R1のNO-GOを維持する。
