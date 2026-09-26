from __future__ import annotations

from dataclasses import dataclass
from collections import Counter

from .auto_expense import expense_id
from .amazon_manual_matching import (
    AmazonManualCandidate,
    ManualMatchRequest,
    aggregate_amazon_orders,
    audit_information,
    candidate_from_storage_row,
    find_amazon_candidates,
    is_manual_match_target,
    major_category_summary,
    validate_manual_batch,
)
from .aupay_card_pipeline import is_amazon
from .reconciliation import ImportTransaction, parse_import_rows
from .review_evidence import (ReviewResource, receipt_original_link,
                              recorded_candidate_ids, recorded_detail_total_link)
from .sheets import CATEGORY_SEPARATOR, HEADERS, SheetsDB
from .utils import now_jst_string


REVIEW_WIDTH = 22
REVIEW_RANGE = "要確認!A2:V"
EVIDENCE_HEADERS = ["確認対象", "比較候補", "日付", "店舗", "金額", "原本"]


def review_evidence_cells(tx: ImportTransaction, *, spreadsheet_id: str,
                          sheet_ids: dict[str, int], review_row: int,
                          imports_by_id: dict[str, ImportTransaction],
                          expense_index: dict[str, int], has_amazon_candidates: bool,
                          candidate_sheet_row: int | None = None,
                          drive=None, unique_import_id: bool = True) -> tuple[str, str]:
    """Choose links from the decision reason, using only exact stored identities."""
    if tx.source == "receipt":
        target = receipt_original_link(tx, drive)
    elif spreadsheet_id and "取込データ" in sheet_ids:
        label = ("銀行抽出行を見る" if "銀行PDF" in tx.source else
                 "カード取込を見る" if "amazon" in tx.status.lower() else
                 "返金情報を見る" if tx.status == "needs_review_refund" else
                 "取込内容を見る")
        target = ReviewResource.sheet_row(
            label=label, role="target", spreadsheet_id=spreadsheet_id,
            sheet_id=sheet_ids["取込データ"], row=tx.row_num,
            last_column="L").formula()
    else:
        target = "資料リンクなし"

    comparison = recorded_detail_total_link(
        tx, spreadsheet_id=spreadsheet_id, sheet_id=sheet_ids.get("取込データ"),
        unique_import_id=unique_import_id)
    if not comparison and tx.target_id:
        if tx.target_id in expense_index and "支出明細" in sheet_ids:
            comparison = ReviewResource.sheet_row(
                label="既存明細を見る", role="comparison", spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_ids["支出明細"], row=expense_index[tx.target_id],
                last_column="M").formula()
        elif tx.target_id in imports_by_id and "取込データ" in sheet_ids:
            comparison = ReviewResource.sheet_row(
                label="関連取込を見る", role="comparison", spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_ids["取込データ"], row=imports_by_id[tx.target_id].row_num,
                last_column="L").formula()
        else:
            comparison = "比較先を特定できません"
    if tx.status == "needs_review_duplicate" and comparison in {"", "比較先を特定できません"}:
        recorded = recorded_candidate_ids(tx)
        resolved = [imports_by_id[candidate_id] for candidate_id in recorded
                    if candidate_id in imports_by_id and candidate_id != tx.import_id]
        missing = len(recorded) - len(resolved)
        if len(resolved) == 1 and spreadsheet_id and "取込データ" in sheet_ids:
            label = "候補明細を見る" + (f"（他{missing}件不明）" if missing else "")
            comparison = ReviewResource.sheet_row(
                label=label, role="comparison", spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_ids["取込データ"], row=resolved[0].row_num,
                last_column="L").formula()
        elif len(resolved) > 1 and candidate_sheet_row is not None and "確認材料" in sheet_ids:
            comparison = ReviewResource(
                label=f"比較候補{len(resolved)}件を見る", role="comparison",
                resource_type="sheet_range", spreadsheet_id=spreadsheet_id,
                sheet_id=sheet_ids["確認材料"],
                cell_range=f"A{candidate_sheet_row}:F{candidate_sheet_row+len(resolved)-1}").formula()
        elif not comparison:
            comparison = "候補を特定できません"
    if (comparison in {"", "比較先を特定できません"}
            and has_amazon_candidates and spreadsheet_id and "要確認" in sheet_ids):
        comparison = ReviewResource(
            label="注文候補を見る", role="comparison", resource_type="sheet_range",
            spreadsheet_id=spreadsheet_id, sheet_id=sheet_ids["要確認"],
            cell_range=f"R{review_row}:V{review_row}").formula()
    elif not comparison and tx.status == "amazon_unmatched":
        comparison = "注文候補なし"
    return target, comparison


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


