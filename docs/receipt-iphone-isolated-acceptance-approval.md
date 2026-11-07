# PR #200：iPhone隔離合成受入試験・資源と変更範囲の承認案（2026-11-08）

## Gate A 実施結果（2026-11-08、作成・owner側独立読戻し）

**実施範囲**：ユーザーのGate A承認後、既存の承認済みprivate親フォルダだけを利用して、新規3資源を1件ずつ作成した。作成はそれぞれ1回のみ。既存CAS probe JSONは読取のみで一切変更していない。

| 論理資源 | 実施結果 | owner接続での独立確認 |
|---|---|---|
| BOOK | 新規native Google Sheets 1冊。初期シートを「確認」に変更、唯一の可視タブ。商品編集欄やcheckbox、結果cacheは**未設定** | 新規実ID・単一parent・MIME・owner＋既存SA writer ACL、tab metadata一致 |
| SOURCE | 新規完全合成PNG 1件。元画像には実額500円/868円/計1,368円を表示し、意図的なAI誤読候補730円は画像に印字しない。**画像だけを訂正した同一新規ファイル** | 新規実ID・単一parent・MIME・owner＋既存SA writer ACL、58,010 bytesのraw読戻し一致 |
| PRIVATE | 新規完全合成JSON 1件、217 bytes、`enabled=false`/`status=unconfigured` | 新規実ID・単一parent・MIME・owner＋既存SA writer ACL、raw読戻しの長さ/内容一致 |

- **Google Drive接続の制約**：owner OAuthでは「親IDを指定してnative Sheetsを直接作成」はサポートされなかった。このため、作成した新規ブックをownerのrootから**承認済みprivate parentへ移動し、旧root親を除去**。移動後の単一parentとACLを独立GETで確認した。既存ブックのコピー・改変はしていない。
- **既存SA自身の直接GET**：現在利用可能なGoogle Drive接続はowner側。owner側でSA writer ACLは確認したが、**既存SAの認証主体による直接metadata/media GET成功は、この実行では未確認**。Gate Aの読み取り判定は「owner側PASS、SA実読取PENDING」。SA書込可能性や実認証scopeまで成功と主張しない。
- 実ID・完全なACLメール・元画像bytes/hash・JSON内容等の詳細は非公開の実行記録に保持し、public PR本文に書き込まない。
- 旧CAS probe JSON・production_run.json・銀行/カード/Amazon/PayPayなどの既存資源への変更0。Apps Script project/trigger/PAT・GitHub Actions新dispatch・main/validated SHA・Cloud Run・金融write・AI送信・原本移動0。新規作成3件と、**その新規ブックの親移動/「確認」へのtab名変更、新規PNGの画像訂正**だけを実施した。
- **判定**：Gate Aの資源作成は完了。**SA直接読取は未検証につきGate B/CはNO-GO維持**。SAを独立主体として既存権限のまま読めなければ、追加権限を自動付与せず停止する。新規資源は削除せず保持する。


> **状態：計画のみ／作成・接続・dispatch未承認。** 2026-11-08の独立した単一JSON Drive v2 media PUT + If-Match CAS実測はPASS。PR #200はDraft、mainとvalidated SHA不変、金融write NO-GO。これはiPhone登録・Google Sheets連携・Python実adapterが実環境で成立した証拠ではない。

## 1. 目標と非目標

**最終受入目標**：iPhoneのGoogle Sheets「確認」1タブで、**完全合成一般レシート1件**の原本を開き、商品2の金額を730円→868円に修正し、明細合計を1,230円→1,368円、差額を138円→0円と確認して、UUID専用チェックを1回操作する。その後、独立原本読取とprivate CAS検証を通じ、同案件に**「検証済み・未記帳」**が表示されること。原本画像には実際の868円と総額1,368円を印字する。OCR保存候補の730円は「誤読を想定した合成値」と明示して作成し、金額の根拠を捏造しない。

**非目標**：実レシート、医療、既存金融台帳への記帳、既存writer・authorityの再開、Cloud Run、実本番Sheets、既存合成ブックの改変、旧CAS probe JSONの転用。本番接続は別工程・別承認。

