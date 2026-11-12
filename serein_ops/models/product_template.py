from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_seeding_flare = fields.Boolean(
        string='Is Seeding Flare',
        help="A flare that releases seeding material when fired. Flares are "
             "stocked by batch and bought from a vendor.",
    )
    grams_per_flare = fields.Float(
        string='Seeding Material per Flare (g)',
        help="Grams of seeding material one flare releases.",
    )

    _grams_per_flare_positive = models.Constraint(
        'check(grams_per_flare >= 0)',
        'Grams per flare cannot be negative.',
    )

    @api.onchange('is_seeding_flare')
    def _onchange_is_seeding_flare(self):
        if not self.is_seeding_flare:
            return
        self.type = 'consu'
        self.is_storable = True
        self.tracking = 'lot'
        # Bought through the warehouse's Buy route, from the vendors on the
        # Purchase tab.
        self.purchase_ok = True
