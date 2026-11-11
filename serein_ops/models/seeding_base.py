from odoo import api, fields, models


class SeedingBase(models.Model):
    _name = 'seeding.base'
    _description = 'Seeding Airbase'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    location_id = fields.Many2one(
        'stock.location', string='Stock Location',
        domain="[('usage', '=', 'internal')]",
        check_company=True,
        help="Where this base keeps its flares. Left empty, a location is "
             "created automatically under the company's main warehouse.",
    )
    aircraft_ids = fields.One2many(
        'maintenance.equipment', 'home_base_id', string='Aircraft',
    )
    aircraft_count = fields.Integer(compute='_compute_aircraft_count')
    note = fields.Text()

    _name_uniq = models.Constraint(
        'unique (name, company_id)',
        'A base with this name already exists.',
    )

    @api.depends('aircraft_ids')
    def _compute_aircraft_count(self):
        for base in self:
            base.aircraft_count = len(base.aircraft_ids)

    @api.model_create_multi
    def create(self, vals_list):
        bases = super().create(vals_list)
        for base in bases.filtered(lambda b: not b.location_id):
            base.location_id = base._create_stock_location()
        return bases

    def _create_stock_location(self):
        """Create an internal stock location for this base under the
        company's first warehouse (or at the top level if there is none).
        Done as superuser: a Serein manager need not be an inventory manager."""
        self.ensure_one()
        warehouse = self.env['stock.warehouse'].sudo().search(
            [('company_id', '=', self.company_id.id)], limit=1,
        )
        return self.env['stock.location'].sudo().create({
            'name': self.name,
            'usage': 'internal',
            'location_id': warehouse.view_location_id.id or False,
            'company_id': self.company_id.id,
        })
