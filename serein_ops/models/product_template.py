from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_seeding_flare = fields.Boolean(
        string='Is Seeding Flare',
        help="A flare that releases seeding material when fired.",
    )
    grams_per_flare = fields.Float(
        string='Seeding Material per Flare (g)',
        help="Grams of seeding material one flare releases.",
    )

    _grams_per_flare_positive = models.Constraint(
        'check(grams_per_flare >= 0)',
        'Grams per flare cannot be negative.',
    )
