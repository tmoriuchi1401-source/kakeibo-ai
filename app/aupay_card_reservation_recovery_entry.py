"""Independent owner-approved recovery release; never reruns an old entry."""
from . import aupay_card_reconciliation_entry as existing
from .aupay_card_reservation_recovery import RECOVERY_WORKFLOW

CONFIRM="APPLY_NEW_CARD_RESERVATION_RECOVERY_ONCE"
LABEL=b"kakeibo-card-reservation-recovery-v2"
SCOPE="fixed_card_unused_reservation_recovery_only"
RECORD_SHA256="1558548ced53ca1689e5f076424b79c4fa88c95877bf35492eafb93a411ea7c5"


def boundary(env,head,github):
    existing.boundary(env,head,github,workflow=RECOVERY_WORKFLOW,confirm=CONFIRM,per_release=True)


def run(env,head,github,diagnostics=None):
    return existing.run(env,head,github,diagnostics,boundary_checker=boundary,record_sha256=RECORD_SHA256,label=LABEL,scope=SCOPE)


if __name__=="__main__":
    raise SystemExit(existing.main(run_profile=run,guard_profile=boundary))