## 2. 必要なGoogle Drive資源（新規最大3件、作成は別承認）

下記の**論理名・既存parentの照合**を承認対象とする。実IDは作成応答後に独立GETで確定し、private manifestに限定保存。公開GitHub PRに実ID・実メール・ETag・画像bytes・原本リンク・資格情報を転記しない。

| 論理名 | 予定名 | MIME | 用途 |
|---|---|---|---|
| BOOK | `KakeiboAI_PR200_iPhone隔離合成受入_20261108` | `application/vnd.google-apps.spreadsheet` | 「確認」1可視タブ、合成商品列、UUID専用checkbox、完全一致結果lookup。必要なら同ブックの非表示の表示cacheタブだけ追加 |
| SOURCE | `receipt-general-synthetic-20261108.png` | `image/png` | 新たに生成する完全合成の買物レシート。実一般レシート／既存owner-only PNGを流用せず、共有変更もしない |
| PRIVATE | `receipt-validation-iphone-acceptance-20261108.json` | `application/json` | 受付用private ticket・journal（単一ファイル強CAS）、受入用private case anchorの分離された論理領域を**実装・査読で成立させた場合のみ**収容。金融authorityの保存先にはしない |

- 保存先は、既にmetadata-onlyで存在と `canAddChildren=true`・owner＋既存SA writerを確認した**同一の既存privateフォルダ1か所**。parentの実IDは前回非公開承認資料から読み、推測しない。
- 3件とも既存ownerをowner、既存service accountをwriterとする**exact ACL**、単一parent、trash=false、対応MIMEをowner側・SA側の独立GETで確認。既存parent・既存資源へのACL追加・変更、Google IAM・Secret・OAuthスコープ追加は禁止。一致しなければ停止。画像はownerでiPhone閲覧可能、SAで実bytesを読めることを試験する。
- **既存CAS probe JSON** `receipt-validation-acceptance-20261108.json` は独立証跡として保持。再初期化・追記・journal用への転用はしない。
- `IndependentReceiptReader`の実装では`private_cases`（保護されたanchor）と`ValidationJournal`（CASで変化する受付履歴）の論理分離が必要。現時点の`validate_journal`は厳密な`schema/generation/requests`形式であり、**anchorを同じJSONへ無検証で追加できない**。新PRIVATEの単一ファイル内で安全に別namespace化できるか、または既存の許可済み保護anchorを安全に読めるか、コード上で確かめる。できなければ**第4資源を勝手に作らず停止**して差分と理由を報告する。
- 追加枠1つの合成レシート1件を最小受入ケースとし、別の+28円ケースは既存オフラインCIで維持。複数の新原本は今回は作らない。

## 3. スクリプト・権限・Actions経路の別ゲート

**資源3件の作成承認はApps Script/trigger/PAT/GitHub workflow/main変更の承認を含まない。** これらは次の条件付き追加ゲートとして、既存権限・実行主体・差分を明示後に個別承認する。

**Gate A：Drive資源の作成・read-only preflight**
1. 前回の非公開計画でparent・所有者・SAの一致を再確認し、同名資源の不存在を確認。上記3件だけ新規作成。実IDを独立GETで確認してprivate allowlistに固定。
2. 新PNGは完全合成・印字内容も原本とは独立に検証。SAから画像bytes/hash/version/parent/ACLが読めることを確認。合成bookは既存「確認」1タブ契約を維持し、内部タブは非表示のみ。
3. これを超えるSheets/JSONの設定・書込みは次のゲートへ持ち越す。既存CAS probeへの書込み0、本番金融write 0。

