# PDF確認：支払カード＋商品別カテゴリ

既存「PDFページ確認」のReceipt Unitごとに、上部の原本・支払情報と下部の商品一覧を統合する。新Sheet、Web UI、trigger、scheduleは作らない。Medical、HGA、元PDF移動の安全境界は変更しない。

## 表示と入力

上部はページ/Receipt Unit・状態・原本リンク・支払日・合計・店舗名・支払方法・メモ・確認理由。下部は「商品名／金額／カテゴリ」。商品、印字済み全体値引き、外税を既存解析itemsと同じ単位で表示する。解析候補の表示は原本との一致や記帳許可を意味しない。

旧UIのC:Pは他ページのhidden metadataなので、そのまま保持する。A/B/Qを画面上で連続する3列として使用し、新カードのmetadataはR:Wへ置く。上部の値はB:Qをmergeする。幅は107+74+133=314px、行見出し46pxを含め360pxを目安とする。行番号はsource/page/Receipt Unit/itemのmarkerから検出し、固定セル番地は使用しない。

日付はDATE_IS_VALID、strict、yyyy/mm/dd。日付・合計・各商品カテゴリだけ空欄時に条件付き書式で薄黄を表示し、入力後は自動解除する。任意欄の空欄は通常色。明細構造の追加確認は独立したオレンジ表示を保持する。

商品名・符号付き金額も既存セルで訂正できる。元の解析candidate、item ID、kind、件数、順序は固定し、訂正値は別snapshotとして保持する。差のある商品だけに初回/再読取比較行を付ける。安定した商品のカテゴリや本人入力値を一律に戻さない。名前・金額の訂正や「確認済み」選択だけではreread Gateは解除されない。

## カテゴリ

独立読取で同じ元itemへ対応付けられ、数量・金額・カテゴリが一致し、confidenceが0.8以上で既存マスタに存在する商品カテゴリのみpre-fillする。NFKCで等価な幅の表記差はitem対応には利用できるが、明細確認済みの証明にはしない。カテゴリ競合はその商品だけ空欄にする。分離/transaction等の追加Gateがある場合は事前入力しない。旧candidateに商品別corroborationがない場合、レシート全体のカテゴリを商品へ展開しない。

本人は既存マスタの「大｜小」1セルdropdownから商品ごとに選択する。Receipt Unit全体のカテゴリを全商品へ暗黙に適用しない。旧画面で本人が入力済みのレシート共通カテゴリがあれば参考値として保持し、商品へ自動転記しない。明示的な一括適用helperは空欄だけを埋め、既に入っている値を上書きしない。日常UIへの追加一括操作は今回は不要。

各商品のcategory provenanceはgemini/human/human_override/missing。完成candidateでは既存「支出明細」のnoteに短いcategory_source enumを残せるため、長期履歴へGemini全文や入力snapshotを複製しない。

`approved_product_prefill`は既存の承認済み商品ルール/完全一致matcherを再利用する。信頼できる取込時刻・店舗・商品名・既存カテゴリを照合し、service/store_totalルールを各商品へ展開しない。Geminiとルールが競合すれば空欄、明細構造Gateがあれば適用しない。これは本人セル編集前だけに使用する。ルール由来は本人承認済みルールとしてhuman provenanceに加え、rule ID/revision/methodを軽量metadataで区別する。p14は構造Gateがあるため適用0。

## 税・値引き

既存prompt/writerは商品に紐づく値引きをその商品のnet amountへ反映し、印字済み全体値引きは負の独立明細、外税は正の独立明細として扱う。内税、小計、税対象額を追加加算しない。writerは各itemをその符号のまま1行にし、合計とtotalの完全一致を維持する。

今回、比例配賦・架空調整額・新カテゴリを追加しない。複数カテゴリにまたがる全体値引き/税の帰属が確定できない場合はtax_discount_allocation_review。信頼できる原本検証adapterが既存itemへの対応を固定できた場合だけ、その対象商品と同じカテゴリで検証できる。セルのカテゴリ選択だけでは対応証明にならない。

## 受付と実行

操作dropdownはReceipt Unitごとに「未選択／記帳する／保留する」。初期値は未選択。保留では値を保持し、会計writeをしない。

