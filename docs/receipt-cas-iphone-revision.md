# PR #200：private確定記録とiPhone通常セル編集

2026-11-07。前提：Cloud Runなし、金融NO-GO、本番設定・実Sheets・Drive原本を変更しない。旧[初期合成設計](receipt-sheet-validation-only.md)の跨サービス原子性と1セル10項目形式を、この文書で置き換える。

## APIの確認結果

| 論点 | 公式仕様から確認できた範囲 | 判定 |
|---|---|---|
| Sheets内の一括更新 | [`spreadsheets.batchUpdate`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)は指定された1つのSpreadsheet内でrequestを一緒に適用する。共同編集による変更も明記され、request bodyにexpected revisionのCAS条件はない | privateファイル保存との一括commitを提供するAPIではない。旧reserveの「private＋Sheets UUIDの同時確定」は撤回 |
| ScriptLock | [LockService](https://developers.google.com/apps-script/reference/lock/lock-service)は同じscript等のコード区間の並行実行を制御する | 人間のSheets編集・sortやActionsの別HTTP要求を止める分散ロックと解釈しない |
| onEdit | [edit event](https://developers.google.com/apps-script/guides/triggers/events#edit)のvalue/oldValueは単一セルの値。row全体のタップ時snapshotはない | 通常TRUE＋座標だけの要求は停止。後からその行を読んで案件を推測しない |
| 専用チェック値 | [`requireCheckbox(checkedValue, uncheckedValue)`](https://developers.google.com/apps-script/reference/spreadsheet/data-validation-builder#requireCheckbox(Object,Object))は指定値をチェックボックス表示にできる | `register:<UUID>` / `ready:<UUID>`を機械設定する案。本人にUUIDを入力させず、event.valueからprivate固定snapshotへ結合できる見通し。iOSで実際に両値が届くことは未検証、欠落時停止 |
| private単一ファイルCAS | [Drive v2 File](https://developers.google.com/workspace/drive/api/reference/rest/v2/files)にetag、[files.update](https://developers.google.com/workspace/drive/api/reference/rest/v2/files/update)にcontent更新がある。repoの`app/pdf_intake_registry.py`に既存v2 PUT＋If-Match/read-back実装がある | **再利用候補は既存の単一JSON条件付き更新だけ**。etagの存在・既存コード・成功read-backだけでは、対象upload URLでのstale拒否まで実証できない。実接続は未承認の隔離probeで確認するまで停止 |
| 結果表示 | [`values.append`](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append)は表末尾に値を追加できる | 案件行へ座標指定で結果を書かず、自己完結したkey＋結果を表示cacheに追加し完全一致lookupする案。cacheは確定記録ではない |

**到達点**：不可能な跨サービスCASは不要になった。Google APIへの接続見通しは「既存Drive v2の単一privateファイルCAS＋custom-value checkbox＋Sheets結果cache」。認証付きGoogle APIのwrite実験は、変更禁止範囲へ入るため今回は実施していない。実API CASを実証済みとは報告しない。Drive v3のversion値やIf-Matchの送信だけを、検証済みCASの代用にしない。新DB、OAuth、Cloud資源の追加案は作らない。

## 確定記録と障害時の境界

1. 下書きの独立読取後、UUID・案件・原本ID/hash/version・context・商品operations・digestを**同じprivate journal**にCAS保存（内部state `下書き準備済み`）。この段階では登録要求ではない。Sheetsへの専用チェック値表示は後から行うprojectionであり、失敗してもprivate ticketを書き戻したり登録したりしない。
2. 単一セルの登録eventから専用UUIDを抽出する。oldValueが同UUIDのready値と一致しない、通常TRUE、複数セル編集、対象sheet/column不明なら停止。`readCaseAtEvent(row)`を廃止し、private ticketの案件IDで独立再読取する。別行へsortされても別案件を選ばない。valueが証明するのは専用ticketの選択であり、e.user・installer・Google本人の金融承認ではない。
3. private ticketと独立再構築したsnapshotが一致した時だけ、private CASで `下書き準備済み→受付済み`。**ここだけが登録の確定点**。SheetsのUUID欄書込みは条件にしない。同UUID、同snapshot別UUID、同案件/原本の受付中・検証中・UNKNOWNは拒否する。
4. CAS保存の読戻し成功後だけUUIDを1回dispatch。dispatch失敗/応答喪失ならprivate受付は保持し、再送・checkbox解除・別UUIDでの迂回をしない。CAS保存自体が不明ならdispatchせずUNKNOWN。後続の任意表示失敗はdispatchや検証の再実行理由にしない。
5. ActionsはUUIDから**同じprivate journal**を読み、`受付済み→検証中`をCASで一度claim。別の要求保存領域やSheetsのsubmission欄を要求しない。旧`execute_request`は以前の合成回帰ハーネスとして残し、接続入口は`execute_accepted_request`に限定する。
6. 既存validatorで検証し、結果をprivateへCAS/read-back。結果は金融authorityにならない。旧フラグ変更による再実行・金融write経路なし。
7. 最終結果の表示cacheにUUID・案件・source・snapshotを含む自己完結したkey/valueを追加する。通常の案件セルへ書かない。表示側は完全一致keyでjoinし、欠落/衝突を「結果表示待ち」にする。cache再取得はprivateのread-only照合であり、登録/dispatch/検証をやり直さない。

**受付後の編集**：処理対象はprivate固定snapshotだけ。独立検証前にSheet入力が変われば保留。検証済み結果の後に編集された下書き・メモはそのまま保持し、結果は登録時snapshotを示して「未登録の変更あり」と区別する。入力と結果を1つの更新トランザクションにしない。

**結果cacheの配置案**：既存「確認」だけを可視タブに維持し、非表示の機械用cacheを分ける案。本番/承認済みブックに新タブを作っていない。表全体のsortでcacheキーが動いてもキーと値の組でlookupし、case行の座標へ書戻さない。cacheは所有者が改変できる表示であり金融承認に使用しない。同keyで異なる値があれば表示を止め、privateへ照合する。受付済み/検証中はprivate照会の一時表示、永続結果cacheは不変の終端結果だけを扱う。

## iPhone商品編集の具体案（適用前）

「確認」1タブ・1行1案件・案件ID/見出し固定・原本リンク・既存入力/メモを維持。変更するのは次の列だけ。PCメニュー、サイドバー、Webサーバー、Webフォームを追加しない。

| 表示する欄 | iPhoneの操作 |
|---|---|
| 支払総額／修正後合計／差額／登録準備・結果 | 先頭で比較。支払総額は原本照合済みの同じ値を再入力しない |
| 商品1、商品2…の列グループ | 右へスワイプ。名称・数量・行金額はそれぞれ普通のセルで編集。quantityを再乗算せず行金額を合算 |
| 商品カテゴリ | 既存マスタの「食費／食品」等のdropdownを選択 |
| 扱い | dropdown「採用／除外」。行や商品セルの削除から除外を推測しない |
| 原本照合 | 変更・追加・除外した商品でチェック。本人認証ではなく根拠申告として既存validatorへ渡す |
| 追加商品欄 | 機械生成した空き列グループへ名称・数量・行金額・カテゴリを入力。空き欄は商品数/金額に含めない |
| 税・値引き | 種類dropdown「商品／外税／値引き」と帰属「商品1」等を選び、原本照合をチェック。内税二重加算・符号・カテゴリ配賦は既存validatorが別検証 |
| 修正内容を登録する | 準備ができた専用チェックを1回タップ。内部値はUUID付きだが表示はチェックだけ |

長い商品ID、原本hash、区切り文字の手入力を廃止。`_item_id`とcase/source bindingは機械列に置き、`receipt_sheet_cells.py`が候補明細と照合して既存operationsへ変換する。新しい架空調整や税ルールは追加しない。従来1セル形式は旧テストfixtureのみ。

列グループは商品数に比例して横に長くなる。追加枠は合成例で1枠とし、足りなければ登録を保留する。既存データを上書きして枠を増やさない。ここはiPhone実機で操作負担を確認する対象であり、PC上の操作例を実機合格と扱わない。

**操作イメージ**：対象案件で「修正後合計1,230円／不足138円」→商品2の金額セル730を868へ→商品2の原本照合をチェック→合計1,368円/差額0円→自動準備完了後「修正内容を登録する」1回→同案件で結果。数量・カテゴリ・追加・除外・外税/値引きも同じ列グループ内の通常セルを使う。

## 今回の追加テスト

旧Python50件とJavaScript8件の検証意味を維持し、専用イベント値の契約に合わせてharnessだけ更新。追加はPython10件・JavaScript5件：private成功/表示失敗、dispatch拒否/応答喪失、イベント後sortと通常TRUE拒否、同UUID再実行、同案件別UUID競合、後の下書き保持、誤案件cache拒否、通常セルの自動ID結合/追加除外/税帰属、private準備不明時にcheckboxを公開しないこと。金融Sheet構築・socket接続のtrapは維持。ローカル関連Python80件/レシートJavaScript13件成功、失敗0。全体CI結果はPR本文へ記録する。

[操作プレビュー](receipt-iphone-cell-preview.html)はネットワークのないローカル合成例。実Sheetsや独自Webフォームではなく、通常セルの入力・合計・1回登録・後の編集保持を確認するための模型。構文・ネットワーク不使用は検査済みだが、描画の実端末確認はしていない。

## 実機への残作業と次の承認対象（各1つ）

- **残作業1つ**：隔離した完全合成fixtureで、単一private CASのstale書込み拒否→専用チェック値のiOSイベント→並べ替え→UUID受付→結果lookupまでを一貫して受入検証する。API CASが成立しなければそこで停止し、iPhone登録を有効にしない。
- **次の承認対象1つ**：上記1回の隔離合成受入試験セット（新しい「確認」1可視タブの合成ブック1冊＋専用private JSON1ファイル、既存資格で足りる範囲の検証用接続）だけ。具体的なID/ACL/trigger差分のレビュー後に適用する。本番・承認済み旧合成ブック・原本・台帳・main・権限/Secrets/Variables・validated SHAは対象外。新権限が必要なら追加せず停止する。

今回、上記資源や接続は作成していない。Google実APIへの変更要求、Cloud Run操作、新認証、金融write、本番設定変更は0。PR #200のDraftコード/合成fixture/説明だけを更新する。
