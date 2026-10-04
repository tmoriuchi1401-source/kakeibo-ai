from types import SimpleNamespace
import pytest
from app.models import ReceiptResult
from app.receipt_validation import apply_receipt_policy, validate_receipt_result

CATS=[('食費','食品')]
def result(**changes):
    return ReceiptResult.model_validate(dict(date='2026-09-26',total=159,
        items=[dict(name='商品',amount=159,major_category='食費',minor_category='食品')],**changes))

@pytest.mark.parametrize('payment',['','不明','現金','PayPay'])
def test_optional_payment_and_merchant_no_inference(payment):
    r=result(payment_method=payment)
    issues,checks=apply_receipt_policy(r,CATS,text='商品 159円 合計159円')
    assert not issues and r.merchant=='' and r.payment_method==''
    assert validate_receipt_result(r,CATS)[0]

def test_optional_null_fields_are_blank_and_quantity_is_not_required():
    r=result(merchant=None,payment_method=None);r.items[0].quantity=None
    assert r.merchant==r.payment_method==''
    assert validate_receipt_result(r,CATS)[0]

def test_readable_payment_retained_and_explicit_contradiction_held():
    r=result(payment_method='現金')
    assert not apply_receipt_policy(r,CATS,text='支払方法 現金')[0]
    assert r.payment_method=='現金'
    r=result(payment_method='現金')
    assert apply_receipt_policy(r,CATS,text='支払方法 カード')[0]
    assert r.payment_method==''

@pytest.mark.parametrize('change',[{'date':''},{'date':'2026-02-30'},{'total':0},{'total':160},
    {'items':[]},{'items':[dict(name='商品',amount=159,major_category='新規',minor_category='AI生成')]}])
def test_required_and_item_safety_preserved(change):
    value=result().model_dump();value.update(change)
    assert not validate_receipt_result(ReceiptResult.model_validate(value),CATS)[0]

def test_adjustment_and_major_reread_changes_held_but_empty_merchant_not():
    r=result();r.items[0].name='調整額'
    assert apply_receipt_policy(r,CATS,text='合計159円')[0]
    r=result()
    assert apply_receipt_policy(r,CATS,readings=[r,r.model_copy(update={'total':160})])[0]
    assert not apply_receipt_policy(r,CATS,readings=[r,r.model_copy(update={'merchant':'店舗'})])[0]
    assert apply_receipt_policy(r.model_copy(update={'transaction_kind':'unknown'}),CATS)[0]
    assert apply_receipt_policy(r,CATS,gate=SimpleNamespace(buyback_evidence=True))[0]
