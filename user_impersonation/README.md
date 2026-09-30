# Login as User

Continue your session inside another user's account, without logging out and
without knowing their password.

## What it does

From **Settings → Users & Companies → Users**, open a user and press
**Log in as**. After a confirmation, the page reloads and you are working as
that user. A bar stays pinned to the bottom of the screen for the whole
session; pressing **Return to my account** puts you back where you were.

While you are in somebody else's account you have **their** rights - their
menus, their record rules, their companies, their language. Not yours, and not
the two combined. That is the point: you see exactly what they see.

## What it records

Every impersonation is written to **Settings → Users & Companies →
Impersonation Log**: who did it, whose account it was, the IP address, when it
started and when it ended. The log cannot be edited or deleted from the
interface by anyone.

A note is also posted on the target's contact record. It is an internal note
with no recipients, so the user is never e-mailed and never sees an unread
notification.

## What it refuses

* Anyone without the **Log in as Another User** group.
* Yourself, archived users, the public user, and the system superuser
  (`OdooBot`).
* Any user holding more rights than you - a support agent cannot reach
  Settings by way of somebody who has it.
* A second impersonation from inside the first. Return to your own account
  before switching again.

Each rule is enforced on the server, in `_check_impersonation_allowed`. The
form button is driven by the same method, so hiding it and refusing it can
never disagree.

## Installation

1. Copy `user_impersonation` into your addons path.
2. Restart the server and update the apps list.
3. Install **Login as User**.
Settings users (`base.group_system`) receive the **Log in as Another User**
privilege on install, so the button is there straight away. It remains a
separate group: take it away from someone who should keep Settings but not
this, or grant it to a support agent who is not an administrator at all.

You will find it on the user form under **Access Rights → Log in as Another
User**.

## Security notes

* The session token is recomputed for the new user, so the swapped session is
  as valid as one created by a real login, and no more.
* The session id is re-issued on the way out of the request. Only a hash of it
  is ever written to the log - the id itself is a credential.
* The **Return** route is guarded by the session marker, not by a group. An
  account with no rights at all must still be able to get out.
* Returning is a plain link with no CSRF token, deliberately: the worst an
  attacker could achieve by forcing it is to end an impersonation early, which
  removes access rather than granting it.

## Supported versions

Built and tested against **Odoo 19**. It uses only documented, long-lived
server APIs (`Session.finalize`'s own pattern, `ir.http.session_info`, the
`main_components` registry), so a port should be small - but Odoo 20 is not
released yet and no compatibility claim can honestly be made for it until it
is.

## Tests

```
odoo-bin -d <db> -i user_impersonation --test-enable --test-tags /user_impersonation
```
