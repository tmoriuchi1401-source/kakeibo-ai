# ページごとの確認と read-only canary

原本は1コピー、長期履歴は軽量metadata、認証・診断は有限TTLとする。
PR #91はDraftのまま。会計writer、Medical writer、原本移動、定期入口はこの経路から呼ばない。

## Gateの範囲

- reconciliation、HGA、解析、completionの失敗は対象Page/Receipt Unitだけを保留する。
- 原本PDFのSHA256、page count、共通authority構造の破損はsource全体を保留する。
- 親PDFは全Page配下の全Receipt Unitがterminalになるまで移動不可。
- 枚数や帰属が不安定な場合、まだidentityを証明できないUnitを自動採用しない。

## 本人確認とページrouting

既存Google OIDC endpointと確認画面を再利用する。onEdit、Apps Script、triggerは変更しない。
管理者がprivate configからp4/p10/p14別のprofileを固定する。各profileは別のprivate Drive authority fileを持つ。
公開endpointにページ選択やrequest作成APIはない。管理者だけがrequest UUIDを作成し、Firestoreの保護されたrouting recordへprofile digestを保存する。
request/routing/sessionは600秒TTL。ブラウザは署名付きリンクを開くだけで、source/pageを差し替えられない。
automatic privacyは不変。旧「一般」確認はAI許可に昇格しない。

## p1 reconciliation

既存「PDFページ確認」のMedical入力は一切書き換えない。直下に今回の原本、既存候補の4項目・原本リンクと判定dropdownを表示する。
初期値は「未選択」。選択だけでは保存せず、Google署名/issuer/sub allowlistと明示POSTが必要。

requestはp1 source/hash/page/review/revision、本人入力digest、既存ledgerと関連receipt/importのexact read-back、判定、current revisionへ固定する。
Service AccountによるSheetsアクセスはread-only。元のMedical入力、候補ledger、page-kindをGETし、原本SHA256を確認する。
確定直前にもfresh readする。強いcurrent digestをETagとして照合し、permanent repositoryのtransactionでcurrent revisionをCASする。staleは412、unconditional fallbackなし。

| 判定 | permanent event | current | 会計write |
|---|---|---|---|
| 同一 | reconciled_existing | terminal | 0 |
| 別 | confirmed_distinct | pair限定・記帳前で保留 | 0 |
| 判断できない | なし | reconciliation_required | 0 |

history/current/request receipt/pair pointerは既存の原子的metadata repositoryへ保存する。historyはyear partition、permanentにTTLを付けない。
replayではevent/current/会計を変更しない。画面の更新失敗後もまずpermanent receiptをread-backし、再appendしない。
terminal表示は行非表示によるprojectionであり、入力・ledger・履歴を削除しない。

## 一般ページ

有効なページ専用HGAだけを使い、Actions内でfresh RGB PNG1ページを送る。元PDF、Medical、別ページの送信は禁止。
Receipt Unit manifestと独立rereadを検証し、信頼できるfieldだけ共通手入力カードへpre-fillする。
既存の本人入力を消さない。新しいcandidateのdigestへbindingし、human/human_overrideとして最終validationする。
必須は日付・positive total・既存category。optional空欄を理由に止めない。item整合性・fake adjustment・transaction_kind・duplicate/replayは維持する。
final candidateはready_to_write/needs_reviewまで。会計writeは別canary。

## live配置の検証

オフラインtests/CI後にprofileを配置する。各ページごとに新requestを1件発行し、本人の成功表示とbackend exact read-backが揃った場合だけ成功。
p1のsameはpermanent event1件、p4/p10のHGAは各専用stateのgrant/audit1件。p14の既存grantは再発行しない。
リンクは使用後/失効後に無効化する。未知のwrite outcomeはread-backして判定し、自動再試行しない。

今回の会計・Medical・Gemini・移動の実施状況はprivate evidenceへ記録する。コードのテスト成功をlive canary成功と混同しない。
