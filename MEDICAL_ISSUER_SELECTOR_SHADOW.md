# Medical issuer selector shadow

`app.medical_issuer_selector_shadow` selects a receipt issuer from existing
offline OCR metadata only. It is not connected to the production receipt
schema, authority evaluator, writer, Drive, or Sheets.

The selector keeps the full printed OCR text. It does not split legal entities,
chains, or branches and does not normalize facility identities. Hospital,
clinic, office, dental, pharmacy, medical-center, and legal-medical-entity
forms can become candidates.

Reference labels such as `保険医療機関名`, `処方元`, `処方医療機関`, and
`紹介元` mark a candidate as a referenced provider. Nearby OCR geometry may
propagate that reference role, but page position alone never selects an issuer.
An independent pharmacy can therefore be selected while a prescribing provider
is retained separately.

Selection fails closed when no independent facility remains, distinct issuer
candidates compete, roles conflict, or the source/image/unit/page binding does
not match. Duplicate OCR regions with the same normalized full text are folded
deterministically without merging their text.

The fixed ten-unit local evaluation matched human issuer adjudication 10/10,
including pharmacy/provider separation for Units 2, 3, and 9. Human labels were
used only after selection for scoring. Real names, images, and OCR dumps remain
outside Git.
