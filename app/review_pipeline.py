from __future__ import annotations

from dataclasses import dataclass

from .auto_expense import expense_id
from .amazon_manual_matching import (
    AmazonManualCandidate,
    ManualMatchRequest,
    aggregate_amazon_orders,
    audit_information,
    candidate_from_storage_row,
    find_amazon_candidates,
    is_manual_match_target,
    validate_manual_batch,
)
from .aupay_card_pipeline import is_amazon
from .reconciliation import ImportTransaction, parse_import_rows
from .sheets import CATEGORY_SEPARATOR, HEADERS, SheetsDB
from .utils import now_jst_string


@dataclass(frozen=True)
class ReviewItem:
    transaction: ImportTransaction
    priority: str
    recommendation: str


def is_reviewable_status(status: str) -> bool:
    return (
        status == "要確認"
        or status.startswith("needs_review")
        or status.endswith("_needs_review")
        or status == "amazon_unmatched"
    )


def selected_category_pair(major: str, minor: str) -> tuple[str, str]:
    major = major.strip()
    minor = minor.strip()
    if CATEGORY_SEPARATOR in major:
        combined_major, combined_minor = major.split(CATEGORY_SEPARATOR, 1)
        return combined_major.strip(), combined_minor.strip()
    return major, minor


def review_items(transactions: list[ImportTransaction]) -> list[ReviewItem]:
    items=[]
    for tx in transactions:
        status=tx.status
        if status == "要確認" or status.startswith("needs_review") or status.endswith("_needs_review"):
            priority="高"
            if status == "amazon_needs_review":
                recommendation="Amazon注文との重複候補を確認"
            elif status == "needs_review_amazon_installment":
                recommendation="Amazon注文・元利用との二重計上を確認"
            elif status == "needs_review_refund":
                recommendation="返金・取消と元取引の扱いを確認"
            elif status == "needs_review_transfer":
                recommendation="チャージ・送金・資金移動かを確認"
            elif tx.source == "receipt":
                recommendation="レシート画像・合計・カテゴリを確認"
            else:
                recommendation="重複候補を確認し、統合先を選択"
        elif status == "amazon_unmatched":
            priority="中"
            recommendation="Amazon注文履歴に一致なし。注文履歴の不足または請求内訳を確認"
        else:
            continue
        items.append(ReviewItem(tx,priority,recommendation))
    # High priority first, then newest date first, then stable import ID.
    def sort_key(item:ReviewItem):
        digits=item.transaction.date.replace("-","")
        date_number=int(digits) if digits.isdigit() else 0
        return (0 if item.priority=="高" else 1,-date_number,item.transaction.import_id)
    return sorted(items,key=sort_key)


