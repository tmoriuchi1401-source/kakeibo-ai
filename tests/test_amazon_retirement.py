"""Retired order entrypoints cannot initialize services or mutate old data."""
from pathlib import Path
import sys

import pytest

RETIRED="""amazon amazon-baseline amazon-cancellation-apply-preview
amazon-cancellation-item-count-fill-apply amazon-cancellation-item-count-fill-preview
amazon-cancellation-order-id-diagnose amazon-cancellation-order-status-apply
amazon-cancellation-quantity-ambiguity-diagnose amazon-cancellation-quantity-preview
amazon-cancellation-reconciliation-preview amazon-cancellation-return-preview
amazon-cancellation-scope-diagnose amazon-daily-import amazon-email-preview
amazon-event-match amazon-event-reparse-apply amazon-event-reparse-preview
amazon-gmail-import amazon-installment-apply amazon-installment-preview
amazon-order-header-preview amazon-payment-coverage-preview amazon-production-preview
amazon-review-preview amazon-review-schema-install amazon-schema-install
amazon-shipping-backfill amazon-shipping-backfill-drive-apply
amazon-shipping-backfill-drive-preview amazon-shipping-backfill-preview
amazon-status-sync-preview amazon-unmatched-export amazon-unmatched-preview
card-amazon-reclassify payment-coverage-status-preview""".split()


@pytest.mark.parametrize("command",RETIRED)
def test_removed_cli_rejects_before_authentication_or_writes(monkeypatch,command):
    from app import cli
    def forbidden(*args,**kwargs):raise AssertionError("retired command accessed services")
    monkeypatch.setattr(cli,"Settings",forbidden)
    monkeypatch.setattr(cli,"make",forbidden)
    monkeypatch.setattr(sys,"argv",["app.cli",command])
    with pytest.raises(SystemExit) as error:cli.main()
    assert error.value.code==2


def test_recurring_entry_cannot_fall_back_to_order_events_without_migration(monkeypatch,capsys):
    from app import cli
    monkeypatch.delenv("KAKEIBO_AMAZON_MONEY_MODE",raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY",raising=False)
    monkeypatch.setattr(cli,"Settings",lambda:pytest.fail("credentials accessed before mode validation"))
    monkeypatch.setattr(sys,"argv",["app.cli","amazon-gmail-recurring","--dry-run"])
    with pytest.raises(SystemExit):cli.main()
    assert "amazon_money_migration_required" in capsys.readouterr().out


@pytest.mark.parametrize("name",["amazon_gmail_preview","amazon_gmail_search_preview"])
def test_standalone_order_diagnostics_cannot_fetch_mail(monkeypatch,capsys,name):
    import importlib
    module=importlib.import_module("app."+name)
    monkeypatch.setattr(module,"gmail_readonly_service",lambda *args:pytest.fail("retired entry accessed Gmail"))
    assert module.main()==1
    assert "amazon_order_processing_retired" in capsys.readouterr().out


def test_no_order_lifecycle_workflow_remains_active():
    root=Path(__file__).resolve().parents[1]/".github/workflows"
    assert {p.name for p in root.glob("amazon*.yml")}=={"amazon-daily-import.yml"}
    for path in root.glob("*.yml"):
        text=path.read_text(encoding="utf-8")
        for command in RETIRED:
            # Token comparison prevents amazon from matching monetary entry.
            assert not any(line.strip().startswith("run: python -m app.cli "+command+" ") or line.strip()=="run: python -m app.cli "+command for line in text.splitlines()),(path.name,command)


def test_money_reconciliation_does_not_exclude_receipt_or_card_by_order_similarity(monkeypatch):
    from app.reconciliation import reconcile_transactions,parse_import_rows
    from test_reconciliation import row
    monkeypatch.setenv("KAKEIBO_AMAZON_MONEY_MODE","confirmed-v1")
    data=[row("order","Amazon","2026-08-16","Amazon",1000,"canonical_amazon"),
          row("receipt","receipt","2026-08-16","Amazon",1000,"解析済"),
          row("card","au PAYカード","2026-08-16","Amazon",1000,"auto_expense"),
          row("normal-r","receipt","2026-08-16","店舗",500,"解析済"),
          row("normal-c","au PAYカード","2026-08-16","店舗",500,"unclassified_card")]
    result=reconcile_transactions(parse_import_rows(data))
    assert [(d.transaction.import_id,d.status) for d in result]==[("normal-c","matched_receipt")]


@pytest.mark.parametrize("amount",[1000,-300])
@pytest.mark.parametrize("source,status",[("au PAYカード","unclassified_card"),("PayPay","unclassified_paypay")])
def test_unconfirmed_legacy_amazon_payment_is_reviewed_not_posted(monkeypatch,amount,source,status):
    from app.auto_expense import AutoExpensePipeline
    from test_auto_expense import FakeDB,import_row
    monkeypatch.setenv("KAKEIBO_AMAZON_MONEY_MODE","confirmed-v1")
    db=FakeDB([import_row("old",source,"Amazon",amount,status)])
    result=AutoExpensePipeline(db).apply()
    assert result["expenses_created"]==result["expenses_updated"]==0
    assert db.updated["取込データ"][0][1][8]=="needs_review_amazon_money"


def test_card_csv_does_not_read_order_table_and_preserves_non_amazon_routes(monkeypatch):
    from app.aupay_card_pipeline import AuPayCardPipeline
    monkeypatch.setenv("KAKEIBO_AMAZON_MONEY_MODE","confirmed-v1")
    class DB:
        def import_ids(self):return set()
        def get(self,*args):pytest.fail("retired order table read")
        def append(self,title,rows):self.rows=rows
    db=DB();pipeline=AuPayCardPipeline(db)
    tx={"import_id":"card","date":"2026-09-01","merchant":"Amazon","amount":1000,
        "payment_type":"1回","member":"本人","memo":"","occurrence":1}
    result=pipeline.import_transactions([tx,{**tx,"import_id":"other","merchant":"店舗"}])
    assert result["needs_review_amazon_money"]==1 and result["unclassified_card"]==1
    assert db.rows[0][8]=="needs_review_amazon_money"
    with pytest.raises(RuntimeError,match="retired"):pipeline.reclassify_amazon()
