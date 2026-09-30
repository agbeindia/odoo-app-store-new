import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { session } from "@web/session";

/**
 * A bar pinned to the bottom of the screen for the whole of an impersonated
 * session.
 *
 * It is driven by `session.impersonation`, which the server only fills in when
 * this browser session carries the impersonation marker. A user signing in
 * normally never gets the key, so they never see the bar - even though, to the
 * ORM, they are the same user the administrator is currently working as.
 */
export class ImpersonationBanner extends Component {
    static template = "user_impersonation.ImpersonationBanner";
    static props = {};

    setup() {
        this.info = session.impersonation;
        if (this.info) {
            // The bar is fixed, so it would otherwise sit on top of the last
            // row of a list. The class pads the action manager to match.
            document.body.classList.add("o_impersonation_active");
        }
    }
}

registry.category("main_components").add("user_impersonation.ImpersonationBanner", {
    Component: ImpersonationBanner,
});
