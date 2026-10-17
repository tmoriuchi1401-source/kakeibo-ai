# Phase 4A：専用合成ブックの復旧と実Sheets構造変更検証

2026-10-18。Draft PR #128。対象は既存のPhase 4専用ブック
`1pmqtTu4NcNrZ-xwF7V0zyp6lKrIAMB8vUwUM8SDJeWg`だけ。
**条件付き復旧と、下記範囲の実Sheets検証は完了。最終状態はcanonicalの8案件へ復帰済み。**
本番接続、認証付き実POST、main統合、Cloud、有効な並べ替えUIの完成を意味しない。

## 4行目削除前の条件と復旧

直前にA1:Z1001をraw CellDataで読み、Git除外の
`.private/phase4a-live-recovery-20261018/`へsnapshotを保存した。
既に承認された1回のtitle-only取得で退避済みの保護設定も、内容・取得成功証跡を確認した。
この復旧ではtitle-only probeを再実行していない。

| 削除条件 | 結果 |
|---|---|
| 4行目の案件ID・原本ID・hash | 全て空欄 |
| C4 | canonical G103と同じ、既知の原本リンク数式だけ |
| 追加行の他の値・注記 | 0。未知の入力を削除しない |
| 既存8案件 | 停止時snapshot・canonicalのA:Qと全て一致 |
| journal | revision 1、request 1。保存済み8件のcellsと現入力が一致 |
| 共有権限 | ownerのみ・非公開。前回退避と一致 |
| 保護退避証跡 | 完全responseと前後照合結果が存在 |

全条件合格後、`deleteDimension`のROWS `[3,4)`だけで4行目を1行削除した。
セルの書き直し、注記・入力規則・保護設定・editor・共有権限の更新は行っていない。

削除後の実API read-backでは、以下が元のcanonical baselineと一致した。

- 8案件のID・種類・原本identity・全17列のraw値／数式・注記・入力規則。
- 40入力枠（空欄とG103保留中の6枠を含む）、原本7件の対応。
- 見出し、Table、1000×26のgrid、ID列・見出し行の固定設定。
- 全シート保護1件、入力例外34セル、ownerだけのeditor ACL。
- G103とH301の保留、両案件の編集例外0、H301のLEGACY_UNMAPPED_HOLD。
- journalのファイルbytes全体、revision 1、未送信入力、共有ACL。

復旧した実API payloadを既存readerへ渡し、8件を正常captureした。
下書き保存を再実行しておらず、journal revisionやrequest数は増えていない。

## 独立baselineからの構造変更negative test

各試験前に全snapshot・保護／Table metadataを退避した。
期待した構造変更は拒否・隔離を検証し、案件内容・注記・規則・journalが保存されていることを
確認してから、事前に定義した逆操作でbaselineへ戻した。
未知の入力の破棄や、不明な差分の自動採用・修復は行っていない。

| 試験／操作 | 実測と判定 |
|---|---|
| 復旧 | 不正追加行だけ削除。8案件・保護・journalが完全一致、reader合格 |
| 空の末尾行1000を削除 | rowCount 1000→999。8案件・保護例外・Table・保留入力は不変 |
| readerによる拒否 | `iphone_sheet_structure_changed`。隔離、下書き採用0 |
| 計画した末尾空行の逆挿入 | 空の直前行からdimension属性を継承。追加数式なし。baseline完全一致、reader合格 |
| A2:Q9を案件ID降順にsort | 8案件全列の値・数式・注記・規則は案件ID別で一致。**保護例外は元座標のまま** |
| readerによる拒否 | `iphone_sheet_case_order_or_identity_changed`。隔離、下書き採用0 |
| 計画した案件ID昇順への逆sort | 事前確認したcanonical順へ復帰。全cells・規則・保護・保留・journalが完全一致 |

実ブックへの構造変更は合計5回：復旧削除1回、negative操作2回、計画した逆操作2回。
本番・既存承認済みブックへの構造変更は0。

