# scripts-archive/

Permanent archive of ad-hoc one-shot scripts that ran successfully against the
account-setup pipeline but were originally written to `/tmp` and would have been
lost on the next Mac restart.

## Policy

**Any `/tmp/*.py` script that I write and run as part of an account-creation
session gets committed here verbatim within the same session**, with:

- Filename: `<date>_<VP-id>_<purpose>.py` (e.g. `2026-06-01_VP34_finish_fb_app.py`)
- Frontmatter comment block: when it ran, which account, what it accomplished
- The exact Python source as it was executed

Why: the F34/K34 scripts that created **META APP 12 (LaunchLy app id
`2107024670234005`)** on 2026-06-01 lived only in `/tmp` and were gone the next
day. The whole 2026-06-02 → 2026-06-03 session was spent reconstructing them
from the conversation transcript. If they had been here, that would have been
30 seconds.

## What's archived

| File | Date | Account | Purpose |
|------|------|---------|---------|
| `2026-06-01_VP34_finish_fb_app.py` | 2026-06-01 | VP34 (`6a1ca48c5cdf7b25a1f4f876`) | Force-finish create_app_wizard from Use Cases page onward. Created LaunchLy app ID 2107024670234005. Source: transcript line 17408. |
| `2026-06-01_VP34_finish_ig_app.py` | 2026-06-01 | VP34 | IG-app counterpart. Continues from use-case-Next. Source: transcript line 17519. |

## Related committed scripts

These started in `/tmp` originally too, are now first-class:

- `force_complete_app_wizard.py` — generalized port of the F34 logic (parameterized over APP_NAME/USE_CASE_TEXT).
- `resume_from_apps.py` — Phase H onward when account A-G is already done.
