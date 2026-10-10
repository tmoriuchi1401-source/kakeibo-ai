# 一般レシート共通policyと限定write canary

`receipt-required-date-total-category-v1` はread-onlyとReceiptPipelineで共通。
必須はvalidなYYYY-MM-DD、正の原本total、既存カテゴリ組合せ。
店舗名・支払方法は空欄を許容する。原本文字による裏付けのない支払方法は
空欄にし、履歴・ファイル名等で補わない。明示的な支払方法の矛盾は要確認。
既存の明細必須・合計完全一致・架空調整禁止・取引種別・日付/合計/種別の
再読取変動検証は維持。privacy、duplicate、replayのauthorityとは独立する。

既存manual-only read-only workflowはGoogle API GET fenceを維持し、writeしない。
診断v2は検証成功の一般候補の構造化明細を既存暗号化envelope内に限って保存。
画像、OCR全文、raw response、Secret、自由記述noteは保存しない。retentionは1日。
同じreview済みSHAのp2 proofがなければ残りの解析へ進まない。

`pdf_receipt_write_canary` は明示的な別operator intentで既存ReceiptPipelineを再利用。
新しいledger writer、workflow schedule、Medical経路、archive経路は追加しない。
固定対象のうちp11を最初に処理し、そのexact read-back成功後にだけ段階拡大する。
p1/p4/p10/p14はallowlist外。grouping/page-kind stateのaccounting scopeは変更しない。

live callerはDrive正本、source SHA、stable page identity、confirmed Unit、human normal、
automatic normalを再確認し、対象ページだけfresh RGB PNG化してexact gateを通す。
保存済みparsed candidateも同じ原本・authority・review済みActions runに結び付ける。
同一原本だけを理由に新しいPNG hashを旧観測hashへ一致させることはしない。

専用private intentは既存v2 conditional transportでIf-Match保存・exact read-back。
既存private authority/stateを上書きしない。新たな認証scope/ACLは追加しない。
ReceiptPipelineのdry materializationで同じtimestampの予定行を固定し、pendingを
durableに保存してから3会計シートへ既存RAW appendする。writer fenceは予定行以外、
既存行のupdate/delete、schema repair、UI変更を拒否する。各append前にsourceと
authority/categoriesとintentのfreshnessを再確認。各append直後にもexact read-back。
曖昧な応答はread-backだけで確認し、appendを自動再送しない。
pending/unknown intentは自動再実行せずreconciliation待ち。

applied replayはsource/authority/candidateと全予定行を照合し、会計・intentを変更しない。
既存同一IDの不一致、他取引の近接日付・同額、競合は停止し、自動上書きしない。
親PDFは未解決Unitが残るためInboxに維持する。
