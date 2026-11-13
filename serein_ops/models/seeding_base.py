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
             "created automatically inside the stock of the company's main warehouse.",
    )
    orderpoint_count = fields.Integer(string='Reorder Rules', compute='_compute_orderpoint_count')
    aircraft_ids = fields.One2many(
        'maintenance.equipment', 'home_base_id', string='Aircraft',
    )
    aircraft_count = fields.Integer(compute='_compute_aircraft_count')
    area_ids = fields.Many2many(
        'seeding.target.area', 'seeding_base_area_rel', 'base_id', 'area_id',
        string='Target Areas Served',
        help="Areas this base's aircraft fly to. Without any, its aircraft may fly to every area.",
    )
    crew_ids = fields.One2many(
        'hr.employee', 'home_base_id', string='Crew', groups='hr.group_hr_user',
        domain=[('is_pilot', '=', True)],
    )
    note = fields.Text()

    _name_uniq = models.Constraint(
        'unique (name, company_id)',
        'A base with this name already exists.',
    )

    @api.depends('aircraft_ids')
    def _compute_aircraft_count(self):
        for base in self:
            base.aircraft_count = len(base.aircraft_ids)

    def _compute_orderpoint_count(self):
        Orderpoint = self.env['stock.warehouse.orderpoint'].sudo()
        for base in self:
            base.orderpoint_count = Orderpoint.search_count(
                [('location_id', '=', base.location_id.id)],
            ) if base.location_id else 0

    @api.model_create_multi
    def create(self, vals_list):
        bases = super().create(vals_list)
        for base in bases.filtered(lambda b: not b.location_id):
            base.location_id = base._create_stock_location()
        return bases

    def _company_warehouse(self):
        self.ensure_one()
        return self.env['stock.warehouse'].sudo().search(
            [('company_id', '=', self.company_id.id)], limit=1,
        )

    def _create_stock_location(self):
        """Create an internal stock location for this base inside the stock
        location of the company's first warehouse (or at the top level if
        there is none). Inside the warehouse stock, a reorder rule at the base
        finds the Buy route and receipts go straight to the base.
        Done as superuser: a Serein manager need not be an inventory manager."""
        self.ensure_one()
        warehouse = self._company_warehouse()
        return self.env['stock.location'].sudo().create({
            'name': self.name,
            'usage': 'internal',
            'location_id': warehouse.lot_stock_id.id or False,
            'company_id': self.company_id.id,
        })

    def _place_location_under_stock(self):
        """Bases created before flare purchasing had their location directly
        under the warehouse. Move such a location into the warehouse stock."""
        for base in self:
            location = base.location_id.sudo()
            warehouse = base._company_warehouse()
            if location and warehouse and location.location_id == warehouse.view_location_id:
                location.location_id = warehouse.lot_stock_id

    def action_view_orderpoints(self):
        """Reorder rules for flares at this base."""
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('stock.action_orderpoint')
        flare = self.env['product.product'].search([('is_seeding_flare', '=', True)], limit=1)
        action['domain'] = [('location_id', '=', self.location_id.id)]
        action['context'] = {
            'default_location_id': self.location_id.id,
            'default_warehouse_id': self.location_id.warehouse_id.id,
            'default_product_id': flare.id,
            'default_trigger': 'auto',
        }
        return action
