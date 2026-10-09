"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
import re
from ..drive_run_state import StateError
def original_uri(source_id,number):
    if source_id.startswith('synthetic-'):return ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}',source_id):raise StateError('pdf_review_source_invalid')
    return 'https://drive.google.com/file/d/'+source_id+'/view#page='+str(number)
