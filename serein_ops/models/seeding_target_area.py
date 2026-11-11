from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SeedingTargetArea(models.Model):
    _name = 'seeding.target.area'
    _description = 'Seeding Target Area'
    _inherit = ['mail.thread']
    _order = 'partner_id, name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, tracking=True,
        index=True,
        domain=[('is_company', '=', True)],
        help="The organisation that pays for seeding. Only companies can be clients.",
    )
    country_id = fields.Many2one('res.country', string='Country', tracking=True)
    latitude = fields.Float(digits=(10, 6), required=True, tracking=True)
    longitude = fields.Float(digits=(10, 6), required=True, tracking=True)
    radius_km = fields.Float(
        string='Radius (km)', default=25.0, required=True, tracking=True,
    )
    contract_ids = fields.Many2many(
        'seeding.contract', 'seeding_contract_area_rel', 'area_id', 'contract_id',
        string='Contracts', readonly=True,
    )
    note = fields.Text()

    _latitude_range = models.Constraint(
        'check(latitude >= -90 and latitude <= 90)',
        'Latitude must be between -90 and 90 degrees.',
    )
    _longitude_range = models.Constraint(
        'check(longitude >= -180 and longitude <= 180)',
        'Longitude must be between -180 and 180 degrees.',
    )
    _radius_positive = models.Constraint(
        'check(radius_km > 0)',
        'The radius must be greater than zero.',
    )

    @api.constrains('partner_id')
    def _check_partner_matches_contracts(self):
        for area in self:
            wrong = area.contract_ids.filtered(lambda c: c.partner_id != area.partner_id)
            if wrong:
                raise ValidationError(_(
                    "Area %(area)s is on contract %(contract)s, which belongs to "
                    "another client. Remove it from that contract first.",
                    area=area.name, contract=wrong[0].name,
                ))
