# Receipt-only production import

Manual dispatch with `scope=receipts` runs only the existing receipt intake,
medical confirmation and ordinary receipt writer. It retains the same workflow
concurrency group, validated-main check, privacy gate, source binding and durable
receipt ledger. A failed or uncertain write remains pending for reconciliation.

Amazon, cards, PayPay, banks and global reconciliation/posting are not invoked.
Other source ledger entries remain unchanged. Bank and Amazon target options and
fixed reimport manifest options are rejected in this scope. Scheduled runs retain
their existing `all` behavior. Preview uses the existing read-only receipt path.
