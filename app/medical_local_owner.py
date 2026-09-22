"""Owner-intent guard for fresh local readings, never for cloud sending.

An otherwise-empty positive confirmation is compatible with automatic local
validation of identical source bytes. Holds, edits, notes, changed content and
reconfirmation requests are not. No owner cell or historical input is changed.
"""
from copy import deepcopy

POSITIVE={'医療費を確定','候補で医療費を確定'}
MISSING_FIELDS='支払日・発行施設・本人の実支払額・カテゴリを入力してください'


def compatible(item,source):
    if item.get('require_reconfirm') or item['status'] in {'pending','applied'}:return False
    inputs=item['inputs']
    if item['status']=='closed_user':
        return (item['kind']=='intake' and inputs==['']*7+['医療']
                and all(item['source'].get(k)==source.get(k) for k in ('source_id','sha256','mime_type')))
    if not any(inputs):return not item.get('error')
    if not all(item['source'].get(k)==source.get(k) for k in ('source_id','sha256','mime_type')):return False
    if item['kind']=='intake':return inputs==['']*7+['医療'] and not item.get('error')
    return (item['kind']=='medical' and inputs[5] in POSITIVE
            and not any(inputs[:5]+inputs[6:]) and item.get('error','') in {'',MISSING_FIELDS})


def blocked(source,value):
    return any(not compatible(item,source) for item in value['confirmation_items'].values()
               if item['source']['source_id']==source['source_id'] and item['kind'] in {'medical','intake'})


def snapshot(review,source):
    """Read every current/historical input so a live hold always wins."""
    rows=review.ui_rows();result={}
    for key,item in review.items.items():
        if item['source']['source_id']!=source['source_id'] or item['kind'] not in {'medical','intake'}:continue
        if not compatible(item,source):return None
        live=rows.get(key)
        if live is None:
            if any(item['inputs']):return None
        elif live[1][7:15]!=item['inputs']:return None
        result[key]=deepcopy(live)
    return result if result else None
