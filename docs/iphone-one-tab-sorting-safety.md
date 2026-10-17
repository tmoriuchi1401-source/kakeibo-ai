# 1タブ確認UI：並べ替え安全性の判断（Phase 2）

2026-10-17。完全合成の独立メモリモデルのみ。本番・承認済み合成ブック・Drive資源の作成／変更は0。実Sheets API／iPhone上での保護動作を追加検証した記録ではない。採用済みの「確認」1タブを維持する。

## 調査と選択

公式資料を2026-10-17に確認した。

| 根拠 | 読み取れる仕様と判断 |
|---|---|
| [ProtectedRange](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/sheets#ProtectedRange) | 保護はrange／unprotectedRangesという座標。案件IDへの自動結合契約はない。全シート保護＋必要入力セルの例外を採用候補とする。 |
| [CellData](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/cells#CellData) | 値・note・dataValidationはセルの属性。保護とは別管理。注記もauthorityにしない。 |
| [SortRange／MoveDimension等](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#SortRangeRequest) | ソート・行移動・挿入・削除は別操作。どの操作でも案件に従って保護が追従するという保証を置かない。値・注記移動と保護残留の問題はユーザー報告を起点に再現したモデル。入力規則の実移動挙動は今回未実測。 |
| [保護の制約](https://support.google.com/docs/answer/1218656?hl=en) | 所有者は編集可能。警告だけの保護は編集阻止にならない。所有者の直接編集／並べ替えを完全禁止できない。 |
| [batchUpdate](https://developers.google.com/workspace/sheets/api/guides/batchupdate) | バッチ内の依存更新はまとめられるが、管理状態・利用者編集を含むトランザクションとは扱わない。 |

| 候補 | 安全性・保守負担 | 判断 |
|---|---|---|
| IDで保護・規則を自動再配置 | 動的範囲、変更検知、競合、途中入力救済、応答不明の処理が必要。再配置前に誤編集される窓が残る | 本番自動修復は採用しない |
| アプリ管理操作だけのソート | 全行・注記・規則・保護・下書きを一括で再構成し、編集中排他と読戻しが必要。ownerは依然回避可能 | 将来必要時に別承認で検討 |
| 検索中心・通常並べ替えを提供しない | 新しいサーバー／Apps Script不要。構造変化時は停止し、正しい下書きだけから再生成 | **推奨**。今回のコードはそのオフライン契約 |

## 実装した境界

`app/iphone_review_drafts.py`はクライアント・資格情報・writerを持たない。既存のStateError、統合reviewのdigest、LEGACY_UNMAPPED_HOLDを再利用する。既存writer／authority／本番runnerは無変更。

- `IssuedProjection`は管理側の発行済み行順・世代・制御情報。シートの隠しセルを信頼しない。行順は構造変更検出用であり、案件の恒久identityではない。
- `binding`は案件ID＋原本ID/hash/version＋page identity＋Unit/index＋種類／purpose。原本が変われば旧下書きを新identityへコピーしない。
- `capture`は2回の同一読取り、案件集合・順序、immutable表示、保護／規則を検証し、全行成功後にだけ下書き更新案を返す。行追加・削除・並べ替え・世代不一致は取込み停止。未送信の値を上書きしない。
- G103は保留と既存入力値を保持し、保留解除はセル編集では不可。H301はLEGACY_UNMAPPED_HOLDのまま、Unitを捏造せず全欄操作不可。一般／医療のPDF欄、PDFの会計欄は対象外。
- `regenerate`は信頼済みcanonical情報と保存済みjournalをbindingで結合。削除・版変更前のjournalも保持。無効な現シートを自動修復して取り込まず、原状を隔離し、人が比較する。
- `validate_handoff`は下流でidentity・種類・保留・snapshotを独立再検証。成功してもauthorized=false、operation_targets=[]。
- `protection_request`は全シート保護と種類別入力例外の**プレビューのみ**。既存ブックの列仕様を推測した適用は禁止。実列の対応、厳密入力規則、editor ACLの読戻しは将来adapterで必要。値・注記・規則の抽象モデルをAPI実検証済みとは呼ばない。

## 合成テスト結果と限界

`python -m pytest -q tests/test_iphone_review_drafts.py`：**38 passed**。ソート／挿入／削除／重複ID／部分identity移動／版変更／再生成／途中入力・注記／保留／対象外欄／保護・規則ずれ／同時更新／古い世代／formula拒否／下流検証を含む。同時更新CASはメモリ契約試験であり、SheetsがCASを提供するという証明ではない。

値だけを別案件と交換し、どちらも有効な入力になる操作は、正当な二つの手修正と区別できない。ownerによる保護回避、読み取り間のABA変更、最後の読取り後の編集も完全検知できない。**任意編集可能なSheetsを入力provenance／確定権限の安全境界にしない**。検索中心・構造検知・独立validatorに加えて、確定時に案件ID・原本・正確な入力snapshotを表示し直し、認証済み本人の明示POSTへ固定する必要がある。

この制約下でも下書きは保存できるが、並べ替え可能な本番UIの完成、ライブ再生成の競合安全性、実Sheetsの保護修正完了とは扱わない。これらの制約を越える安全性は主張せず、**本番接続ゲートは閉じたまま**、Phase 3では既存処理のread-only／dry-run設計だけを検証する。

最小追加作業：承認後の独立Sheets検証、raw CellData・注記・規則・保護・列schemaの厳密reader、保存済み下書きの永続CAS、編集停止を伴う再生成と読戻し・隔離復旧、確定時snapshot再確認。新規Driveブック作成、実ブック変更、main merge、本番有効化はそれぞれ別承認。