**Gate B：合成ブックのセル／iPhoneイベント**
1. PR #200の通常セル差分・機械ID、カテゴリdropdown、合成商品の追加枠、`register:<UUID>`/`ready:<UUID>` checkbox、結果cacheの完全一致キーを合成book内だけに設定する。
2. iPhone Sheetsアプリから**人の実タップ**で生成された単一セル`event.value/oldValue`を観測。通常TRUE、複数セル、欠落oldValue、別UUID、変更後sort/行挿入・削除はfail-closed。Apps Script `e.user`を本人認証とみなさない。Google APIによる`setValue()`では`onEdit`を発火できないため、プログラム上の擬似eventを実機合格としない。
3. この試験には専用Apps Script projectと、その**合成ブックだけを対象にするinstallable onEdit trigger**が必要になる可能性がある。実project数・trigger ID・実行者・OAuth scope・ログ内容・無効化/削除方法を明示して、**作成・接続前に別承認**。既存カテゴリ・手入力のスクリプト/triggerにコードを混ぜない。新資格・PATを無断発行／保存しない。
4. Gate Bでは金融write、prod workflow dispatch、既存実案件の読み込みを禁止。イベントだけ確認できてもbackend終端成功とは報告しない。

**Gate C：UUID→独立Python検証→結果の同案件表示**
1. 同一の保護private snapshotを参照し、実メタデータ・画像bytesと商品候補・anchorの整合を独立読取。単一PRIVATEへの強ETag/If-Match CAS（旧probeでPASSしたAPI経路）を実adapterで使用し、stale/競合/再送/応答喪失/UNKNOWNを検証。iPhoneセルの変更後に別案件へ結果を書かない。結果cacheは終端のみ、UNKNOWNは一時照会に限定。
2. 直接dispatchを行う前に、**専用の非金融・合成限定Actions実行経路**を査読。GitHub公式仕様上、新規`workflow_dispatch`を使うにはworkflow定義がdefault branchに存在する必要があり、Draft PRへの追加だけでは起動できない。main変更を避けられない場合はその**試験専用・Secrets不使用・実金融state非参照の差分**を示し、明示承認までdispatchせずに停止。既存金融workflowを合成UUID用に流用しない。PATが必要なら許可scope・保存・失効を提示し別承認。
3. 成功条件は同じcase/source/UUID/snapshotへの`検証済み・未記帳`表示、別案件/二重登録/UNKNOWN/遅延レスポンスによる誤表示0、金融writer呼出し0、対象外write 0。iPhone実機試験の観測は利用者が操作・確認した結果として記録する。

## 4. 明確な停止・復旧条件

- 既存owner／SAから新規source/image/JSONが想定ACL・親・bytesで読めない、CAS adapterで原子的な競合拒否と独立read-backが成り立たない、anchorとjournalが確実に分離できない、iOSの専用checkbox edit eventに必要情報がない、別案件へのbindingが不明な場合は**fail-closed**。
- JSONの応答がUNKNOWNでも同UUID・別UUIDの自動再実行・削除・rollback writeをしない。final cacheの欠落は表示待ちとして扱い、再dispatchしない。
- 既存bank/card/Amazon/PayPay/Payroll、Cloud Run手動0、main/validated SHA、GitHub Variables/Secrets、実データ、旧CAS probe、既存合成ブック/原本、既存triggerを**一切変更しない**。新規資源は削除せず保持し、削除・共有変更は別承認。
- Gate B/Cの専用triggerを将来停止する場合は**新規trigger IDだけ**を無効化し、既存triggerや金融writerには触れない。実API CASと実iPhone受入に成功しても本番writeはNO-GOのまま。

## 5. 今回の判断と次の承認単位

**この文書自体は計画提出のみ**。PR #200のDraft文書に保存する以外に、Drive/Sheets/Apps Script/Actions/Secrets/IAM/Cloud Run/金融環境は変更しない。実API CASのPASS（既存probe）を再試行・再実行しない。

次の最小承認対象は**Gate AのGoogle Driveファイル3件の新規作成と独立GET/SA読取まで**。Gate B（Apps Script trigger/実iPhoneイベント）・Gate C（試験用Actions dispatch/PRIVATE CAS受付）は、それぞれ対象・実行資格・差分が確定するまで明示NO-GO。3資源でprivate anchorの分離が成立しないなら、追加資源案を提示して停止する。
