import json
import pytest
from app.pdf_single_card_ui import pending, patch_requests, locate
from app.drive_run_state import StateError
from app.pdf_page_review import SCHEMA
from test_page_receipts import page, CATEGORIES

def rows(p):
    ident=json.dumps({'source_file_id':p.source.source_file_id,'page_numbers':[p.page_number]})
    protected=[['medical unchanged',42],['unknown other card',12]]
    block=[['x','',SCHEMA,'old',ident,'x'] for _ in range(13)]
    return protected+block+[['synthetic link untouched']]

@pytest.mark.parametrize('shift',[0,7,42])
def test_scoped_card_follows_binding_not_fixed_coordinates(shift):
    p,_=page();before=[['unrelated']]*shift+rows(p)
    reqs=patch_requests(before,before,pending(p),CATEGORIES)
    indices=locate(before,p.source.source_file_id,p.page_number)
    for r in reqs:
        area=next(iter(r.values()))['range']
        assert area['startRowIndex']>=indices[0] and area['endRowIndex']<=indices[-1]+1
    assert all('一般支出を確定' not in json.dumps(r,ensure_ascii=False) for r in reqs)
    cells=reqs[0]['updateCells']['rows']
    dates=[r for r in cells if r['values'][5]['userEnteredValue']=={'stringValue':'date'}]
    assert len(dates)==1 and dates[0]['values'][1]['userEnteredValue']=={'stringValue':''}
    assert any(r.get('setDataValidation',{}).get('rule',{}).get('condition',{}).get('type')=='DATE_IS_VALID' for r in reqs)

def test_projection_changed_rejected_and_no_request_or_writer():
    p,_=page();before=rows(p)
    with pytest.raises(StateError):patch_requests(before,[['changed']],pending(p),CATEGORIES)
    assert pending(p)['identity']['accounting_allowed'] is False

def test_fragmented_card_refused():
    p,_=page();before=rows(p);before.insert(5,['unrelated'])
    with pytest.raises(StateError):patch_requests(before,before,pending(p),CATEGORIES)


@pytest.mark.parametrize('count',[1,2,3])
def test_completion_cards_expand_only_target_page_without_posting(count):
    from app.pdf_single_card_ui import completion_card_requests
    from app.general_receipt_completion import card,drafts_for_page
    from test_general_receipt_completion import setup
    p,raw,m,a,b=setup(count)
    initial=pending(p)
    before=[['Medical unchanged']]+[[l,v,SCHEMA,initial['token'],json.dumps(initial['identity']),f] for f,l,v in initial['rows']]+[['Other unchanged']]
    cards=[card(r,categories=CATEGORIES) for r in drafts_for_page(p,m,a,b,categories=CATEGORIES)]
    reqs=completion_card_requests(before,before,cards,CATEGORIES)
    insert=[r for r in reqs if 'insertDimension' in r]
    assert len(insert)==(1 if count>1 else 0)
    if insert:assert insert[0]['insertDimension']['range']=={'sheetId':261001091,'dimension':'ROWS','startIndex':14,'endIndex':14+13*(count-1)}
    updates=[r['updateCells'] for r in reqs if 'updateCells' in r]
    assert len(updates)==count and all(u['range']['startRowIndex']==1+13*i for i,u in enumerate(updates))
    assert not any('deleteDimension' in r for r in reqs)
    assert '一般支出を確定' not in json.dumps(reqs,ensure_ascii=False)
