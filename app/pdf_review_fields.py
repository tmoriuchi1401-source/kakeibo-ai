"""Pure shared PDF review wire fields; no Medical/runner/writer imports."""
from datetime import date,timedelta
import re
from .drive_run_state import StateError

SCHEMA='pdf-page-review-v1'
TITLE='PDFページ確認'
QUEUE='_PDF確認受付'
SHEET_ID=261001091
PAYMENT_DATE_HINT='原本の支払日。PCではダブルクリックでカレンダー選択。直接入力も可：yyyy/mm/dd（例：2026/10/04）。入力途中は空欄可、確定時は必須。iPhoneでカレンダーが出ない場合は直接入力してください。'


def sheet_date_value(value):
    if value is None or value=='':return ''
    epoch=date(1899,12,30)
    try:
        if isinstance(value,bool):raise ValueError()
        if isinstance(value,(int,float)):
            serial=int(value)
            if serial!=value:raise ValueError()
            epoch+timedelta(days=serial);return serial
        if type(value) is date:day=value
        else:
            parts=re.fullmatch(r'(\d{4})([-/])(\d{1,2})\2(\d{1,2})',str(value).strip())
            if not parts:raise ValueError()
            day=date(int(parts[1]),int(parts[3]),int(parts[4]))
        return (day-epoch).days
    except (ValueError,OverflowError,TypeError):raise StateError('pdf_date_input_invalid') from None


def original_uri(source_id,number):
    if source_id.startswith('synthetic-'):return ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}',source_id):raise StateError('pdf_review_source_invalid')
    return 'https://drive.google.com/file/d/'+source_id+'/view#page='+str(number)
