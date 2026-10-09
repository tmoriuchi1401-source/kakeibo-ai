"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
import json
from ..drive_run_state import StateError
from .items import SCHEMA,check_snapshot
def read_snapshot(rows,expected,original_link):
    found=[r for r in rows if len(r)>20 and r[17]==SCHEMA and r[18]==expected['token']]
    if len(found)!=len(expected['rows']):raise StateError('item_review_snapshot_stale')
    if any(json.loads(r[19])!=expected['identity'] for r in found):raise StateError('item_review_ui_identity_changed')
    current={'identity':expected['identity'],'token':expected['token'],
             'rows':[[r[20],r[0],r[1]] for r in found],'items':[],'original_link':original_link}
    baseline={i['item_id']:i for i in expected['items']}
    for row in found:
        if row[20].startswith('item:'):
            key=row[20][5:]
            if key not in baseline or row[21]!=key:raise StateError('item_review_ui_item_changed')
            current['items'].append({**baseline[key],'name':row[0],'amount':row[1],
                                    'category':row[16] if len(row)>16 else ''})
    check_snapshot(current,expected)
    return current
