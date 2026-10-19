# B1/B2 隔離runtime実装（writer非接続）

対象基点は#143 head `9ab8ca506528a5de55d950adf940fc05f9ea19c9`。
mainのAGENTS・継続仕様、#123～#143の依存鎖、#142停止18件と#143設計70件を引き継ぐ。
本書は#143の「設計のみ」という当時の完了記録を上書きせず、その後に別承認された隔離実装を記録する。
新Draftのhead、専用Ubuntu CIのrunと確定件数はPR本文に記録する。

## 実装範囲と変更ファイル

| ファイル・関数 | 責務 |
|---|---|
| `app/receipt_confirmation_draft_request.py:validate_request` | schemaなしは旧validatorへ、正確なinputs v2だけ新validatorへ。失敗後fallbackなし |
| 同 `DraftReaders.observe`, `prepare_inputs`, `validate_draft_request_v2` | 独立保存draft/source bytes/master/full ledgerの前後二重read、再計算とexact比較 |
| 同 `_derive`, `PlanningDB` | 画像whole-source固定catalog、typed ReceiptResult、既存manual parser/receipt validator/_plan。DBはread-only |
| 同 `pending_projection` | immutable baselineと別のexecution projection。旧legacyは変更しない |
| `staging_shadow/receipt_draft_contract.py:IsolatedDraftStore` | 既存ConditionalRequestsのCASを使うmemory-only root v3、履歴継承とstrict validation |
| 同 `IsolatedDraftContract` | fixture資格照合、共有claim、pending+consume同一CAS、start、read-only照会。dispatch/completion常時拒否 |
| `tests/draft_runtime_fixtures.py`, `tests/test_receipt_draft_runtime.py` | 合成入力・競合・異常・別process再構築。新経路のwriter/clientはfail spy |
| `.github/workflows/iphone-draft-runtime-checks.yml` | 独立Ubuntu 24.04/Python 3.12。新テスト、R0/設計、既存安全回帰 |
| 既存R0/設計/CI分類テスト3ファイル | 今回許可された新2モジュールだけ追加を認める。旧runtime/auth/pinの変更は禁止のまま |

既存writer、guarded writer、旧validator、金融authority schema、共有CAS実装、認証host、privacy、
production runner/workflow、hash固定bundleは変更0。UIも変更0。

## B1：一般の修正plan

単一画像のsource ID/hash/Drive revision/numeric version/native versionを独立readerから取得し、
bytesのSHA-256と保存baselineを照合する。PDF/Unit/indexへの変換はなく、Unit/indexはnull。
baseline candidateの位置・全item digestと既存writer ID `R-{source_id}-{index:02d}`を固定する。
既存明細ID・順序・集合・quantity/confidence/parsed noteを維持し、日付/店/総額/支払/メモと
明細名/金額/master内カテゴリだけを変更する。追加/削除/並替/分割/架空調整は拒否する。

合成fixtureの比較（同じ保存baselineから独立再計算）：

| 項目 | original plan | proposed plan |
|---|---|---|
| receipt ID | `R-synthetic-legacy-stop` | 同じ |
| 日付・店 | 2026-01-02 / Synthetic shop | 2026-01-03 / Synthetic edited shop |
| 明細01 | Synthetic A / 70 / 食費・食品 | Synthetic corrected 1 / 80 / 日用品・消耗品 |
| 明細02 | Synthetic B / 50 / 食費・食品 | Synthetic corrected 2 / 40 / 食費・食品 |
| 合計 | 120 | 120 |
| 支払 | 現金 | Synthetic card |
| 既存明細01 label | preserve-label | preserve-label |

全plan行、全ledger beforeの値と数式、無関係行、旧timestampを比較する。
新規receipt col7/import col1だけserver prepared_atへ固定し、それ以外の差を無視しない。
既存materializerは新runtimeで使用しない。PlanningDB上で既存`ReceiptConfirmation._plan`を呼ぶだけで、
`_materialize_result`、writer dispatch、append/updateは0。

## B2：医療とbaseline/proposedの分離

inputs schemaは`receipt-confirmation-draft-inputs-v2`。
7領域baseline/draft/source_binding/proposed/categories/derivation/schemaを厳密に検査する。
baselineにはlegacy全体digest、保存item、candidate digest、初回native raw/tag観測を保持する。
draftにはDRAFT_ONLY、owner/case、generation/revision、保存値digest、実raw/strong tag観測を保持する。
proposedは保存draftのrawから再構成し、全confirmed/parsed/catalog/original plan/proposed planを結合する。
未知field/schema、不正型、bool整数、NaN/Infinity、duplicate JSON key、数式入力、容量超過を拒否する。
public digestを再計算しても、独立readerからの再導出と一致しなければ通らない。

医療は日付・施設・実支払額・医療カテゴリの完全手入力だけを既存medical `_parsed`へ渡す。
OCR/AI provenanceや候補明細を拒否し、旧parsedを補完に使わない。
合成fixtureの旧planは2026-01-02 / Synthetic clinic / 120、
新planは2026-01-03 / Synthetic manual clinic / 135。
同じ`R-synthetic-legacy-stop-01`で本人確認医療支出を計画する。
旧入力が未完ならoriginal parsed/planはnullであり、旧値を入力補完しない。