class ReviewPipeline:
    def __init__(self,db:SheetsDB):
        self.db=db

    def preview(self)->dict:
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx)
        candidates=self._candidate_rows(tx)
        result=self._summary(items)
        result.update(self._candidate_summary(candidates))
        return result

    @staticmethod
    def _selection_label(candidate:AmazonManualCandidate)->str:
        date=candidate.order_date[5:].replace("-","/") if len(candidate.order_date)>=10 else candidate.order_date
        difference=f"{candidate.amount_difference:+,}円"
        summary=candidate.short_item_summary.replace("\n"," ")[:30]
        return (f"#{candidate.candidate_id[-8:]}｜{date}｜{candidate.order_amount:,}円｜"
                f"差{difference}｜{candidate.date_difference_days}日前｜"
                f"{candidate.item_count}商品｜{summary}")

    def _candidate_rows(self,transactions:list[ImportTransaction]):
        orders=aggregate_amazon_orders(self.db.get("Amazon注文!A2:M"))
        generated=[]
        for tx in transactions:
            result=find_amazon_candidates(tx,orders)
            generated.append((tx,result))
        return generated

    @staticmethod
    def _candidate_summary(generated)->dict:
        targets=[(tx,result) for tx,result in generated if is_manual_match_target(tx)]
        return {
            "amazon_manual_matching_rows":len(targets),
            "candidates_generated":sum(len(result.candidates) for _,result in targets),
            "rows_with_candidates":sum(bool(result.candidates) for _,result in targets),
            "rows_without_candidates":sum(not result.candidates for _,result in targets),
        }

    def refresh(self)->dict:
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx)
        generated=self._candidate_rows(tx)
        generated_by_card={item.import_id:result for item,result in generated}
        categories=self.db.categories()
        self.db.ensure_sheet("要確認",HEADERS["要確認"])
        self.db.ensure_sheet("Amazon照合候補",HEADERS["Amazon照合候補"])
        existing={r[0]:(list(r)+[""]*max(0,20-len(r)))[:20]
                  for r in self.db.get("要確認!A2:T") if r}
        old_candidates={}
        old_labels={}
        for raw in self.db.get("Amazon照合候補!A2:S"):
            row=list(raw)+[""]*max(0,19-len(raw)); row=row[:19]
            if row[0]:
                old_candidates[str(row[0])]={"fingerprint":str(row[16]),"label":str(row[17])}
                if row[17]: old_labels[str(row[17])]=str(row[0])
        self.db.clear("要確認!A2:T")
        self.db.clear("Amazon照合候補!A2:S")
        rows=[]
        candidate_rows=[]
        validation_options={}
        generated_at=now_jst_string()
        for item in items:
            tx=item.transaction
            old=existing.get(tx.import_id,[""]*20)
            manual=old[9:15]
            display_date=tx.date.replace("-","/") if tx.date else ""
            result=generated_by_card.get(tx.import_id)
            candidates=list(result.candidates) if result else []
            labels=[]; current_by_id={}
            for rank,candidate in enumerate(candidates,start=1):
                label=self._selection_label(candidate); labels.append(label)
                current_by_id[candidate.candidate_id]=candidate
                candidate_rows.append([
                    candidate.candidate_id,candidate.card_import_id,candidate.order_id,rank,
                    candidate.card_date,candidate.order_date,candidate.card_amount,candidate.order_amount,
                    candidate.amount_difference,candidate.amount_difference_rate,
                    candidate.date_difference_days,candidate.item_count,candidate.short_item_summary,
                    " / ".join(candidate.major_categories),candidate.payment_method,
                    candidate.source_kind,candidate.order_fingerprint,label,generated_at,
                ])
            summary="\n".join(f"{index}. {label}" for index,label in enumerate(labels,start=1))
            selected_label=str(old[17]); selected_id=str(old[18])
            mapped=old_labels.get(selected_label)
            if mapped and mapped!=selected_id:
                selected_id=mapped
            if not selected_id and mapped:
                selected_id=mapped
            selection_state=""
            if selected_id:
                current=current_by_id.get(selected_id)
                previous=old_candidates.get(selected_id)
                if current is None:
                    selection_state="選択無効: 候補なし（要再選択）"
                elif previous and previous["fingerprint"]!=current.order_fingerprint:
                    selection_state="選択無効: 注文内容変更（要再選択）"
                else:
                    selection_state="選択済み"
            rows.append([tx.import_id,item.priority,display_date,tx.source,tx.merchant,tx.amount,
                         tx.status,item.recommendation,tx.note]+manual+
                        [summary,result.total_candidate_count if result else 0,
                         selected_label,selected_id,selection_state])
            if labels: validation_options[len(rows)+1]=labels
        self.db.append("要確認",rows)
        self.db.append("Amazon照合候補",candidate_rows)
        self.db.configure_review_validation(categories,validation_options)
        result=self._summary(items)
        result.update(self._candidate_summary(generated))
        result["refreshed"]=True
        return result

    @staticmethod
    def _summary(items:list[ReviewItem])->dict:
        return {
            "review_rows":len(items),
            "high":sum(x.priority=="高" for x in items),
            "medium":sum(x.priority=="中" for x in items),
        }