末尾行削除は、実案件を失わずにgrid削除を検出する試験である。
**実案件を削除して再作成する実Sheets試験は実施していない。**
案件削除・部分identity入替等の拒否は既存の合成reader testに含まれるが、実Sheetsの証拠と区別する。

## 並べ替え時の保護のずれ

降順sortでは、136セル（8案件×17列）の規則metadata（未設定セルを含む）が全て案件ID別で一致した。
保護例外34セルの座標は不変で、G103／H301の移動先にそれぞれ7個の編集例外が重なった。
値や原本対応が同じでも、種類別・保留欄の編集制御は安全ではなくなることを実測した。
この状態は予定したnegative testとして隔離し、下書きへ採用しなかった。

逆sortは、8案件の全値・数式・注記・規則がbaselineと案件ID別に完全一致し、
保護／Table自体は変わっておらず、journalもbyte単位で不変という条件を確認してから実行した。
最終的なG103／H301の編集例外は0へ戻っている。

**通常運用は検索を継続し、アプリ側へ並べ替え機能を追加しない。**
ownerの直接編集・sortを完全禁止できるとは扱わない。
セル保護に依存せず、管理側canonicalとreaderの行集合・案件ID・kind・原本identity・
入力snapshot・規則・保護の独立照合でfail-closedを維持する。

## 最終確認と証跡

構造変更後に実Sheetsを2回GETし、既存`RawReader`へ実API payloadを受け渡した。
原本7件のDrive revisionはこの2回読取りの前後に再確認し、全件が前回bytes/hash照合時と一致した。
今回、原本bytesを再ダウンロードしたり、一時URL・認証情報を保存したりしていない。

最終snapshotはcanonicalと完全一致、reader合格。`regeneration_preview`はpreview-onlyのまま。
journalはファイルbytes全体が試験開始前と同じで、revision 1・request 1・8件を維持。
未送信入力を消さず、実際の再生成やjournalへの書込みは行っていない。
共有ACLはownerのみ、非公開のまま。

private証跡には、各baseline、全read-back、2つの隔離snapshot、判定結果、journalの開始時copy、
`final-verification.json`を保存した。Git・PR・CIへraw snapshotやeditorの個人情報を持ち込まない。
Google-renderedのPC画面でも1タブ、固定列／見出し、原本リンク、保留行の表示を確認した。
iPhone実機での再検証を行ったという意味ではない。

## CIと今回の追加証拠の区別

- Phase 4B既存CIは114件のadapter＋417件の安全回帰＝531件PASS。
  [既存CI](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39070278156)。
- **今回の追加証拠は実Sheetsの復旧1件・negative test 2件・逆操作2回**。
  既存531件へ新しいunit testを追加したという数え方はしない。
- 今回のrepository差分はMarkdownのみ。処理コード・既存テスト・workflowを変更していない。
  報告commitに対する既存suiteのCI再実行結果はPR #128のchecksを参照。

## 残課題と次の承認

- 行挿入時の原本リンク自動補完は、Sheets側の式継承という観測まで。
  今回のTable外の末尾空行復帰では数式は増えなかったが、Table内部の計算列機構や停止設定を
  特定したとは扱わない。Table設定変更、表削除、UI再設計は別途承認が必要。
- iPhoneでは復旧済み専用ブックの検索・原本往復・入力保持・保留・横スクロール／固定表示を再確認する。
- 一般の複数明細カテゴリ、既存nativeのpage/Unit snapshot、Drive revision IDとnative versionの
  freshな実ホスト側対応付け、追加ユーザー認可、明示POSTと既存writer gate整合は次工程。
  実ホスト接続・本番有効化は別途承認が必要。

本番記帳、原本移動、AI送信、実authority更新、新規Drive資源、共有権限変更、Cloudは全て0。
#123/#124/#127を変更せず、#128もDraft維持。main merge・本番接続は行わない。
