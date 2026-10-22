# Google Sheets完結型 A方式（合成実装）

基点は #156 最終head `33a1c82bc71758e54142252110687b0695ee1a59`。
main `d62e1e254692e676f4e4a72dc593524a80fbea08`・継続仕様・#125/#127〜#156を確認した。
日常UIは正式「確認」1タブだけ。#156のSafari用画面は保存するが今回の操作経路から呼ばない。
本番・既存合成Driveブック・#153〜#156は無変更。全てローカル合成fixtureであり、新しいGoogleブックも作らない。

## 採用構成と判断

**既存Python backend内のpolling core + Sheets標準セル**を採用する。
`SheetsOnlyReview.poll()` は明示依頼の現在値を処理する内部メソッドであり、稼働scheduler・Cloud host・CLI自動実行入口は追加しない。
将来の既存非金融runnerへの組込みと実Google transportは別工程。今回それを有効化しない。

| 候補 | 判断 |
|---|---|
| simple onEdit | 不採用。最大2件のqueue、認証サービスの制限、ユーザー情報の条件付き取得がある。イベント列を正本にできない |
| installable onEdit | 不採用。実行権限は作成者。イベントuserも条件付きであり、編集者の金融承認に転用できない |
| Apps Scriptの定期poll/bridge | 今回追加しない。既存Python validator/CASを使うには別の認証bridge・秘密管理が必要で保守点が増える。作成者の実行権限で本人承認は解決しない |
| 既存backendのpolling | 採用。現在値からfresh Sheet/Drive/nativeを読み、既存Python/CAS/journalをそのまま使える。イベント欠落・重複・順序逆転に依存しない |

読取失敗でまだ依頼を受け付けていない場合は「受付待ち」。次のpollは読取だけからやり直せる。
writeの受付結果が不明ならUNKNOWNで停止し、結果読戻しだけ。利用者に同じ依頼を再送させない。
比較・結果投影は金融処理ではなく、backend保存状態の表示。Sheetの結果セルを正本にしない。

## セル配置と操作（実ブックは未変更）

既存8案件のfixtureから、案件ID直後にB「操作依頼」、C「比較・結果」を追加するオフライン投影。
元の17列は順序・値・式・注記・入力規則を保持。ID以外の元列は2列右へ移る。
保護範囲・tableの座標も明示変換し、元17列へ逆投影したsnapshotを既存RawReaderへ渡す。
新しい現物を読んで契約を再発行することはない。新列を切り捨てる前にブック/タブ/ヘッダ/構造/全行を確認する。
既存RawReaderの行追加・削除・並替え・重複・原本/注記/規則変更の拒否は緩めない。

A/B/Cは88/132/146px、WRAP・TOP。390pxで依頼と結果をIDの隣に表示する提案であり、元の入力欄はその右側。
入力編集時の横スクロールは残るが、比較・結果のためのタブ移動やSafari遷移は不要。
幅・位置・プルダウンのモバイル描画は実機未確認。format planは合成データのみで、Google APIへ適用していない。

1. 「確認」で案件IDを検索し、既存Drive原本リンクを確認する。
2. 日付・店舗/施設・金額・カテゴリ等、既存欄を編集する。医療は完全手入力。
3. Bの標準プルダウンで **比較 1** を選択する。受付待ちの後、Cへ比較版・原本版・原本ID/hash・入力の修正前後・明細・メモを表示する。
4. Cの比較を読んで、Bで **確定 <8桁の比較識別子>** を別操作で選択する。
5. Cへ受付・保留・競合・UNKNOWN・承認未成立を表示する。現状は本人承認が成立しないため、正常な確定依頼も **承認未成立・未記帳**。
6. 修正後は次の **比較 2** を選ぶ。古い比較依頼/識別子は再利用しない。UNKNOWNでは新しい依頼を送らず照合を待つ。

セル式はIF/AND/EXACT/CHARだけ。入力変更は「入力変更・比較を依頼してください」、未処理の新しい選択は「受付待ち」と表示する。
式の数値/文字列比較は操作案内だけであり、backendのdigest・版照合の代わりではない。
文字列の引用符と改行はエスケープし、メモを実行可能な式として連結しない。
独立したオフライン計算エンジンで式の実計算を検証する。**Google Sheets自身の計算結果ではない**。

## 本人承認についての結論

**今回のセル/pollingモデルでは、既存の本人承認基準を満たさない。金融承認は成立しない。**
Google SheetsアプリでGoogleへログインして編集できることと、backendがその人の特定の比較版への明示承認を検証できることは異なる。

