"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
from datetime import date
from ..drive_run_state import StateError
from ..receipt_reimport import _date,_money
TABLES=('レシート','支出明細','取込データ')
def table_rows(db,title):
    width={'レシート':'I','支出明細':'M','取込データ':'L'}[title]
    return db.get_raw(f"'{title}'!A2:{width}")

def check_duplicates(db,source_id,result,*,verified_distinct=frozenset()):
    """Same identity is handled by exact replay; other matching totals are held."""
    rid='R-'+source_id;iid='receipt:'+source_id
    if any(r and r[0] in {rid,iid} for title in ('レシート','取込データ') for r in table_rows(db,title)):
        raise StateError('canary_existing_identity_without_intent')
    for row in table_rows(db,'レシート'):
        if len(row)<4:continue
        if row[0] in verified_distinct:continue
        day=_date(row[1])
        if day and _money(row[3])==result.total and abs((date.fromisoformat(day)-date.fromisoformat(result.date)).days)<=7:
            raise StateError('canary_possible_duplicate')
    for row in table_rows(db,'支出明細'):
        if len(row)<5:continue
        if len(row)>12 and row[12] in {'superseded','excluded'}:continue
        if len(row)>9 and row[9] in verified_distinct:continue
        day=_date(row[1])
        if day and _money(row[4])==result.total and abs((date.fromisoformat(day)-date.fromisoformat(result.date)).days)<=7:
            raise StateError('canary_possible_duplicate')

class PlanningDB:
    """Exercise the real writer without persistence; refuse any other capability."""
    def __init__(self,categories):self.cats=categories;self.plan=[]
    def categories(self):return self.cats
    def import_ids(self):return set()
    def receipt_ids(self):return set()
    def expense_index(self):return {}
    def ensure_expense_status_column(self):pass
    def append(self,title,rows):
        if title not in TABLES:raise StateError('canary_write_forbidden')
        self.plan.extend((title,deepcopy(row)) for row in rows)
