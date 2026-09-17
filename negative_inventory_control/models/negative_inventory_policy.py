from markupsafe import Markup

POLICY_SELECTION = [
    ('allow', "Allow"),
    ('warn', "Warn"),
    ('approval', "Approval Required"),
    ('block', "Block"),
]

POLICY_LABELS = dict(POLICY_SELECTION)

# Severity increases with position in POLICY_SELECTION (Allow < Warn < Approval < Block).
_POLICY_SEVERITY = {key: index for index, (key, _label) in enumerate(POLICY_SELECTION)}


def strictest_policy(policies):
    """Return the strictest (highest-severity) policy among `policies`.

    Empty/falsy values (meaning "inherit from the next level up") are ignored.
    Returns False if none of the given values is explicitly set.
    """
    explicit_policies = [policy for policy in policies if policy]
    if not explicit_policies:
        return False
    return max(explicit_policies, key=_POLICY_SEVERITY.get)


def format_entries_message(env, entries, header):
    """Render a list of negative-inventory detection entries (see
    stock.location._project_negative_inventory_entry) as a plain-text,
    newline-separated message - for UserError text and toast notifications,
    neither of which render HTML.

    Takes `env` explicitly rather than using the bare `odoo._()` shortcut:
    that shortcut finds the current language by walking the Python call
    stack looking for a frame with an env in scope, which works fine called
    directly from a model method but not from a plain helper function like
    this one, one frame further removed - it would silently give up and log
    a warning instead of translating.
    """
    lines = [header]
    for entry in entries:
        lines.append(env._(
            "- %(product)s at %(location)s: on hand %(on_hand).2f, requested %(decrement).2f -> would result in %(projected).2f (policy: %(policy)s)",
            product=entry['product'].display_name,
            location=entry['location'].display_name,
            on_hand=entry['on_hand'],
            decrement=entry['decrement'],
            projected=entry['projected'],
            policy=POLICY_LABELS.get(entry['policy'], entry['policy']),
        ))
    return "\n".join(lines)


def format_entries_message_html(env, entries, header):
    """Same as `format_entries_message`, but as safe HTML (real <p> tags, not
    escaped-into-visible-text ones) for chatter posts. Uses Markup's %
    operator, which auto-escapes every interpolated value while leaving the
    literal template markup intact - passing a plain str with hand-inserted
    tags to message_post() gets the whole string escaped, which is why that
    approach showed literal "<br/>" text in the chatter instead of a break.
    """
    parts = [Markup("<p>%s</p>") % header]
    for entry in entries:
        parts.append(Markup(env._(
            "<p>- %(product)s at %(location)s: on hand %(on_hand).2f, requested "
            "%(decrement).2f → would result in %(projected).2f (policy: %(policy)s)</p>"
        )) % {
            'product': entry['product'].display_name,
            'location': entry['location'].display_name,
            'on_hand': entry['on_hand'],
            'decrement': entry['decrement'],
            'projected': entry['projected'],
            'policy': POLICY_LABELS.get(entry['policy'], entry['policy']),
        })
    return Markup('').join(parts)


def build_log_vals(entry, outcome, company, **origin_refs):
    """Build a negative.inventory.log create-vals dict from a detection
    entry. `origin_refs` carries whichever of picking_id/move_id/scrap_id/
    quant_id/request_id/reason applies to the caller - all optional.
    """
    vals = {
        'product_id': entry['product'].id,
        'location_id': entry['location'].id,
        'company_id': company.id,
        'quantity_before': entry['on_hand'],
        'transaction_quantity': entry['decrement'],
        'quantity_after': entry['projected'],
        'policy_applied': entry['policy'],
        'outcome': outcome,
    }
    vals.update(origin_refs)
    return vals