- 既存ownerはGoogle issuer/subに結び付き、600秒の認証session、Origin/CSRF、exact比較proofを検証する。
- セルsnapshot/pollingには、その編集者の署名済みissuer/sub・本人session・CSRF・比較版への意思表示がない。
- e.user/getActiveUserのメールが取得できない場合がある。共有ACL・記入名・隠しセル・注記・状態値・installable triggerの実行者をその代わりにしない。
- workerの認証はworkerの権限であり、本人の同意ではない。設定済みownerは保存領域のnamespaceであり、編集者を名乗る根拠ではない。
- 新しい疑似session/CSRF/承認token/認証基盤は作らない。既存の認証済み`prepare/post/_session`をworkerから呼ばない。
- 比較識別子は案件/比較版の選択用。秘密でも承認権限でもない。

Workspaceの限定条件で編集者情報が取れる可能性はあるが、それだけで既存基準の成立を認定しない。
将来金融接続するには、実際のアカウント条件で、編集者を安定したGoogle主体へ結び付ける保証、改ざん不能な依頼attestation、
案件/原本/入力/比較版/期限への厳密なbind、replay拒否、権限分離を実証する必要がある。
メール取得の一般的保証はなく、この経路を今回「実現可能な承認方式」として採用していない。
証明できない限りSheetsは下書き・比較・承認依頼まで。Safari切替や共有権限だけで問題を回避しない。

## 既存再利用と安全な状態遷移

| 既存成果 | 今回の使用 |
|---|---|
| #125 / 継続仕様 | 1タブ・1行1案件・原本リンク・入力保持。今回の明示指示を第11節に追記 |
| #127/#128 | RawReader、8案件・入力/構造/原本identityの独立検証。新列は厳密な投影で分離 |
| #129〜#131 | 認証ホスト/旧Unitの境界を確認。旧PDF identityを画像へ流用しない |
| #130 | PersistentJournal/EncryptedDraftIOの既存CAS、read-back、履歴を再利用。`details.sheets_only_review`へ依頼状態を保存。新DBなし |
| #132〜#145 | purpose/native/ownership/guarded writerの境界を維持。writerやqualificationを新経路から呼ばない |
| #146/#155 | `_capture`でfresh二重Sheet読取・Drive revision bytes/hash/native照合、DraftCASでメタデータ保存、既存exact native比較 |
| #147〜#153 | 従来4処理と新R1のNO-GOを継承。監査結果・validated SHAは無変更 |
| #154 | 歴史的4タブは無変更。正式1タブのみの投影を合成検証 |
| #156 | 取込/比較/別の明示操作/保存結果照会の分離と状態契約を継承。HTML/JS/hostは削除も日常導線への追加もしない |

比較依頼はjournalへATTEMPTINGをCAS保存し、新規CAS成功時だけ既存draft CASへ進む。結果の確定もその新規CASを取得した処理だけに限定する。
同じ要求を見た別workerの事前検証失敗は、既存のATTEMPTINGやterminal結果を上書きしない。
これは認証未検証のSheet下書きの**メタデータ保存**であり、本人承認や金融処理ではない。
既存明細ID/カテゴリ/状態は独立backend下書きから保持する。
保存後に再導出したexact native snapshotをjournalへ保存する。部分医療入力は保存し必須不足で保留する。

確定依頼は同じ案件・最新比較識別子・600秒期限・freshな全8案件のsnapshot・原本・native版・保存下書き・比較表示式を照合する。
一致しても`APPROVAL_UNESTABLISHED`。資格付与・authority発行・金融writerには到達しない。
一般カテゴリのreceipt一括値を複数明細へ配賦せず、未入力明細は保留。
PDF、G103/H301、旧G102/M102は確定不可。医療AI送信経路はない。

同一依頼は同じ案件/command/snapshotのIDに帰着し、重複poll・連続タップで再importしない。
同時workerは既存journal/draft CASで最大1回。未知の結果や永続ATTEMPTINGはプロセス再構築後も全案件を停止する。
結果照会はjournalだけを読む。結果CASの応答消失でもterminalが独立読戻しできれば、その保存結果だけを採用する。
過去#155のATTEMPTING/UNKNOWNも停止条件に含め、保存済み「確認済み・未記帳」は既存版の結果として表示し、今回のセル入力への承認へ転用しない。

## Sheet投影と実接続の限界

