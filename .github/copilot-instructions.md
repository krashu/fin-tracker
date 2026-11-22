# Copilot instructions — fin-tracker

Repository conventions and invariants are specified in the tracked `AGENTS.md` files:
- [Root AGENTS.md](../AGENTS.md): Universal domain invariants (money, tenancy, clock), orientation, commands, and boundaries.
- [backend/AGENTS.md](../backend/AGENTS.md): Backend routing, models/migrations, clock, logging, and `ty` type checking.
- [frontend/AGENTS.md](../frontend/AGENTS.md): Frontend state invalidation, API client mirror, component rules, and balance masking.

Consult these files and the relevant `docs/adr/` records before generating code.
