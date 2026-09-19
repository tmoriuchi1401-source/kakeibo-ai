"""No-write decisions that preserve established accounting and owner choices."""
from .auto_expense import expense_id
from .receipt_reimport import _date, _money, compare_receipt


def manual_expense_already_recorded(source_id, parsed, *, receipt_rows, import_rows,
                                    expense_rows, review_rows, categories):
    rid,iid='R-'+source_id,'receipt:'+source_id
    receipts=[list(r)+['']*max(0,9-len(r)) for r in receipt_rows if r and r[0]==rid]
    imports=[list(r)+['']*max(0,12-len(r)) for r in import_rows if r and r[0]==iid]
    eid=expense_id(iid)
    expenses=[list(r)+['']*max(0,13-len(r)) for r in expense_rows if r and
              (r[0]==eid or (len(r)>9 and r[9]==rid) or (len(r)>10 and r[10]==iid))]
    if len(receipts)!=1 or len(imports)!=1 or len(expenses)!=1:return False
    h,i,e=receipts[0],imports[0],expenses[0]
    if (i[2:4]!=['receipt',source_id] or i[8:10]!=['manual_expense',eid]
            or e[0]!=eid or e[3]!='手動計上' or e[8:11]!=['receipt','',iid]
            or e[12]!='active' or tuple(e[5:7]) not in set(map(tuple,categories))):return False
    if (_date(h[1])!=parsed.date or _date(i[4])!=parsed.date or _date(e[1])!=parsed.date
            or _money(h[3])!=parsed.total or _money(i[6])!=parsed.total or _money(e[4])!=parsed.total
            or parsed.total<=0 or not h[2] or not h[2]==i[5]==e[2]
            or not h[4]==i[7]==e[7]):return False
    return not any(r and r[0]==iid and any((list(r)+['']*15)[9:15]) for r in review_rows)


def general_review_guidance(source_id, parsed, **tables):
    comparison=compare_receipt(source_id,parsed,**tables)
    if any(r and r[0]=='receipt:'+source_id and len(r)>9 and
           (str(r[8]).startswith('manual_') or r[9]) for r in tables['import_rows']):
        return '既存の判断・記帳あり。追加計上せず、原本と既存記帳を確認してください。'
    if 'missing_detail_candidates' in comparison.reasons:
        return '明細不足。未計上なら「候補明細で確定」、既存支出ありなら「既存支出と重複（紐付け）」を確認。迷う場合は保留。'
    changes=[]
    for row in tables['receipt_rows']:
        if not row or row[0]!='R-'+source_id:continue
        for same,label in [(_date(row[1])==parsed.date,'日付'),(_money(row[3])==parsed.total,'合計金額'),
                           (row[2]==parsed.merchant,'店舗表記'),(row[4]==parsed.payment_method,'支払方法')]:
            if not same:changes.append(label)
    indexed={r[0]:r for r in tables['expense_rows'] if len(r)>=13}
    for n,item in enumerate(parsed.items,1):
        row=indexed.get(f'R-{source_id}-{n:02d}')
        if not row:continue
        for same,label in [(row[3]==item.name,'商品名'),(_money(row[4])==item.amount,'明細金額'),
                           (row[5:7]==[item.major_category,item.minor_category],'カテゴリ')]:
            if not same:changes.append(label)
    differences='・'.join(dict.fromkeys(changes)) or '記帳条件'
    return f'差分：{differences}。現在の記帳が正しければ「既存値を維持」を推奨。変更する場合だけ候補を確認。'
