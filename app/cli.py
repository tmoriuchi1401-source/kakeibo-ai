from __future__ import annotations
import argparse, csv, mimetypes, os
from .settings import Settings
from .sheets import SheetsDB
from .gemini_ai import GeminiAI
from .receipt_pipeline import ReceiptPipeline
from .amazon_pipeline import AmazonPipeline
from .drive_receipts import process_inbox
from .drive_paypay import DrivePayPayPipeline
from .aupay_card_pipeline import AuPayCardPipeline
from .paypay_pipeline import PayPayPipeline
from .aupay_mail_pipeline import (
    AuPayMailPipeline,
    authorize_gmail,
    parse_eml,
    parse_aupay_card_eml,
)
from .aupay_csv_pipeline import AuPayCsvPipeline
from .reconciliation import ReconciliationPipeline
from .review_pipeline import ReviewApprovalPipeline, ReviewPipeline
from .expense_view import ExpenseViewPipeline
from .maintenance import (
    authorize_drive_backup,
    backup_drive_service,
    backup_spreadsheet,
    cleanup_processed_receipts,
)
from .auto_expense import AutoExpensePipeline
from .amazon_installment import AmazonInstallmentPipeline
from .amazon_csv_diagnostics import diagnose_amazon_csv_amounts
from .amazon_unmatched import (
    AmazonUnmatchedPreview,
    export_amazon_unmatched_input,
    load_amazon_unmatched_input,
)
from .amazon_email import parse_amazon_email
from .amazon_gmail_preview import gmail_readonly_service
from .amazon_gmail_storage import import_amazon_gmail_events
from .amazon_daily_import import run_amazon_daily_import
from .amazon_cancellation_return_preview import preview_amazon_cancellation_returns
from .amazon_event_reparse_preview import (
    apply_amazon_event_reparse,
    preview_amazon_event_reparse,
)
from .amazon_event_matching import AmazonEventMatchingPipeline
from .amazon_order_header_preview import preview_amazon_order_headers
from .amazon_schema_install import install_amazon_schema
from .amazon_shipping import AmazonShippingBackfillPipeline
from .drive_amazon_shipping import DriveAmazonShippingPipeline
from .google_clients import (
    read_only_drive_service,
    read_only_sheets_service,
    shipping_backfill_drive_service,
    shipping_backfill_sheets_service,
)
from .payroll_statement_parser import preview_payroll_file
from .drive_payroll import DrivePayrollPreview
from .payroll_sheets import PayrollSheetsReadRepository
from .payroll_storage_preview import (
    build_append_plan,
    drive_save_preview,
    drive_storage_candidates,
    preview_summary,
)
from .payroll_schema import (
    PayrollSchemaWriteRepository,
    apply_schema_initialization,
    build_schema_initialization_plan,
    schema_plan_preview,
)
from .payroll_master_sync import build_master_sync_plan, master_sync_preview

def load_categories(path="config/categories.tsv"):
    with open(path,encoding="utf-8") as f:
        rd=csv.reader(f,delimiter="\t"); next(rd,None); return [(r[0],r[1]) for r in rd if len(r)>=2]

def make(require_gemini=True):
    s=Settings(); s.validate(need_gemini=require_gemini,need_sheet=True)
    db=SheetsDB(s.spreadsheet_id)
    ai=GeminiAI(s.gemini_api_key,s.gemini_model) if require_gemini else None
    return s,db,ai

