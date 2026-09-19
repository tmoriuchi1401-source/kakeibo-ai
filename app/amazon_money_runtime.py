"""Opt-in money mode shared by Amazon and its card-payment source."""
import os

from .amazon_money import MoneyError,decide,validate_book
from .amazon_money_writer import MoneyLedger,MoneyWriter


def money_enabled(env=None):
    env=os.environ if env is None else env
    value=env.get("KAKEIBO_AMAZON_MONEY_MODE","")
    if value not in {"","confirmed-v1"}:raise MoneyError("money_mode_invalid")
    return value=="confirmed-v1"


def money_writer(db,env=None):
    from .projection_store import store_from_environment
    env=os.environ if env is None else env
    if not money_enabled(env):return None
    store=store_from_environment(db.sid,env)
    if store is None:raise MoneyError("money_projection_binding_required")
    validate_book(store.read("money"))
    from .monthly_projection_sheets import SheetsLedgerReader
    from .projection_refresh import ProjectionRefresh
    from .projection_store import ProjectionJournal
    from .monthly_projection import shift_month
    from datetime import date
    reader=SheetsLedgerReader(db)
    can_refresh=False
    def prepare():
        nonlocal can_refresh
        can_refresh=True
        ProjectionRefresh(store,reader).refresh(db.categories())
    def possible(record):
        dirty=ProjectionJournal(store).read()
        if dirty["append"] or dirty["ranges"] or dirty["months"]:
            if not can_refresh:raise MoneyError("money_projection_refresh_required")
            ProjectionRefresh(store,reader).refresh(db.categories())
        for month in [shift_month(record.day[:7],offset) for offset in (-1,0,1)]:
            projection=ProjectionRefresh(store,reader).read_month(month)
            if projection is None:continue
            for purchase in projection.purchases:
                merchant=purchase.merchant.upper()
                if (not purchase.purchase_id.startswith("AM-") and purchase.amount==record.amount
                        and ("AMAZON" in merchant or "アマゾン" in merchant)
                        and abs((date.fromisoformat(purchase.day)-date.fromisoformat(record.day)).days)<=7):
                    return True
        return False
    from .amazon_money_products import SavedProducts
    writer=MoneyWriter(store,MoneyLedger(db),possible_duplicates=possible,
        product_details=SavedProducts(db,auto_apply=env.get("CATEGORY_RULE_AUTO_APPLY_ENABLED","false").strip().lower() in {"1","true","yes","on"}))
    writer.prepare=prepare
    return writer


def run_money_records(writer,records,*,dry_run,limit):
    # Preview also uses the pinned migration book. No bootstrap by normal runs.
    if dry_run:
        decisions=writer.preview(records)
        return {"money_eligible":sum(d.action in {"post","resume"} for d in decisions),
                "money_review":sum(d.action=="review" for d in decisions),
                "money_supplement":sum(d.action=="supplement" for d in decisions),
                "money_posted":0,"expense_rows_written":0,"import_rows_written":0}
    if records and hasattr(writer,"prepare"):writer.prepare()
    book=writer._book()
    pending=[r for r in records if decide(r,book).action!="duplicate"]
    return writer.apply(pending,limit=limit)


def run_money_canary(writer,records,*,dry_run,target=""):
    """One exact monetary ID; never advances a source checkpoint."""
    import re
    if target and not re.fullmatch(r"AM-[0-9a-f]{32}",target):raise MoneyError("money_canary_target_invalid")
    if not target:
        if not dry_run:raise MoneyError("money_canary_target_required")
        return run_money_records(writer,records,dry_run=True,limit=1)
    matches=[r for r in records if r.money_id==target]
    if not matches:raise MoneyError("money_canary_target_not_found")
    if len({r.fingerprint for r in matches})!=1:raise MoneyError("money_canary_target_conflict")
    record=matches[0]
    if not dry_run and hasattr(writer,"prepare"):writer.prepare()
    book=writer._book()
    decision=writer._decision(record,book)
    posted=book["records"].get(target,{}).get("state")=="posted"
    if decision.action not in {"post","resume"} and not (decision.action=="duplicate" and posted):
        raise MoneyError("money_canary_not_postable")
    if dry_run:return {"money_eligible":int(decision.action in {"post","resume"}),"money_canary_selected":1,
        "money_canary_replay":int(posted),"money_posted":0,"expense_rows_written":0,"import_rows_written":0}
    result=writer.apply([record],limit=1)
    writer.verify_posted(record)
    return {**result,"money_canary_selected":1,"money_canary_verified":1,"money_canary_replay":int(posted)}


def run_amazon_money_messages(writer,messages,*,dry_run,limit,canary_target=None):
    from .amazon_money_mail import amazon_money_outcome
    from .amazon_money_notices import save_notices
    records=[];notices=[]
    for message in messages:
        record,notice=amazon_money_outcome(message.raw_mime,gmail_id=message.gmail_message_id)
        if record is not None:records.append(record)
        if notice is not None:notices.append(notice)
    notice_counts=save_notices(writer.store,notices,dry_run=dry_run or canary_target is not None)
    result=(run_money_canary(writer,records,dry_run=dry_run,target=canary_target)
            if canary_target is not None else run_money_records(writer,records,dry_run=dry_run,limit=limit))
    return {**result,**notice_counts,"event_rows_written":0,"header_rows_written":0,
            "status":"dry_run_ready" if dry_run else "complete","failure":0}


def money_review_items(store):
    from .daily_view import ReviewItem
    from .amazon_money_notices import review_items
    notices=review_items(store)
    book=store.read("money")
    if book is None:return notices
    validate_book(book)
    labels={"confirmation_missing":"確定情報を確認", "payment_leg_unresolved":"支払元・混合払いを確認",
        "legacy_payment_identity_required":"旧計上との対応を確認", "legacy_unsettled_purchase_possible":"旧計上との重複の可能性",
        "refund_purchase_link_required":"返金の元購入を指定", "refund_purchase_link_invalid":"返金の関連先を確認",
        "refund_exceeds_purchase":"返金額と元購入を確認", "possible_resend_requires_identity":"再通知か別取引かを確認",
        "source_identity_already_imported":"既存取込との対応を確認", "existing_expense_identity_required":"既存支出との重複候補を確認"}
    return notices+[ReviewItem(identity,"Amazon金銭",f"{item.get('day','')} / {item.get('amount','')}円 / {item.get('account','')}\n"+labels.get(item.get("reason"),"金銭記録の情報を確認"),
        "反映待ち" if item["state"]=="pending" else "保留" if item.get("resolution",{}).get("action")=="hold" else "要確認",item.get("original_url",""))
        for identity,item in book["records"].items() if item["state"] in {"review","pending"}]
