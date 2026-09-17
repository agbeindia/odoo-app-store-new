# Negative Inventory Control

Configurable **Allow / Warn / Approval Required / Block** policies for negative stock in Odoo 19 Community, with a full audit trail.

Odoo's core `stock` module does not prevent, warn about, or track when a transaction pushes on-hand quantity below zero — that's a deliberate design choice, not a bug (see *Design notes* below), but it means the business gets no say in the matter unless they add a module for it. This module gives Inventory managers explicit, hierarchical control over that behaviour, without modifying any core file.

## What it does

- **Detects** every transaction that would drive a location's stock negative — deliveries, internal transfers, scrap, manufacturing consumption, unbuild, and inventory adjustments.
- **Applies a configurable policy**, resolved per product/location combination:
  - **Allow** — unchanged Odoo behaviour (the default, so installing this module changes nothing until you configure it).
  - **Warn** — the transaction proceeds, but a warning is logged and posted to the transfer's chatter.
  - **Approval Required** — the transaction pauses; a stock manager must approve it (with a reason on record) before it completes.
  - **Block** — the transaction is refused outright.
- **Runs an approval workflow** for picking-driven transfers, scrap, and inventory adjustments: request → review → approve/reject → automatic resume.
- **Keeps a full audit trail** — every negative-stock event is logged, including ones that were *allowed*, so "we chose to permit this" is still on record.

## Configuration hierarchy

Policy is set at up to four levels, evaluated together with the **strictest one always winning**, regardless of which level it came from:

```
Product  ─┐
Category ─┤→ strictest policy applies
Location ─┤   (Block > Approval Required > Warn > Allow)
Company  ─┘
```

- **Company** (Inventory → Configuration → Settings): the fallback default. Defaults to *Allow*.
- **Location**: set directly on a location's form. Only meaningful for *Internal*/*Transit* locations — a warehouse's own stock/view location inherits down through its child locations, so "warehouse-level" policy is just policy set on that warehouse's root location; there is no separate warehouse field.
- **Product Category**: set on the category form; inherits up its own parent chain.
- **Product**: set directly on the product form (Inventory tab).

Leave a field empty to inherit from the next level up. Because *strictest wins*, a product can tighten a looser location's policy, but it can never loosen a stricter one — that's intentional: this is a control mechanism, and a deliberately-set guardrail at any level should never be silently overridden by a looser setting elsewhere.

## Approval workflow

When a transaction hits an Approval Required policy:

1. The transaction pauses (nothing is written yet) and a request is created, listing exactly which product/location/quantity triggered it.
2. The requester fills in a reason and submits it (Inventory → Negative Stock → Approval Requests).
3. A Stock Manager (`stock.group_stock_manager`) reviews it and approves or rejects.
4. On approval, the original transaction resumes and completes automatically. On rejection, it stays as-is with the decision recorded.

This covers picking-driven transfers (deliveries, internal transfers, manufacturing component pickings), scrap, and inventory adjustments — the three paths that can independently drive stock negative (see *Design notes*).

## Audit log

Inventory → Negative Stock → Audit Log. Read-only for everyone, including managers — a log that can be edited or deleted isn't much of an audit trail. Every entry records product, location, quantity before/requested/expected-after, the policy in effect at the time, the outcome (allowed / warned / blocked / pending approval / approved / rejected), the user, and the timestamp.

## Design notes (for maintainers)

This module deliberately targets exactly two extension points in core Odoo, chosen after tracing the actual Odoo 19 source rather than assuming behaviour from other versions:

- **`stock.move._action_done()`** — the one method common to *every* path that can drive stock negative (delivery, internal transfer, scrap, unbuild, manufacturing consumption, inventory adjustment). This is the mandatory backstop; it's what actually protects the system regardless of which higher-level path was taken.
- **`stock.picking._pre_action_done_hook()`**, **`stock.scrap.action_validate()`**, **`stock.quant._apply_inventory()`** — the three origin-specific pre-validation gates, used purely for the friendlier UX (pausing *before* anything is written, to show a Warn confirmation or create an Approval request), mirroring the exact pattern Odoo's own core already uses for its backorder-confirmation and insufficient-quantity wizards.

No core files are modified. Every new field defaults to "inherit"/"Allow", so installing this module on an existing database changes nothing until it's configured.

A Blocked outcome is immediately followed by a raised `UserError`, which rolls back the whole transaction — including, ordinarily, the audit log entry meant to record that it happened. `negative.inventory.log._create_standalone()` writes those specific entries on an independent database cursor that commits on its own, so the record of a blocked *attempt* survives even though the attempt itself never does.

## Requirements

- Odoo 19.0, Community or Enterprise.
- Depends on `stock` and `mail` only. No hard dependency on `mrp` — manufacturing consumption is covered automatically through the shared `stock.move` mechanism if/when `mrp` is installed.

## License

LGPL-3
