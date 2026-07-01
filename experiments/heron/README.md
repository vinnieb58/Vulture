# Heron — expense report preparation for Brown & Root Atomiq

Heron runs on **Raven** and helps create **draft** expense entries in Atomiq from receipt files. It never submits, approves, certifies, finalizes, or sends an expense report.

## Data on Raven (outside git)

```
/mnt/pelican_backup/Heron/
  inbox/      # drop receipt PDFs/images here
  reviewed/   # one JSON candidate per receipt
  done/       # optional archive after Atomiq draft
```

Real receipt files stay on Raven — they are **not** copied into this repo.

## Workflow

1. **Extract** — read inbox receipts, write conservative JSON to `reviewed/`:
   ```bash
   python experiments/heron/heron_extract.py --root /mnt/pelican_backup/Heron
   ```

2. **Review** — confirm or edit fields in the terminal:
   ```bash
   python experiments/heron/heron_review.py --file /mnt/pelican_backup/Heron/reviewed/example.json
   ```

3. **Probe Atomiq** (read-only UI mapping, manual login):
   ```bash
   python experiments/heron/heron_atomiq_probe.py --headed --save-session
   python experiments/heron/heron_atomiq_probe.py --headed --use-session --dump-ui
   ```

4. **Draft entry** (default dry-run; `--live` required to touch the site):
   ```bash
   python experiments/heron/heron_atomiq_draft.py --expense /mnt/pelican_backup/Heron/reviewed/example.json --dry-run --headed
   python experiments/heron/heron_atomiq_draft.py --expense /mnt/pelican_backup/Heron/reviewed/example.json --live --headed
   ```

## Safety

- Unknown extracted values are `null` — Heron does not hallucinate receipt data.
- Low-confidence extractions set `needs_review=true`.
- Only `status="reviewed"` expenses can be drafted in Atomiq.
- Hard blocks refuse interaction with controls containing: submit, finalize, approve, certify, send for approval, complete report, reimbursement submit, pay, checkout.
- No SSO/MFA/CAPTCHA bypass — manual login only.

## Known mappings

| Category guess | Cost code guess |
|----------------|-----------------|
| meals with tip / Meals & Entertainment / Meals | 402200 |

## Session files

Saved locally at `experiments/heron/.auth/atomiq_storage_state.json` when `--save-session` is used. Never commit credentials or session state.
