from odoo import _, api, fields, models

# Settings Serein needs from Inventory: flares are stocked by batch, at each
# base's own storage location.
STOCK_GROUPS = ('stock.group_production_lot', 'stock.group_stock_multi_locations')


class StockLocation(models.Model):
    _inherit = 'stock.location'

    is_seeding_release = fields.Boolean(
        string='Seeding Release Location', readonly=True, copy=False,
        help="Flares fired on seeding missions are moved here when the mission is completed.",
    )


class StockPickingType(models.Model):
    _inherit = 'stock.picking.type'

    is_seeding_release = fields.Boolean(
        string='Seeding Releases', readonly=True, copy=False,
        help="Operation type of the transfers that record flares fired on seeding missions.",
    )


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    seeding_mission_id = fields.Many2one(
        'seeding.mission', string='Seeding Mission', readonly=True, copy=False, index='btree_not_null',
    )


class ResCompany(models.Model):
    _inherit = 'res.company'

    def _seeding_release_location(self):
        """The company's "Released to Atmosphere" location, created on first use."""
        self.ensure_one()
        Location = self.env['stock.location'].sudo().with_context(active_test=False)
        location = Location.search(
            [('is_seeding_release', '=', True), ('company_id', '=', self.id)], limit=1,
        )
        if not location:
            location = Location.create({
                'name': _("Released to Atmosphere"),
                'usage': 'inventory',
                'company_id': self.id,
                'is_seeding_release': True,
            })
        return location

    def _seeding_release_picking_type(self, warehouse):
        """The "Seeding Releases" operation type of a warehouse, created on first use."""
        self.ensure_one()
        Type = self.env['stock.picking.type'].sudo().with_context(active_test=False)
        picking_type = Type.search(
            [('is_seeding_release', '=', True), ('warehouse_id', '=', warehouse.id)], limit=1,
        )
        # Internal type: Odoo shows a delivery's partner instead of its
        # destination (transfer form, traceability), and releases have no partner.
        values = {
            'code': 'internal',
            'default_location_src_id': warehouse.lot_stock_id.id,
            'default_location_dest_id': self._seeding_release_location().id,
        }
        if not picking_type:
            picking_type = Type.create({
                **values,
                'name': _("Seeding Releases"),
                'sequence_code': 'REL',
                'warehouse_id': warehouse.id,
                'company_id': self.id,
                'use_create_lots': False,
                'use_existing_lots': True,
                'create_backorder': 'never',
                'print_label': False,
                'is_seeding_release': True,
            })
        elif picking_type.code != 'internal':
            # Release types created as deliveries before 19.0.1.5.1.
            picking_type.write({**values, 'print_label': False})
        return picking_type

    @api.model
    def _seeding_setup_flare_stock(self):
        """Run on every install and update of serein_ops. Idempotent.

        - Switches on Lots & Serial Numbers and Storage Locations, as the
          Inventory settings do.
        - Creates the release location and the release operation type.
        - Moves base locations that sit directly under a warehouse into its
          stock location, so a base reorder rule can use the Buy route.
        """
        group_user = self.env.ref('base.group_user')
        for xmlid in STOCK_GROUPS:
            group_user._apply_group(self.env.ref(xmlid))
        Warehouse = self.env['stock.warehouse'].sudo()
        for company in self.sudo().search([]):
            company._seeding_release_location()
            for warehouse in Warehouse.search([('company_id', '=', company.id)]):
                company._seeding_release_picking_type(warehouse)
        for base in self.env['seeding.base'].sudo().with_context(active_test=False).search([]):
            base._place_location_under_stock()
        return True