Cの結果式とBの入力規則だけを更新し、Bの選択値・入力欄・原本・注記・保留値を書き換えない。
write前後のsnapshot/read-backで同時編集を検出し、比較を最新と誤表示せず競合へ切り替える。
応答不明の投影は同じ式/規則の読戻しで照合し、同じwriteを自動再送しない。
結果投影がまだ確認できない間、backendの保存済み結果と画面投影の状態は区別する。

**実Google Sheetsへbackend journalと同等の原子的CASがあるとは仮定しない。**
本番用publisher・行移動との競合対策・独立read-back・投影の不明状態の運用は未実装。
ownerの直接編集やsortをセル保護だけで防ぐことも保証しない。構造/IDが変わればbackendで採用を拒否し、入力を別案件へ移植しない。
Sheetsに残った結果値・式・注記を権限の正本として採用しない。

## 合成検証と実機未確認

新機能試験は同じ8案件、既存原本bytes/native readers/journal/CASを使用し、Google/Firestore client、認証gate、金融writer、資格付与をfail-on-callで禁止。
workerはmemory-only surfaceのSheet/source読取メソッドへ明示接続されたRawReaderだけを受け入れ、任意のGoogle callbackへの差替えを拒否する。
金融native/shared/ledger/authority portと認証保存先の前後不変を確認する。
入力→比較→別の確定依頼→承認未成立表示、医療部分保存、一般の明細保持、PDF/旧保留、原本/版/別案件/構造変更、
同時2/16 worker、イベントなし/重複/順序逆転、期限切れ、通信断、再読込み相当のworker再構築、UNKNOWN保持を検証する。

390px/PCの画像は**合成fixtureセルの静的な表示検証**。新しい日常HTML UIではなく、ルート/メニュー/画像ボタン/HTMLダイアログを追加していない。
既存#156のブラウザー回帰も維持する。Ubuntuでは既存のloopback-only namespace内でChromiumを実行し、外部通信を遮断する。
画像・計算値・境界counter・JUnit・namespace証拠をCI artifactへ保存する。

ローカル最終検証：新機能/式/CAS/CI境界63、既存回帰442、既存ブラウザー34、静的セル表示10、計549 passed。
compile/whitespaceも成功。最終headのUbuntu結果は独立Draft PRのCIとartifactで確認する。

次の実機項目は未確認：実Google Sheetsの式再計算、プルダウンの表示/選択、セル編集後の同期、offline/再起動/再ログイン、
縮小倍率・キーボード・横スクロール・Drive原本往復・同時編集時の遅延。
別承認の隔離実機環境を準備した後、上記1〜6の手順と通信断/UNKNOWNの停止を確認し、機種/iOS/Sheets版と結果を記録する。

## 本番利用までの最小工程

1. 本Draftのレビュー、合成配置の実機受入。実Sheetsへの変更・接続は別承認で合成専用から行う。
2. 実Sheet/Drive/nativeのread-only transportと、backend CAS/ACL/鍵/耐久性・結果投影read-backを実証し、既存非金融runnerへpollingを組み込む。
3. **本人承認が成立する保証を得るまで下書き/比較運用に限定**する。Sheets標準操作だけで保証できなければ金融接続は保留を継続する。
4. 条件が証明できた場合だけ、旧writerの競合排除・実行権限・目的境界を確認し、金融canary/authority・merge/deployを個別承認する。

従来4処理・新R1はNO-GO。本番認証/Google transport/金融write/authority発行/原本移動/AI送信/Cloud/IAM/Secrets/Variables/validated SHA変更0。

## 公式仕様（2026-10-22確認）

- [Session.getActiveUser / getEffectiveUser](https://developers.google.com/apps-script/reference/base/session)：ID取得が許可されない場合、メールは空文字。installable triggerの実効主体は作成者。
- [Event objects](https://developers.google.com/apps-script/guides/triggers/events)：editのuserはセキュリティ制限による条件付き。value/oldValueも単一セル編集に限定される。
- [Simple triggers](https://developers.google.com/apps-script/guides/triggers)：onEdit queueは最大2件。API/スクリプトによる編集はtriggerを発火しない。
- [Installable triggers](https://developers.google.com/apps-script/guides/triggers/installable)：作成者権限で実行する。編集した人の承認を意味しない。
- [Menus / drawings](https://developers.google.com/apps-script/guides/menus)：画像/図形へ割り当てたスクリプトはmobileで起動しない。今回依存しない。

「既存owner/session/CSRFの本人承認がこのセルモデルでは成立しない」は、この公式制約と既存実装を照合した設計判断である。