旧`validate_request`の`old.inputs == confirmed`は維持。
旧validatorは新schemaを拒否し、新validatorも旧validatorの例外を握りつぶさない。
金融authorityのfield集合/ACCOUNTING_POST/VerifiedPost/issuer/owner/session/CSRF/TTL600秒/
expected_revision=0は従来どおり。資格は合成fixtureデータであり、発行APIは追加しない。
draftや#131 acknowledge_snapshotは金融authorityにならない。

## 保存root v3と継承

rootは`schema/revision/legacy/operations`の4領域を維持する。
v1はread-only、v2は旧validatorでread-only、新requestはv3だけ、未知versionはUNKNOWN。
新しいDB/CAS engineはなく、全CASは既存`ConditionalRequests.replace/transition`とversioned IOを使う。

同じmemory backendのv2からの明示継承だけを許可する。serverが事前に固定したparent digestが必須。
operations内の予約済み`__synthetic_v2_lineage__`は金融operationではなく、親revision/digest/raw tagの継承証拠。
既存operationsは値・全証拠をそのまま保持し、legacyも不変にする。
v3を読むたび、現在のlegacyと旧operationsから親v2全体を再構成して、**root自身から採用しないtrusted pin**と照合する。
履歴を落としてmarkerを再hashしてもpinと一致せず、空v3による迂回を拒否する。
これは設計の4領域を維持する最小の継承証拠であり、実native文書のmigrationではない。
pinなしで新規v3を受け入れず、v1から自動昇格せず、旧operationsを更新/修復しない。

qualification→共有accepted/executing CAS照合→pending+consume同一native CAS→startを検証する。
共有claimのschemaとownership relationは既存のまま。全証拠を新request fingerprintへ結合する。
各段階でsource/draft/master/before/authを再検証し、自己CAS後のnative/shared raw/tagも照合する。
412、timeout、lost response、read-back不一致、stale revisionはsticky UNKNOWNとして停止する。
不明結果を記録する追加write、retry、補完、pending/fence解除はない。
再構築はdefault read-onlyでticketを復元しない。過去pending/applied/UNKNOWNを新要求へ転用しない。
start済でもcompletionは発行しない。観測beforeはUNKNOWN、一部変更はPARTIAL、全行一致だけでもUNKNOWN。
現在のdraft/sourceが変わって結果bindingを再確認できなければUNKNOWNとする。

## テストと判定

新規runtimeテストと既存回帰は別集計し、確定件数とCIはPR本文へ記録する。
新規は正常一般/医療、旧request拒否維持、schema/型/容量、再hash改ざん、原本・draft・master・全beforeのrace、
owner/session/CSRF/purpose/TTL、再送/別内容、旧状態/fence/保留、PDF混同、2/16 facade競合、
native/shared CAS異常、別process read-only再構築を検査する。
#142の18件と#143の70件は件数と拒否根拠を維持する。
当時の「新モジュールが存在しない」guardだけを、今回の新2ファイル追加以外は不可というguardへ更新する。

#139/#140を含む既存安全回帰では従来のmemory writer fixtureを実行する。
それは今回の新runtimeのdispatch/記帳成功へ加算しない。新経路のwriter dispatch/append/実authority発行・消費は0。
本番Spreadsheet/Drive/Firestore/OAuth/医療情報、Cloud/deploy、AI送信、原本移動へのアクセスは0。
一般/医療とも今回の合成記帳は0件。PC/390px/実機UI試験は今回対象外。

## R1再開に必要な最小差分（まだ実装しない）

1. `staging_shadow/synthetic_legacy_writer.py`の`SyntheticLegacyConfirmation.__init__/submit/_current/_native_completed/inspect`：
   新contractのexact type/schemaを別分岐として接続し、`pending_projection`を既存writerへ渡す。
   現在の旧v2 guardを緩めず、新v3 completion certificateと独立全ledger read-backを追加検証する。
2. `staging_shadow/receipt_draft_contract.py:IsolatedDraftContract.dispatch/complete_fixture`：
   次の別承認でのみguarded writer接続と完了証拠を追加する。今回両方常時拒否。
3. `app/iphone_item_drafts.py`等の1タブdraft reader、および
   `app/iphone_confirmation.py:ConfirmationService.read/proof/post`と
   `app/iphone_confirmation_host.py`のsnapshot/本人確定adapter：
   PDF Unitとは別のimage whole-source catalogを注入し、保存draftから新inputsへ結合する。
   正式UIを変える場合は先に承認。金融POST/authority発行は今回追加しない。
4. 明示確定から合成writer・結果までの統合正常/停止/16並列、PC/390px検証。
   iPhone Safari/Sheets実機受入は別に行う。

両障害の隔離runtimeが関連CIを通過した場合のみ、次の「R1 writer合成接続の検討へ進める」と判定する。
writer接続そのものの許可やR1全体完了とはしない。
R2は全旧runner mandatory gate/drain、R3は実認証host/provider・実versioned backend・監査/保持/復旧。
本番までにはその別承認、統合安全試験、iPhone本人受入、新規一般canary、段階有効化とmain統合の承認が残る。