def main():
    p=argparse.ArgumentParser(description="家計簿AI")
    sub=p.add_subparsers(dest="cmd",required=True)
    sub.add_parser("init")
    r=sub.add_parser("receipt"); r.add_argument("image")
    an=sub.add_parser("analyze"); an.add_argument("image")
    a=sub.add_parser("amazon"); a.add_argument("csv")
    ab=sub.add_parser("amazon-baseline"); ab.add_argument("csv")
    asp=sub.add_parser("amazon-shipping-backfill-preview"); asp.add_argument("csv")
    asa=sub.add_parser("amazon-shipping-backfill"); asa.add_argument("csv")
    sub.add_parser("amazon-shipping-backfill-drive-preview")
    sub.add_parser("amazon-shipping-backfill-drive-apply")
    cp=sub.add_parser("card-preview"); cp.add_argument("csv")
    ci=sub.add_parser("card-import"); ci.add_argument("csv")
    sub.add_parser("card-amazon-reclassify")
    pp=sub.add_parser("paypay-preview"); pp.add_argument("csv")
    pi=sub.add_parser("paypay-import"); pi.add_argument("csv")
    ae=sub.add_parser("aupay-eml"); ae.add_argument("eml")
    ag=sub.add_parser("aupay-gmail"); ag.add_argument("--max-results",type=int,default=100)
    acp=sub.add_parser("aupay-csv-preview"); acp.add_argument("csv")
    aci=sub.add_parser("aupay-csv-import"); aci.add_argument("csv")
    ce=sub.add_parser("card-eml-import"); ce.add_argument("eml")
    ga=sub.add_parser("gmail-authorize")
    ga.add_argument("client_json")
    ga.add_argument("token_output")
    sub.add_parser("reconcile-preview")
    sub.add_parser("reconcile")
    sub.add_parser("review-preview")
    sub.add_parser("review-refresh")
    sub.add_parser("review-apply-preview")
    sub.add_parser("review-apply")
    sub.add_parser("expenses-preview")
    sub.add_parser("expenses-refresh")
    sub.add_parser("auto-expense-preview")
    sub.add_parser("auto-expense")
    sub.add_parser("amazon-installment-preview")
    sub.add_parser("amazon-installment-apply")
    sub.add_parser("amazon-event-match")
    sub.add_parser("amazon-order-header-preview")
    sub.add_parser("amazon-schema-install")
    sub.add_parser("amazon-gmail-import")
    sub.add_parser("amazon-daily-import")
    sub.add_parser("amazon-cancellation-return-preview")
    sub.add_parser("amazon-event-reparse-preview")
    aera=sub.add_parser("amazon-event-reparse-apply")
    aera.add_argument("--apply",action="store_true")
    aup=sub.add_parser("amazon-unmatched-preview")
    aup.add_argument("--amazon-csv")
    aup.add_argument("--transactions-json")
    aue=sub.add_parser("amazon-unmatched-export")
    aue.add_argument("--output",required=True)
    aep=sub.add_parser("amazon-email-preview")
    aep.add_argument("eml")
    sub.add_parser("drive-receipts")
    sub.add_parser("drive-paypay-preview")
    sub.add_parser("drive-paypay")
    sub.add_parser("backup")
    dba=sub.add_parser("drive-backup-authorize")
    dba.add_argument("client_json")
    dba.add_argument("token_output",nargs="?",default="drive-backup-token.json")
    sub.add_parser("receipts-cleanup-preview")
    sub.add_parser("receipts-cleanup")
    sub.add_parser("doctor")
    payroll_preview=sub.add_parser("payroll-file-preview")
    payroll_preview.add_argument("file")
    sub.add_parser("payroll-import-preview")
    sub.add_parser("payroll-drive-preview")
    sub.add_parser("payroll-storage-preview")
    sub.add_parser("payroll-save-preview")
    sub.add_parser("payroll-schema-preview")
    sub.add_parser("payroll-master-sync-preview")
    payroll_schema_apply=sub.add_parser("payroll-schema-apply")
    payroll_schema_apply.add_argument("--apply",action="store_true")
    args=p.parse_args()
    if args.cmd=="doctor":
        import importlib.util
        checks = {
            "SPREADSHEET_ID": bool(Settings().spreadsheet_id),
            "service-account.json": os.path.exists(Settings().service_account_file) or bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON","").strip()),
            "google.genai": importlib.util.find_spec("google.genai") is not None,
            "googleapiclient": importlib.util.find_spec("googleapiclient") is not None,
            "pandas": importlib.util.find_spec("pandas") is not None,
        }
        for k,v in checks.items(): print(f"{'OK' if v else 'NG'}  {k}")
        if not all(checks.values()):
            raise SystemExit(1)
        print("開発環境チェック完了")
    elif args.cmd=="payroll-file-preview":
        import json
        print(json.dumps(preview_payroll_file(args.file).model_dump(),ensure_ascii=False))
    elif args.cmd in {"payroll-import-preview", "payroll-drive-preview"}:
        import json
        s=Settings(); s.validate(need_payroll_drive=True)
        print(json.dumps(DrivePayrollPreview(s.payroll_drive_folder_id).preview(),ensure_ascii=False))
    elif args.cmd=="payroll-storage-preview":
        import json
        s=Settings(); s.validate(need_sheet=True, need_payroll_drive=True)
        snapshot=PayrollSheetsReadRepository(s.spreadsheet_id).snapshot()
        candidates=drive_storage_candidates(s.payroll_drive_folder_id,snapshot)
        plans=build_append_plan(candidates,snapshot)
        print(json.dumps(preview_summary(plans,snapshot),ensure_ascii=False))
    elif args.cmd=="payroll-save-preview":
        import json
        s=Settings(); s.validate(need_sheet=True, need_payroll_drive=True)
        snapshot=PayrollSheetsReadRepository(s.spreadsheet_id).snapshot()
        print(json.dumps(
            drive_save_preview(s.payroll_drive_folder_id,snapshot),
            ensure_ascii=False,
        ))
    elif args.cmd in {"payroll-schema-preview", "payroll-schema-apply"}:
        import json
        s=Settings(); s.validate(need_sheet=True)
        reader=PayrollSheetsReadRepository(s.spreadsheet_id)
        plan=build_schema_initialization_plan(reader)
        if args.cmd=="payroll-schema-apply" and args.apply:
            result=apply_schema_initialization(
                plan,PayrollSchemaWriteRepository(s.spreadsheet_id),confirmed=True,
            )
            print(json.dumps(result,ensure_ascii=False))
        else:
            output=schema_plan_preview(plan)
            output["applied"]=False
            print(json.dumps(output,ensure_ascii=False))
    elif args.cmd=="payroll-master-sync-preview":
        import json
        s=Settings(); s.validate(need_sheet=True)
        snapshot=PayrollSheetsReadRepository(s.spreadsheet_id).snapshot()
        print(json.dumps(
            master_sync_preview(build_master_sync_plan(snapshot)),
            ensure_ascii=False,
        ))
    elif args.cmd=="init":
        s,db,_=make(False); db.ensure_schema(load_categories()); print("Sheets初期化/検証完了")
    elif args.cmd=="receipt":
        s,db,ai=make(); data=open(args.image,"rb").read(); mime=mimetypes.guess_type(args.image)[0] or "image/jpeg"
        print(ReceiptPipeline(db,ai).process_bytes(data,mime,os.path.basename(args.image)))
    elif args.cmd=="analyze":
        s,db,ai=make(); data=open(args.image,"rb").read(); mime=mimetypes.guess_type(args.image)[0] or "image/jpeg"
        result=ai.analyze_receipt(data,mime,db.categories())
        print(result.model_dump())
    elif args.cmd=="amazon":
        s,db,ai=make(); print(AmazonPipeline(db,ai).import_csv(args.csv))
    elif args.cmd=="amazon-baseline":
        s,db,_=make(False); print(AmazonPipeline(db,None).import_csv(args.csv,baseline=True))
    elif args.cmd=="amazon-shipping-backfill-preview":
        s,db,_=make(False); print(AmazonShippingBackfillPipeline(db).preview(args.csv))
    elif args.cmd=="amazon-shipping-backfill":
        s,db,_=make(False); print(AmazonShippingBackfillPipeline(db).apply(args.csv))
    elif args.cmd=="amazon-shipping-backfill-drive-preview":
        s=Settings()
        s.validate(need_sheet=True)
        if not s.amazon_order_history_folder_id:
            raise RuntimeError("未設定: AMAZON_ORDER_HISTORY_FOLDER_ID")
        db=SheetsDB(s.spreadsheet_id,service=read_only_sheets_service())
        result=DriveAmazonShippingPipeline(
            s.amazon_order_history_folder_id,db,read_only_drive_service(),
        ).preview()
        for key in ("csv_file","csv_rows","matched_amazon_rows",
                    "would_update_ship_date","would_update_shipment_count",
                    "ambiguous","unmatched"):
            print(f"{key}={result[key]}")
    elif args.cmd=="amazon-shipping-backfill-drive-apply":
        s=Settings()
        s.validate(need_sheet=True)
        if not s.amazon_order_history_folder_id:
            raise RuntimeError("未設定: AMAZON_ORDER_HISTORY_FOLDER_ID")
        db=SheetsDB(s.spreadsheet_id,service=shipping_backfill_sheets_service())
        result=DriveAmazonShippingPipeline(
            s.amazon_order_history_folder_id,db,shipping_backfill_drive_service(),
        ).apply()
        for key in ("csv_file","csv_rows","matched_amazon_rows",
                    "would_update_ship_date","would_update_shipment_count",
                    "ambiguous","unmatched","updated_rows"):
            print(f"{key}={result[key]}")
    elif args.cmd=="card-preview":
        s,db,_=make(False); print(AuPayCardPipeline(db).preview(args.csv))
    elif args.cmd=="card-import":
        s,db,_=make(False); print(AuPayCardPipeline(db).import_csv(args.csv))
    elif args.cmd=="card-amazon-reclassify":
        s,db,_=make(False); print(AuPayCardPipeline(db).reclassify_amazon())
    elif args.cmd=="paypay-preview":
        print(PayPayPipeline().preview(args.csv))
    elif args.cmd=="paypay-import":
        s,db,_=make(False); print(PayPayPipeline(db).import_csv(args.csv))
    elif args.cmd=="aupay-eml":
        s,db,_=make(False); print(AuPayMailPipeline(db).import_notice(parse_eml(args.eml)))
    elif args.cmd=="aupay-gmail":
        s,db,_=make(False); s.validate(need_gmail=True)
        print(AuPayMailPipeline(db).import_gmail(s.gmail_token_json,s.aupay_gmail_query,args.max_results))
    elif args.cmd=="aupay-csv-preview":
        s,db,_=make(False); print(AuPayCsvPipeline(db).preview(args.csv))
    elif args.cmd=="aupay-csv-import":
        s,db,_=make(False); print(AuPayCsvPipeline(db).import_csv(args.csv))
    elif args.cmd=="card-eml-import":
        s,db,_=make(False)
        print(AuPayCardPipeline(db).import_transactions(parse_aupay_card_eml(args.eml)))
    elif args.cmd=="gmail-authorize":
        authorize_gmail(args.client_json,args.token_output)
        print(f"Gmail読み取り用トークンを保存しました: {args.token_output}")
    elif args.cmd=="reconcile-preview":
        s,db,_=make(False); print(ReconciliationPipeline(db,s.reconciliation_lookback_months).preview())
    elif args.cmd=="reconcile":
        s,db,_=make(False); print(ReconciliationPipeline(db,s.reconciliation_lookback_months).apply())
    elif args.cmd=="review-preview":
        s,db,_=make(False); print(ReviewPipeline(db).preview())
    elif args.cmd=="review-refresh":
        s,db,_=make(False); print(ReviewPipeline(db).refresh())
    elif args.cmd=="review-apply-preview":
        s,db,_=make(False); print(ReviewApprovalPipeline(db).preview())
    elif args.cmd=="review-apply":
        s,db,_=make(False); print(ReviewApprovalPipeline(db).apply())
    elif args.cmd=="expenses-preview":
        s,db,_=make(False); print(ExpenseViewPipeline(db).preview())
    elif args.cmd=="expenses-refresh":
        s,db,_=make(False); print(ExpenseViewPipeline(db).refresh())
    elif args.cmd=="auto-expense-preview":
        s,db,_=make(False); print(AutoExpensePipeline(db).preview())
    elif args.cmd=="auto-expense":
        s,db,_=make(False); print(AutoExpensePipeline(db).apply())
    elif args.cmd=="amazon-installment-preview":
        s,db,_=make(False); print(AmazonInstallmentPipeline(db).preview())
    elif args.cmd=="amazon-installment-apply":
        s,db,ai=make(True); print(AmazonInstallmentPipeline(db,ai).apply())
    elif args.cmd=="amazon-event-match":
        s,db,_=make(False); print(AmazonEventMatchingPipeline(db).apply())
    elif args.cmd=="amazon-order-header-preview":
        s=Settings(); s.validate(need_sheet=True)
        db=SheetsDB(s.spreadsheet_id,service=read_only_sheets_service())
        print(preview_amazon_order_headers(db))
    elif args.cmd=="amazon-schema-install":
        s,db,_=make(False); print(install_amazon_schema(db))
    elif args.cmd=="amazon-gmail-import":
        s=Settings(); s.validate(need_sheet=True,need_gmail=True)
        db=SheetsDB(s.spreadsheet_id)
        service=gmail_readonly_service(s.gmail_token_json)
        print(import_amazon_gmail_events(service,db))
    elif args.cmd=="amazon-daily-import":
        s=Settings(); s.validate(need_sheet=True,need_gmail=True)
        db=SheetsDB(s.spreadsheet_id)
        service=gmail_readonly_service(s.gmail_token_json)
        print(run_amazon_daily_import(service,db))
    elif args.cmd=="amazon-cancellation-return-preview":
        s=Settings(); s.validate(need_gmail=True,need_sheet=True)
        service=gmail_readonly_service(s.gmail_token_json)
        db=SheetsDB(s.spreadsheet_id,service=read_only_sheets_service())
        print(preview_amazon_cancellation_returns(service,db=db))
    elif args.cmd=="amazon-event-reparse-preview":
        s=Settings(); s.validate(need_sheet=True,need_gmail=True)
        db=SheetsDB(s.spreadsheet_id,service=read_only_sheets_service())
        service=gmail_readonly_service(s.gmail_token_json)
        print(preview_amazon_event_reparse(service,db))
    elif args.cmd=="amazon-event-reparse-apply":
        if not args.apply:
            p.error("amazon-event-reparse-apply requires --apply")
        s=Settings(); s.validate(need_sheet=True,need_gmail=True)
        db=SheetsDB(s.spreadsheet_id)
        service=gmail_readonly_service(s.gmail_token_json)
        print(apply_amazon_event_reparse(service,db))
    elif args.cmd=="amazon-unmatched-preview":
        if args.transactions_json:
            if not args.amazon_csv:
                p.error("--transactions-jsonには--amazon-csvが必要です")
            transactions=load_amazon_unmatched_input(args.transactions_json)
            print({"raw_csv_diagnostics":diagnose_amazon_csv_amounts(args.amazon_csv,transactions)})
        else:
            s,db,_=make(False); print(AmazonUnmatchedPreview(db).preview(args.amazon_csv))
    elif args.cmd=="amazon-unmatched-export":
        s,db,_=make(False); print(export_amazon_unmatched_input(db,args.output))
    elif args.cmd=="amazon-email-preview":
        with open(args.eml,"rb") as f:
            print(parse_amazon_email(f.read()).anonymized())
    elif args.cmd=="drive-receipts":
        s,db,ai=make(); s.validate(need_drive=True)
        for name,res in process_inbox(s.receipt_drive_folder_id,ReceiptPipeline(db,ai),s.processed_drive_folder_id): print(name,res)
    elif args.cmd=="drive-paypay-preview":
        s=Settings(); s.validate(need_paypay_drive=True)
        print(DrivePayPayPipeline(s.paypay_drive_folder_id).preview())
    elif args.cmd=="drive-paypay":
        s,db,_=make(False); s.validate(need_paypay_drive=True)
        print(DrivePayPayPipeline(
            s.paypay_drive_folder_id, db, s.processed_drive_folder_id,
        ).apply())
    elif args.cmd=="backup":
        s=Settings(); s.validate(need_sheet=True,need_backup=True)
        print(backup_spreadsheet(
            s.spreadsheet_id,s.backup_drive_folder_id,
            service=backup_drive_service(s.drive_backup_token()),
        ))
    elif args.cmd=="drive-backup-authorize":
        authorize_drive_backup(args.client_json,args.token_output)
        print(f"Driveバックアップ用トークンを保存しました: {args.token_output}")
    elif args.cmd=="receipts-cleanup-preview":
        s=Settings(); s.validate(need_processed=True)
        print(cleanup_processed_receipts(s.processed_drive_folder_id))
    elif args.cmd=="receipts-cleanup":
        s=Settings(); s.validate(need_processed=True)
        print(cleanup_processed_receipts(s.processed_drive_folder_id,apply=True))

if __name__=="__main__":main()
