---
paths:
  - "custom_components/tibber_pulse_mqtt/repairs.py"
---

# Repairs rules

- Use an `issue_id` that stays stable across restarts.
- Issues must survive Home Assistant restarts and support persistent dismissal. Do not recreate a dismissed issue unless new underlying data exists.
- Use a `fix_flow` when user action is required; issue descriptions themselves are informational only.
- All titles and descriptions must use translations (update `strings.json` and every locale in `translations/`).
- Never delete user data automatically without confirmation.
