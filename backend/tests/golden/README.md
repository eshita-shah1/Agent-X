# Golden contract-note tests (B2)

Each `contract_notes/*.json` file is a contract note: the trades as printed, and every charge as printed. The
test `tests/unit/test_contract_notes.py` recomputes the note from the referenced cost schedule with the production
cost model and requires **exact** equality for every component.

- `"synthetic": true` notes prove the harness only. They are constructed, not issued by a broker.
- To validate a production schedule, add **real, redacted Zerodha delivery contract notes** (at least one buy, one
  sell with a DP charge, and one day with several trades), with `"synthetic": false` and `"schedule"` pointing at the
  candidate schedule. The schedule is acceptable only when every real note matches.
- Remove names, client codes, PAN and any other personal data before adding a note.
