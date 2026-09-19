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
    from .projection_refresh import ProjectionRefresh,load_month
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
            projection=load_month(store.read("month-"+month))
            if projection is None:continue
            for purchase in projection.purchases:
                merchant=purchase.merchant.upper()
                if (not purchase.purchase_id.startswith("AM-") and purchase.amount==record.amount
                        and ("AMAZON" in merchant or "アマゾン" in merchant)
                        and abs((date.fromisoformat(purchase.day)-date.fromisoformat(record.day)).days)<=7):
                    return True
        return False
    writer=MoneyWriter(store,MoneyLedger(db),possible_duplicates=possible)
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


def run_amazon_money_messages(writer,messages,*,dry_run,limit):
    from .amazon_money_mail import amazon_money_from_mail
    records=[]
    for message in messages:
        record=amazon_money_from_mail(message.raw_mime,gmail_id=message.gmail_message_id)
        if record is not None:records.append(record)
    result=run_money_records(writer,records,dry_run=dry_run,limit=limit)
    return {**result,"event_rows_written":0,"header_rows_written":0,
            "status":"dry_run_ready" if dry_run else "complete","failure":0}


def money_review_items(store):
    from .daily_view import ReviewItem
    book=store.read("money")
    if book is None:return []
    validate_book(book)
    labels={"confirmation_missing":"確定情報を確認", "payment_leg_unresolved":"支払元・混合払いを確認",
        "legacy_payment_identity_required":"旧計上との対応を確認", "legacy_unsettled_purchase_possible":"旧計上との重複の可能性",
        "refund_purchase_link_required":"返金の元購入を指定", "refund_purchase_link_invalid":"返金の関連先を確認",
        "refund_exceeds_purchase":"返金額と元購入を確認", "possible_resend_requires_identity":"再通知か別取引かを確認",
        "source_identity_already_imported":"既存取込との対応を確認", "existing_expense_identity_required":"既存支出との重複候補を確認"}
    return [ReviewItem(identity,"Amazon金銭",labels.get(item.get("reason"),"金銭記録の情報を確認"),
        "反映待ち" if item["state"]=="pending" else "要確認",item.get("original_url",""))
        for identity,item in book["records"].items() if item["state"] in {"review","pending"}]
