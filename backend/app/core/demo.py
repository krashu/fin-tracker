"""Demo account constants (PRD §Users & access v2).

The seeded fixed-UUID row is the demo account (migration 0017 stamps these creds).
The frontend's "Try the demo" button logs in with them, and tests authenticate the
seeded user through the real login flow.

The password is intentionally public, so **the login is refused by default** —
:attr:`app.core.config.Settings.demo_login_permitted` requires an explicit
``DEMO_LOGIN_ENABLED`` on plain http (ADR-0003 §Demo account gate). Note the row is
``v1_user_id``: on a dev-seeded install it owns the synthetic dataset, but on one
upgraded through 0017 from the pre-auth single-user era it owns the operator's REAL
data — which is why the gate is opt-in rather than opt-out. If the argon2 hash in
migration 0017 is ever regenerated, it must correspond to :data:`DEMO_PASSWORD`.
"""

from __future__ import annotations

DEMO_EMAIL = "demo@fin-tracker.local"
DEMO_PASSWORD = "demofintracker"
