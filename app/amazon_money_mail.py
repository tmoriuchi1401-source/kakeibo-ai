"""Final-money adapters. Nonmonetary Amazon mail creates no stored event."""
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from .amazon_money import MoneyRecord, MoneyError
from .amazon_email import _body, _label, _amount, ORDER_ID_RE


def is_amazon_money_merchant(value):
    value=unicodedata.normalize("NFKC",str(value)).upper()
    return "AMAZON" in value or "アマゾン" in value


def _message(raw,domain):
    msg=BytesParser(policy=policy.default).parsebytes(raw)
    sender=parseaddr(str(msg.get("From","")))[1].lower().rsplit("@",1)[-1]
    if sender!=domain and not sender.endswith("."+domain):raise MoneyError("money_sender_invalid")
    text=unicodedata.normalize("NFKC",_body(msg))
    subject=unicodedata.normalize("NFKC",str(msg.get("Subject","")))
    return msg,subject,text


def _day(msg,text,labels):
    value=_label(text,labels)
    match=re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?",value or "")
    if match:
        try:return datetime(*map(int,match.groups())).date().isoformat()
        except ValueError:raise MoneyError("money_date_invalid") from None
    try:
        stamp=parsedate_to_datetime(str(msg.get("Date","")))
        if stamp.tzinfo is None:raise ValueError()
        return stamp.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    except (ValueError,TypeError,OverflowError):raise MoneyError("money_date_missing") from None


def card_money_from_mail(raw,*,gmail_id):
    """Only issuer sales-detail notices; refunds use confirmation, not purchase day."""
    from .aupay_mail_pipeline import parse_aupay_card_raw_partial
    msg,subject,text=_message(raw,"kddi-fs.com")
    if "【ご利用詳細】" not in subject or "au PAY カード" not in subject or "速報" in subject:
        return [],0
    parsed=parse_aupay_card_raw_partial(raw)
    if parsed.mail_rejection_reason:raise MoneyError("money_card_parse_failed")
    records=[]
    rfc=str(msg.get("Message-ID",""))
    for item in parsed.accepted_items:
        if not is_amazon_money_merchant(item.merchant):continue
        if not item.member:raise MoneyError("money_card_account_missing")
        kind="refund" if item.transaction_kind=="return" else "purchase"
        day=_day(msg,text,("返金確定日","返金処理日","返金日")) if kind=="refund" else item.date
        order=ORDER_ID_RE.search(item.merchant)
        records.append(MoneyRecord("au_pay_card",item.source_record_id,"au PAYカード／"+item.member,
            "message:"+rfc+":"+str(item.item_number),day,item.amount_yen,kind,"card",True,
            order_id=order.group() if order else "",original_url="https://mail.google.com/mail/u/0/#all/"+gmail_id))
    return records,len(parsed.review_items)


def amazon_money_from_mail(raw,*,gmail_id):
    msg,subject,text=_message(raw,"amazon.co.jp")
    content=subject+"\n"+text
    # Order confirmations, delivery, return requests and refund estimates do not
    # establish any monetary fact, even if they contain an amount.
    if re.search(r"(?:返金が完了|返金処理が完了|お支払いが確定|請求が確定|お支払いが完了)"
                 r"(?:していません|していない|しておりません|するまで|する予定|次第|予定)",content):
        return None
    refunded=any(s in content for s in ("返金が完了","返金処理が完了","返金を処理しました","返金いたしました"))
    charged=any(s in content for s in ("お支払いが確定","請求が確定","請求確定のお知らせ","お支払いが完了"))
    if not (refunded or charged):return None
    kind="refund" if refunded else "purchase"
    method=_label(text,("支払い方法","お支払い方法","返金方法")) or ""
    normalized=method.upper()
    card=any(x in normalized for x in ("VISA","MASTER","JCB","AMEX","クレジット","カード払い","AU PAY カード"))
    gift="ギフト" in method
    cash="代金引換" in method or "現金" in method
    bank=any(x in method for x in ("銀行振込","コンビニ払い","ATM払い"))
    count=sum((card,gift,cash,bank))
    payment="mixed" if count>1 else "card" if card else "gift_balance" if gift else "cash" if cash else "bank_payment" if bank else "unknown"
    # Non-card/mixed legs are never inferred by subtracting order totals, points,
    # gift balances or expected refunds from another source's amount.
    amount=_amount(text,("返金額",)) if refunded else _amount(text,("実際の請求額","今回の請求額","ご請求額","請求金額","お支払い金額"))
    if amount is None or amount<=0:raise MoneyError("money_confirmed_amount_missing")
    day=_day(msg,text,("返金確定日","返金処理日","返金日") if refunded else ("請求確定日","請求日","支払日"))
    rfc=str(msg.get("Message-ID","")).strip()
    if not rfc:raise MoneyError("money_message_id_missing")
    reference=_label(text,("返金ID", "返金番号")) if refunded else _label(text,("請求ID","決済ID","取引ID"))
    reference=("transaction:"+reference.strip()) if reference else "message:"+rfc
    order=ORDER_ID_RE.search(text)
    if kind=="purchase" and any(x in text for x in ("自分のギフトカード残高にチャージ","ギフトカード残高へのチャージ")):
        kind="transfer"
    return MoneyRecord("amazon",rfc,"Amazon／"+payment,reference,day,-amount if refunded else amount,
        kind,payment,True,order_id=order.group() if order else "",
        original_url="https://mail.google.com/mail/u/0/#all/"+gmail_id)


def amazon_money_outcome(raw,*,gmail_id):
    from .amazon_money_notices import notice,REASONS
    try:return amazon_money_from_mail(raw,gmail_id=gmail_id),None
    except MoneyError as error:
        if str(error) not in REASONS:raise
        return None,notice("amazon",gmail_id,raw,str(error))


def card_money_outcome(raw,*,gmail_id):
    from .amazon_money_notices import notice,REASONS
    try:msg,subject,text=_message(raw,"kddi-fs.com")
    except MoneyError:return [],None  # Existing card parser rejects the sender.
    if ("【ご利用詳細】" not in subject or "au PAY カード" not in subject or "速報" in subject
            or not is_amazon_money_merchant(text)):return [],None
    try:
        records,remaining=card_money_from_mail(raw,gmail_id=gmail_id)
        return records,notice("au_pay_card",gmail_id,raw,"money_card_partial") if remaining else None
    except (MoneyError,ValueError) as error:
        reason=str(error) if str(error) in REASONS else "money_card_parse_failed"
        return [],notice("au_pay_card",gmail_id,raw,reason)
