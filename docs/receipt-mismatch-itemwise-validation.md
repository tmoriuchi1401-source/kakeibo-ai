# 一般レシート商品別修正：合成検証と未接続部分

2026-11-07。PR #196 の既存13コミットを保持して追補。main、PR #195、PR #196、AGENTS.md、両仕様を監査した。PR #195 は #196 の祖先であり、両PRはDraft・未merge。開始時 main は `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`、#196 は `48bc829a4a04c95b39e274fa53c888752ddc43e7`。

**到達点はローカル／CIの合成検証。iPhoneから本番登録できる完成版ではない。NO-GO維持。** 本番Sheets・原本・authority・validated SHA・Variables/Secrets・workflow dispatch・医療AI・金融writeは操作しない。

## 実装と再利用

- `receipt_item_correction_plan.py` の既存previewを修正。商品名・金額・数量、原本根拠付きの追加／除外、税・値引きの帰属を検証。数量を変える場合は行合計金額も明示確認する（amountは単価ではなく行合計）。架空差額行は拒否。名前による拒否は補助であり、原本の実在性の証明ではない。
- 既存 `verified_candidate_range` で候補ID・取込ID・解析hash・行順・支払総額を照合。登録snapshotには、除外前を含む候補全体・取込行・修正操作・修正後商品を結合する。原本のbytes hashとは別物。候補のhash列が一致するだけで内容の真正性を証明したとは扱わない。
- 既存 `category_rules.match_transaction` の承認済みproductルールと候補カテゴリ、既存カテゴリマスタを再利用。店舗全体の分類を配らない。改名時に旧商品カテゴリを流用せず再評価。ルール競合、承認時期不適合、候補／明示分類とルールの競合は保留。未知の商品だけはマスタに存在する「その他／未分類」を商品単位で利用する。
- 外税・値引きは商品への帰属とカテゴリを照合。単一カテゴリでも税だけ別カテゴリなら保留。内税を独立加算しない。混合カテゴリの未配賦調整は保留する。
- `receipt_item_correction_review.py` は**純粋な表示・登録検証契約**。支払総額、元の明細・合算式、差額・不足／超過、修正後合算式、原本リンク、原因未特定／検証理由、状態を「確認」案件用に返す。原本ID/hash/version・用途・本人確認済み総額・入力snapshot・分類マスタ／ルール版を結合する。同一原本版の総額確認を再要求しない。
- `registration_plan` は最新の独立読取から再構築した表示snapshotと要求snapshotを比較するだけであり、認証や記帳権限を付与しない。行番号を恒久IDにしない。表示・セル編集で金融writeは0。
- テスト内 `MemoryRegistration` はメモリDBに限定。既存 `ReceiptConfirmation._plan`、`_write_accounting_plan`、`_complete`、`validate_receipt_result` を利用し、一回の合成要求→商品別明細→独立get読戻しを実行する。新しい本番writerは作っていない。テスト内の本人／session／CSRF／期限／NO-GOは**stub**であり、Google認証・本番authority検証成功の証拠ではない。
- 旧「要確認」の総額1行記帳遮断は維持。Amazonとの併存テストの古い期待値を修正し、PayPay等の別決済は従来どおり処理されることを確認。決済の処理コードは変更しない。

## A〜Iの合成試験

| 条件 | 検証 |
|---|---|
| A/B | 1,368−1,230＝+138円、967−939＝+28円、不足／超過、総額確認保持 |
| C/D | 必要な1商品だけ修正、改名、数量＋行金額確認、追加、重複除外、修正前候補保持 |
| E | productルール再利用、AI候補との競合、ルール同士の競合、店舗分類の不使用 |
| F | 外税、内税二重加算防止、負の値引き、帰属／カテゴリ競合、混合カテゴリの保留 |
| G | 原本ID/hash/version・候補改変、案件混同、入力snapshot変更、重複、再送、認証stub拒否、台帳変化 |
| H | NO-GOで検証済み・未記帳、設定変更後も旧要求を再送／自動処理しない |
| I | 受付→検証→完了／修正必要／未記帳／結果不明の表示データ、部分write・応答喪失後の再送禁止 |

正常系は商品行の和＝支払総額。検証異常系は金融writer呼出し0。合成の書込み障害注入では、障害前の一部商品行が残り得るため「結果不明・照合待ち」で停止し、同一要求・別要求とも追加writeを行わない。部分書込みを0件と偽らない。

関連最終ローカル実行：Python **251成功・0失敗**（商品修正／review／receipt confirmation／unit posting／旧review／候補snapshot／evidence／PayPay／カテゴリ／Amazon併存／Gemini診断）。JavaScript既存チェック **13成功・0失敗**。compileall・diffチェックも実施。全体試験とCIの最終結果はPR本文に記録する。

開始時の対象既存テスト45件は成功。既存CI run `42241059803` は `httpx` の直接テスト依存不足でcollection失敗していたため、`requirements-test.txt` に追加（本番依存変更なし）。ローカル全体試験の初回はsandboxのTEMP権限、作業ディレクトリ内の再試行は既存「stateはrepository外」契約で失敗した。安全条件を緩めず、許可済みのrepository外一時領域で再実行する。

## iPhoneの操作と残作業

採用済み仕様は維持する。「確認」1タブ、1行＝1案件、案件ID・見出し固定、横スクロール、原本往復、入力・途中メモの保持。G103／H301は変更しない。

接続後の操作は、①案件検索・原本確認 → ②必要な商品だけ修正／追加／除外 → ③「修正内容を登録する」を1回 → ④同じ案件で結果確認。総額確認済みの同一原本版では総額を再入力しない。原因を特定できなければ保留する。**現時点の本番Sheetsにはこの登録機能を設置していないので、この手順による実登録はできない。iPhone実機検証も未実施。**

最小の未接続部分：

1. 採用済み1タブへの商品編集コントロール・追加／除外・登録操作・結果反映の実接続。今回の表示契約はSheetsのrender/updateを実行しない。旧複数タブやPDF用UIを流用して確定UIを変えない。
2. Google本人認証・owner認可・session/CSRF・期限と要求UUIDを、独立取得した原本bytes/hash/version・保存済み候補・入力snapshotへ結合するイベント入口。セル編集／状態選択から自動記帳するpollingは作らない。
3. 既存private journal/CAS・concurrency lock・authority・validated SHA・本番停止条件・台帳重複照合を接続する。原本版に結び付いた支払総額確認をtrusted storeから読む。失敗／UNKNOWNは照合専用にする。
4. 本番は既存writerを維持し、独立台帳読戻し後だけ完了表示する。金融writeの可否をUIの値・digest・test stubで代用しない。通常の一回登録で安全ゲートが成立する認可契約を確立する（取引ごとの手動canary承認を通常運用に転用しない）。

mainにあるone-unit posting runnerは限定された明示canary authority契約であり、一般の不一致案件用の即時登録権限ではない。認証付きone-tab入口も未接続のため、本番までつなぐにはこの認可／接続契約の確定が必要。新規インフラや権限緩和による代替実装は行わず、ユーザー指定の停止条件に従って検証済み部分をDraftに保存する。

本番反映には上記接続の合成／認証テスト、iPhone実機確認、独立した本番導入承認が必要。このPRの合成成功だけでNO-GO・validated SHA・authority・本番フラグを変更しない。

再現例（networkはtests/conftest.pyで拒否）：

```sh
python -m pytest -q tests/test_receipt_item_correction_plan.py tests/test_receipt_item_correction_review.py
```

全体試験では既存state安全契約に従い、repository外の許可された一時領域を `--basetemp` とTEMP/TMPに指定する。