「記帳する」は要求入力だけ。既存Google OIDCの画面で原本/明細/税/値引き/カテゴリを明示確認し、完全なsnapshotへ署名検証済みactorをbindingする。既存onEdit/trigger/schedule/通常workflowを変更しない。現在の限定運用では、operatorがfresh snapshotから10分限定の本人確認リンクを既存カードへ提示する。セル選択だけで認証やwriterは起動しない。

`receipt_item_auth_transport`は既存OIDC/state/nonce/PKCE/CSRF/完全一致Origin検証を再利用する。request UUID、candidate digest、source/page/review/revision、Receipt Unit、元item ID、値引き対応、入力snapshotを固定する。認証済みの明示POSTでのみprivate Drive journalへconditional保存・exact read-backする。過去HGAの本人情報を新snapshotの確定に流用しない。

確定証跡は既存private Driveに置き、GitHub ActionsへはUUIDだけを渡す。既存`pdf-page-manual-canary`のreview入口がprotected contextとrequestをfresh readし、既存production concurrency内で`receipt_item_queue.capture`を呼ぶ。Cloud Runはhidden受付へ並行追記しない。受付は固定6列のexact read-backを行い、同UUID replayは追加0、通信切断後の不明状態を自動再追記しない。

`ReviewRequests`はsource/page/review/HGA/manifestのfreshnessを既存GeneralCompletion.freshへ委譲し、snapshot/item identity、全商品カテゴリ、sum/transaction/構造Gate、duplicateを再検証する。actorは既存Google OIDCが検証したVerifiedActorをtrusted backend adapterから受け取り、owner allowlist・有効期限・request UUID・snapshot digest・Receipt Unit identityへのbindingを検証する。HGAのactorやセルemailを、そのまま会計確定authorityへ流用しない。

`ConfirmedItems`は検証済み本人・request UUID・時刻・snapshot/candidate/input/identity digestに固定する。解除可能な差はreread/replayのitem structureだけ。segmentation、fake adjustment、Medical、PII、transaction、sum不整合等を解除しない。カテゴリだけの補完やセル内actor文字列では成立しない。

今回接続するrunnerはplan専用。既存ReceiptPipelineの同じrow materializerをPlanningDBへ渡し、レシート1行・各itemの支出明細・取込行を生成する。会計tableへのwrite capabilityは持たず、実会計writeには別の明示承認と限定canaryが必要。duplicate候補は保留し、同額だけで確定しない。

受付はrequest UUIDとsnapshot/candidate/plan digestをconditional durable保存する。実行は独立したbackend flagがdefault false。許可されたexecutorは既存writer callbackを再利用し、新しい会計row builderを持たない。runningを保存後にwriterを1回だけ呼び、通信切断後はread-backでcomplete/not_written/unknownを区別する。unknown/partialは自動再追記しない。runningの重複workerもwriterを再送しない。

exact read-back後だけcompleteとし、既存compact permanent event schema/年partitionを再利用する。完了時刻を固定してからhistory adapterを呼ぶ。同request replayでは履歴/authority/会計追加0。terminalカードはrowをhideし、入力、ledger、identity、historyを削除しない。

candidate/入力snapshot/詳細provenanceは既存medium policy（90日）の対象。認証sessionとroutingは600秒。`item_structure_confirmed`永久eventは既存年partitionへappendし、actor hash・時刻・authority/request digest・source/unit identityのみ保存する。会計未実行なのでterminal eventや確認カード非表示にはしない。cleanupの有効化は行わない。

## p14限定の終了点

p14の保存済み2結果は10明細/3,801円で一致し、保存された差は商品名の幅表記4件とカテゴリ2件。元の内部再読取の全文は保持されていないため、過去Gateを自動解除しない。原本には単品割引後の9商品と全体値引き13円があり、印字対象162円のCHARMY Magicへの対応を提案として固定する。内税335円を追加加算しない。7商品を部分事前入力し、バウンシア/NONIO/全品割引は本人選択する。本人の認証付き明示確認と全Safety Gateが成立して初めてplan検証可能になる。

p1 reconciled_existing、p4/p10 HGA0、既存10件29,034円は不変。parent archive_allowed=false、会計/Medical writer、PDF move、main mergeは0。既存iPhone表示/編集/保存の合格を引き継ぐ。新Sheet/独立画面/新triggerは作らない。