def review_items(transactions: list[ImportTransaction], *, money_mode=False) -> list[ReviewItem]:
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
        if money_mode and (is_amazon(tx.merchant) or tx.source.lower().startswith("amazon")):
            recommendation="日常画面のAmazon金銭確認で確定情報を確認"
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
        from .amazon_money_runtime import money_enabled
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx,money_mode=money_enabled())
        candidates=self._candidate_rows(tx)
        result=self._summary(items)
        result.update(self._candidate_summary(candidates))
        return result

    @staticmethod
    def _selection_label(candidate:AmazonManualCandidate)->str:
        date=candidate.order_date[5:].replace("-","/") if len(candidate.order_date)>=10 else candidate.order_date
        difference=f"{candidate.amount_difference:+,}円"
        summary=candidate.short_item_summary.replace("\n"," ")[:48]
        if candidate.ship_date:
            ship_date=candidate.ship_date[5:].replace("-","/") if len(candidate.ship_date)>=10 else candidate.ship_date
            shipping_days=candidate.shipping_date_difference_days
            if shipping_days==0: shipping=f"{ship_date}発送・同日"
            elif shipping_days is not None and shipping_days>0: shipping=f"{ship_date}発送・{shipping_days}日前"
            elif shipping_days is not None: shipping=f"{ship_date}発送・{abs(shipping_days)}日後"
            else: shipping=f"{ship_date}発送"
            if candidate.shipment_count>1: shipping+=f"・{candidate.shipment_count}発送"
        else:
            shipping="発送日不明"
        category=major_category_summary(candidate.major_categories)
        return (f"#{candidate.candidate_id[-8:]}｜{date}｜{candidate.order_amount:,}円｜"
                f"差{difference}｜注文{candidate.date_difference_days}日前｜{shipping}｜"
                f"{category}｜{summary}")

    def _candidate_rows(self,transactions:list[ImportTransaction]):
        from .amazon_money_runtime import money_enabled
        if money_enabled():return []
        orders=aggregate_amazon_orders(self.db.get("Amazon注文!A2:O"))
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
            "amazon_candidates_with_ship_date":sum(
                bool(candidate.ship_date) for _,result in targets for candidate in result.candidates
            ),
            "amazon_candidates_without_ship_date":sum(
                not candidate.ship_date for _,result in targets for candidate in result.candidates
            ),
        }

    def refresh(self)->dict:
        from .amazon_money_runtime import money_enabled
        money_mode=money_enabled()
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx,money_mode=money_mode)
        generated=self._candidate_rows(tx)
        generated_by_card={item.import_id:result for item,result in generated}
        categories=self.db.categories()
        imports_by_id = {item.import_id: item for item in tx}
        import_id_counts = Counter(item.import_id for item in tx)
        duplicate_groups = {
            item.transaction.import_id: [imports_by_id[candidate_id]
                for candidate_id in recorded_candidate_ids(item.transaction)
                if candidate_id in imports_by_id and candidate_id != item.transaction.import_id]
            for item in items if item.transaction.status == "needs_review_duplicate"
        }
        self.db.ensure_sheet("要確認",HEADERS["要確認"])
        if not money_mode:self.db.ensure_sheet("Amazon照合候補",HEADERS["Amazon照合候補"])
        has_evidence_sheet = ("確認材料" in self.db.sheet_titles()
                              if isinstance(self.db, SheetsDB)
                              else "確認材料" in getattr(self.db, "sheets", {}))
        if has_evidence_sheet and isinstance(self.db, SheetsDB):
            header = self.db.get("確認材料!1:1")
            if not header or header[0][:len(EVIDENCE_HEADERS)] != EVIDENCE_HEADERS:
                raise ValueError("review_evidence_sheet_not_owned")
        if any(len(group) > 1 for group in duplicate_groups.values()):
            self.db.ensure_sheet("確認材料", EVIDENCE_HEADERS)
            has_evidence_sheet = True
        sheet_ids = ({s["properties"]["title"]: s["properties"]["sheetId"]
                      for s in self.db._sheet_metadata()["sheets"]}
                     if isinstance(self.db, SheetsDB) else getattr(self.db, "review_sheet_ids", {}))
        spreadsheet_id = getattr(self.db, "sid", "")
        expense_index = self.db.expense_index() if any(item.transaction.target_id for item in items) else {}
        existing={r[0]:(list(r)+[""]*max(0,REVIEW_WIDTH-len(r)))[:REVIEW_WIDTH]
                  for r in self.db.get(REVIEW_RANGE) if r}
        old_candidates={}
        old_labels={}
        for raw in ([] if money_mode else self.db.get("Amazon照合候補!A2:V")):
            row=list(raw)+[""]*max(0,19-len(raw)); row=row[:19]
            if row[0]:
                old_candidates[str(row[0])]={"fingerprint":str(row[16]),"label":str(row[17])}
                if row[17]: old_labels[str(row[17])]=str(row[0])
        self.db.clear(REVIEW_RANGE)
        if not money_mode:self.db.clear("Amazon照合候補!A2:V")
        if has_evidence_sheet:self.db.clear("確認材料!A2:F")
        rows=[]
        candidate_rows=[]
        evidence_rows=[]
        candidate_sheet_rows={}
        validation_options={}
        generated_at=now_jst_string()
        for item in items:
            tx=item.transaction
            duplicate_candidates = duplicate_groups.get(tx.import_id, [])
            if len(duplicate_candidates) > 1:
                candidate_sheet_rows[tx.import_id] = len(evidence_rows) + 2
                for candidate in duplicate_candidates:
                    candidate_link = ReviewResource.sheet_row(
                        label="候補明細を見る", role="comparison", spreadsheet_id=spreadsheet_id,
                        sheet_id=sheet_ids["取込データ"], row=candidate.row_num,
                        last_column="L").formula()
                    evidence_rows.append([f"{tx.merchant} {tx.amount}円", candidate_link, candidate.date,
                                          candidate.merchant, candidate.amount,
                                          receipt_original_link(candidate)
                                          if candidate.source == "receipt" else ""])
            old=existing.get(tx.import_id,[""]*REVIEW_WIDTH)
            manual=old[11:17]
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
                    candidate.ship_date or "",candidate.shipping_date_difference_days
                    if candidate.shipping_date_difference_days is not None else "",
                    candidate.shipment_count,
                ])
            summary="\n".join(f"{index}. {label}" for index,label in enumerate(labels,start=1))
            selected_label=str(old[19]); selected_id=str(old[20])
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
                    selected_label=self._selection_label(current)
                    selection_state="選択済み"
            # Legacy selections remain evidence for migration; no order lookup,
            # regenerated candidates, or silent invalidation in monetary mode.
            candidate_fields=old[17:22] if money_mode else [summary,result.total_candidate_count if result else 0,
                          selected_label,selected_id,selection_state]
            target, comparison = review_evidence_cells(
                tx, spreadsheet_id=spreadsheet_id, sheet_ids=sheet_ids,
                review_row=len(rows)+2, imports_by_id=imports_by_id,
                expense_index=expense_index, has_amazon_candidates=bool(candidates),
                candidate_sheet_row=candidate_sheet_rows.get(tx.import_id),
                unique_import_id=import_id_counts[tx.import_id] == 1)
            rows.append([tx.import_id,item.priority,display_date,tx.source,tx.merchant,tx.amount,
                          target,comparison,
                          tx.status,item.recommendation,tx.note]+manual+candidate_fields)
            if labels: validation_options[len(rows)+1]=labels
        self.db.append("要確認",rows)
        if not money_mode:self.db.append("Amazon照合候補",candidate_rows)
        if has_evidence_sheet:self.db.append("確認材料",evidence_rows)
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

    def _review_rows(self, *, migrate: bool) -> list[list]:
        if migrate and isinstance(self.db, SheetsDB):
            # In the production sequence approval precedes refresh. Complete
            # the column migration before interpreting operator selections.
            self.db.ensure_sheet("要確認", HEADERS["要確認"])
        if isinstance(self.db, SheetsDB):
            header = self.db.get("要確認!1:1")
            actual = header[0] if header else []
            expected = HEADERS["要確認"]
            legacy_21 = expected[:6] + ["原本"] + expected[8:]
            legacy_20 = expected[:6] + expected[8:]
            if actual[:len(expected)] == expected:
                return self.db.get(REVIEW_RANGE)
            if not migrate and actual[:len(legacy_21)] == legacy_21:
                return [list(row[:7]) + [""] + list(row[7:])
                        for row in self.db.get("要確認!A2:U")]
            if not migrate and actual[:len(legacy_20)] == legacy_20:
                return [list(row[:6]) + ["", ""] + list(row[6:])
                        for row in self.db.get("要確認!A2:T")]
            raise ValueError("review_schema_not_ready")
        return self.db.get(REVIEW_RANGE)

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
            row=(list(raw)+[""]*REVIEW_WIDTH)[:REVIEW_WIDTH]
            if str(row[11]).strip()!="Amazon注文と照合": continue
            card_id=str(row[0]); candidate_id=str(row[20]).strip()
            tx=by_id.get(card_id); candidate=candidates.get(candidate_id)
            errors=[]
            if tx is None: errors.append("card transaction no longer exists")
            if str(row[21]).strip()!="選択済み": errors.append("candidate selection is not current")
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
        from .amazon_money_runtime import money_enabled
        imports=parse_import_rows(self.db.get("取込データ!A2:L"))
        review_rows=self._review_rows(migrate=False)
        selected=sum(str((list(row)+[""]*12)[11]).strip()=="Amazon注文と照合"
                     for row in review_rows)
        requests,errors=self._amazon_plan(imports,review_rows) if selected and not money_enabled() else ({},{})
        conflicts=sum(any("more than once" in error for error in values)
                      for values in errors.values())
        return {"amazon_manual_selected":selected,"amazon_manual_valid":len(requests),
                "amazon_manual_invalid":len(errors),"amazon_manual_conflicts":conflicts,
                "amazon_manual_would_match":len(requests)}

    def apply(self)->dict:
        from .amazon_money_runtime import money_enabled
        money_mode=money_enabled()
        imports=parse_import_rows(self.db.get("取込データ!A2:L"))
        by_id={tx.import_id:tx for tx in imports}
        categories=set(self.db.categories())
        expense_idx=self.db.expense_index()
        review_rows=self._review_rows(migrate=True)
        amazon_selected=not money_mode and any(str((list(row)+[""]*12)[11]).strip()=="Amazon注文と照合"
                            for row in review_rows)
        amazon_requests,amazon_errors=self._amazon_plan(imports,review_rows) if amazon_selected else ({},{})
        import_updates=[]; expense_new=[]; expense_updates=[]; review_updates=[]
        stats={"requested":0,"applied":0,"held":0,"errors":0,
               "expenses_created":0,"expenses_excluded":0,
               "amazon_manual_matched":0,"amazon_manual_invalid":0}
        for row_num,raw in enumerate(review_rows,start=2):
            row=list(raw)+[""]*max(0,REVIEW_WIDTH-len(raw)); row=row[:REVIEW_WIDTH]
            action=str(row[11]).strip()
            if not action: continue
            stats["requested"]+=1
            error=""
            tx=by_id.get(str(row[0]))
            if money_mode and action!="保留" and (action=="Amazon注文と照合" or
                    (tx is not None and (is_amazon(tx.merchant) or tx.source.lower().startswith("amazon")))):
                row[16]="未反映: 日常画面のAmazon金銭確認で対応してください"
                stats["held"]+=1;review_updates.append((row_num,row));continue
            if action not in self.ACTIONS: error="許可されていない判断です"
            elif action=="保留":
                row[16]="保留"; stats["held"]+=1; review_updates.append((row_num,row)); continue
            elif tx is None: error="元の取込データが見つかりません"
            elif action=="Amazon注文と照合":
                request=amazon_requests.get(tx.import_id)
                errors=amazon_errors.get(tx.import_id,())
                if request is None or errors:
                    row[16]="未反映: "+self._amazon_error(errors)
                    row[21]="未反映"
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
                row[16]="反映済み"; row[21]="反映済み"
                review_updates.append((row_num,row)); stats["applied"]+=1
                stats["amazon_manual_matched"]+=1; continue
            elif not (
                is_reviewable_status(tx.status)
                or tx.status in {"unclassified_aupay", "unclassified_card", "unclassified_paypay"}
            ):
                error=f"既に処理済みです: {tx.status}"
            if error:
                row[16]="エラー: "+error; stats["errors"]+=1; review_updates.append((row_num,row)); continue

            target=""; new_status=""
            if action=="支出として計上":
                if (tx.source=="au PAYカード" and is_amazon(tx.merchant)
                        and tx.status=="amazon_unmatched"):
                    row[16]="未反映: Amazon注文と照合 または 保留 を選択してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                pair=selected_category_pair(str(row[13]),str(row[14]))
                if pair not in categories:
                    row[16]="エラー: カテゴリマスタに存在する大・小カテゴリを選択してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                expense_id=self._expense_id(tx.import_id); target=expense_id; new_status="manual_expense"
                expense=[expense_id,tx.date,tx.merchant,"手動計上",tx.amount,pair[0],pair[1],
                          tx.row[7],tx.source,"",tx.import_id,str(row[15]).strip(),"active"]
                if expense_id in expense_idx: expense_updates.append((expense_idx[expense_id],expense))
                else: expense_new.append(expense)
                stats["expenses_created"]+=1
            elif action=="重複として除外":
                target=str(row[12]).strip(); new_status="manual_duplicate_excluded"
                for expense_row_num,expense_raw in self.db.expense_rows_for_import(tx.import_id):
                    expense=list(expense_raw)+[""]*max(0,13-len(expense_raw)); expense=expense[:13]
                    expense[12]="duplicate_excluded"
                    expense_updates.append((expense_row_num,expense)); stats["expenses_excluded"]+=1
            elif action=="レシートと統合":
                target=str(row[12]).strip(); receipt=by_id.get(target)
                if receipt is None or receipt.source!="receipt":
                    row[16]="エラー: 実在するレシートの取込IDを入力してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                new_status="matched_receipt"

            updated=list(tx.row); updated[8]=new_status; updated[9]=target
            manual_note=str(row[15]).strip()
            annotation=f"スマホ判断={action}"+(f"; {manual_note}" if manual_note else "")
            updated[11]="; ".join(x for x in (tx.note,annotation) if x)
            import_updates.append((tx.row_num,updated))
            row[16]="反映済み"; review_updates.append((row_num,row)); stats["applied"]+=1

        if expense_new or expense_updates: self.db.ensure_expense_status_column()
        self.db.append("支出明細",expense_new)
        self.db.update_rows("支出明細",expense_updates)
        self.db.update_rows("取込データ",import_updates)
        self.db.update_rows("要確認",review_updates)
        return stats