class ReviewApprovalPipeline:
    ACTIONS={"支出として計上","重複として除外","レシートと統合","Amazon注文と照合","保留"}

    def __init__(self,db:SheetsDB): self.db=db

    _expense_id = staticmethod(expense_id)

    @staticmethod
    def _amazon_error(errors:tuple[str,...])->str:
        text=" ".join(errors)
        if "more than once" in text: return "Amazon注文候補が他の選択と競合しています"
        if "already used" in text: return "このAmazon注文は別取引で使用済みです"
        if "changed after" in text: return "注文内容が変更されています"
        if "no longer exists" in text: return "Amazon候補が無効です"
        if "does not belong" in text: return "Amazon候補が対象カードと一致しません"
        return "Amazon候補を安全に確認できません"

    def _amazon_plan(self,imports,review_rows):
        selected=[]; preliminary={}
        candidate_rows=self.db.get("Amazon照合候補!A2:S")
        candidates={}; duplicate_ids=set()
        for raw in candidate_rows:
            try: candidate=candidate_from_storage_row(raw)
            except ValueError: continue
            if candidate.candidate_id in candidates: duplicate_ids.add(candidate.candidate_id)
            else: candidates[candidate.candidate_id]=candidate
        by_id={tx.import_id:tx for tx in imports}
        for raw in review_rows:
            row=(list(raw)+[""]*20)[:20]
            if str(row[9]).strip()!="Amazon注文と照合": continue
            card_id=str(row[0]); candidate_id=str(row[18]).strip()
            tx=by_id.get(card_id); candidate=candidates.get(candidate_id)
            errors=[]
            if tx is None: errors.append("card transaction no longer exists")
            if str(row[19]).strip()!="選択済み": errors.append("candidate selection is not current")
            if not candidate_id or candidate is None: errors.append("candidate no longer exists")
            if candidate_id in duplicate_ids: errors.append("candidate storage contains duplicate identity")
            if errors: preliminary[card_id]=tuple(errors)
            else: selected.append(ManualMatchRequest(tx,candidate))
        validation=validate_manual_batch(
            selected,aggregate_amazon_orders(self.db.get("Amazon注文!A2:M")),imports,
        ) if selected else None
        errors=dict(preliminary)
        if validation: errors.update(validation.errors_by_card)
        requests={request.card.import_id:request for request in selected if request.card.import_id not in errors}
        return requests,errors

    def preview(self)->dict:
        imports=parse_import_rows(self.db.get("取込データ!A2:L"))
        review_rows=self.db.get("要確認!A2:T")
        selected=sum(str((list(row)+[""]*10)[9]).strip()=="Amazon注文と照合"
                     for row in review_rows)
        requests,errors=self._amazon_plan(imports,review_rows) if selected else ({},{})
        conflicts=sum(any("more than once" in error for error in values)
                      for values in errors.values())
        return {"amazon_manual_selected":selected,"amazon_manual_valid":len(requests),
                "amazon_manual_invalid":len(errors),"amazon_manual_conflicts":conflicts,
                "amazon_manual_would_match":len(requests)}

    def apply(self)->dict:
        imports=parse_import_rows(self.db.get("取込データ!A2:L"))
        by_id={tx.import_id:tx for tx in imports}
        categories=set(self.db.categories())
        expense_idx=self.db.expense_index()
        review_rows=self.db.get("要確認!A2:T")
        amazon_selected=any(str((list(row)+[""]*10)[9]).strip()=="Amazon注文と照合"
                            for row in review_rows)
        amazon_requests,amazon_errors=self._amazon_plan(imports,review_rows) if amazon_selected else ({},{})
        import_updates=[]; expense_new=[]; expense_updates=[]; review_updates=[]
        stats={"requested":0,"applied":0,"held":0,"errors":0,
               "expenses_created":0,"expenses_excluded":0,
               "amazon_manual_matched":0,"amazon_manual_invalid":0}
        for row_num,raw in enumerate(review_rows,start=2):
            row=list(raw)+[""]*max(0,20-len(raw)); row=row[:20]
            action=str(row[9]).strip()
            if not action: continue
            stats["requested"]+=1
            error=""
            tx=by_id.get(str(row[0]))
            if action not in self.ACTIONS: error="許可されていない判断です"
            elif action=="保留":
                row[14]="保留"; stats["held"]+=1; review_updates.append((row_num,row)); continue
            elif tx is None: error="元の取込データが見つかりません"
            elif action=="Amazon注文と照合":
                request=amazon_requests.get(tx.import_id)
                errors=amazon_errors.get(tx.import_id,())
                if request is None or errors:
                    row[14]="未反映: "+self._amazon_error(errors)
                    row[19]="未反映"
                    stats["errors"]+=1; stats["amazon_manual_invalid"]+=1
                    review_updates.append((row_num,row)); continue
                candidate=request.candidate; audit=audit_information(candidate)
                updated=list(tx.row); updated[8]="matched_amazon"
                updated[9]=f"amazon:{candidate.order_id}"
                rate=f"{audit['amount_difference_rate']:.4%}"
                annotation=(f"手動照合={candidate.candidate_id}; Amazonキー=amazon:{candidate.order_id}; "
                            f"カード側は支出計上しない; 手動照合監査="
                            f"カード額:{audit['card_amount']},注文額:{audit['amazon_order_amount']},"
                            f"差額:{audit['amount_difference']},差額率:{rate},"
                            f"日付差:{audit['date_difference_days']}日,商品数:{audit['item_count']},"
                            f"支払方法:{audit['payment_method']},データ種別:{audit['source_kind']}")
                updated[11]="; ".join(x for x in (tx.note,annotation) if x)
                import_updates.append((tx.row_num,updated))
                for expense_row_num,expense_raw in self.db.expense_rows_for_import(tx.import_id):
                    expense=list(expense_raw)+[""]*max(0,13-len(expense_raw)); expense=expense[:13]
                    expense[12]="duplicate_excluded"
                    expense_updates.append((expense_row_num,expense)); stats["expenses_excluded"]+=1
                row[14]="反映済み"; row[19]="反映済み"
                review_updates.append((row_num,row)); stats["applied"]+=1
                stats["amazon_manual_matched"]+=1; continue
            elif not (
                is_reviewable_status(tx.status)
                or tx.status in {"unclassified_aupay", "unclassified_card", "unclassified_paypay"}
            ):
                error=f"既に処理済みです: {tx.status}"
            if error:
                row[14]="エラー: "+error; stats["errors"]+=1; review_updates.append((row_num,row)); continue

            target=""; new_status=""
            if action=="支出として計上":
                if (tx.source=="au PAYカード" and is_amazon(tx.merchant)
                        and tx.status=="amazon_unmatched"):
                    row[14]="未反映: Amazon注文と照合 または 保留 を選択してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                pair=selected_category_pair(str(row[11]),str(row[12]))
                if pair not in categories:
                    row[14]="エラー: カテゴリマスタに存在する大・小カテゴリを選択してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                expense_id=self._expense_id(tx.import_id); target=expense_id; new_status="manual_expense"
                expense=[expense_id,tx.date,tx.merchant,"手動計上",tx.amount,pair[0],pair[1],
                         tx.row[7],tx.source,"",tx.import_id,str(row[13]).strip(),"active"]
                if expense_id in expense_idx: expense_updates.append((expense_idx[expense_id],expense))
                else: expense_new.append(expense)
                stats["expenses_created"]+=1
            elif action=="重複として除外":
                target=str(row[10]).strip(); new_status="manual_duplicate_excluded"
                for expense_row_num,expense_raw in self.db.expense_rows_for_import(tx.import_id):
                    expense=list(expense_raw)+[""]*max(0,13-len(expense_raw)); expense=expense[:13]
                    expense[12]="duplicate_excluded"
                    expense_updates.append((expense_row_num,expense)); stats["expenses_excluded"]+=1
            elif action=="レシートと統合":
                target=str(row[10]).strip(); receipt=by_id.get(target)
                if receipt is None or receipt.source!="receipt":
                    row[14]="エラー: 実在するレシートの取込IDを入力してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                new_status="matched_receipt"

            updated=list(tx.row); updated[8]=new_status; updated[9]=target
            manual_note=str(row[13]).strip()
            annotation=f"スマホ判断={action}"+(f"; {manual_note}" if manual_note else "")
            updated[11]="; ".join(x for x in (tx.note,annotation) if x)
            import_updates.append((tx.row_num,updated))
            row[14]="反映済み"; review_updates.append((row_num,row)); stats["applied"]+=1

        if expense_new or expense_updates: self.db.ensure_expense_status_column()
        self.db.append("支出明細",expense_new)
        self.db.update_rows("支出明細",expense_updates)
        self.db.update_rows("取込データ",import_updates)
        self.db.update_rows("要確認",review_updates)
        return stats
