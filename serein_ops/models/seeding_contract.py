from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class SeedingContract(models.Model):
    _name = 'seeding.contract'
    _description = 'Seeding Contract'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'season_start desc, name desc'

    name = fields.Char(
        string='Reference', required=True, copy=False, readonly=True,
        default=lambda self: _('New'), tracking=True,
    )
    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, tracking=True, index=True,
        domain=[('is_company', '=', True)],
        help="The organisation that pays for seeding. Only companies can be clients.",
    )
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(related='company_id.currency_id')
    area_ids = fields.Many2many(
        'seeding.target.area', 'seeding_contract_area_rel', 'contract_id', 'area_id',
        string='Target Areas', domain="[('partner_id', '=', partner_id)]",
    )
    season_start = fields.Date(required=True, tracking=True)
    season_end = fields.Date(required=True, tracking=True)
    flight_hour_budget = fields.Float(
        string='Flight-Hour Budget', required=True, tracking=True,
        help="Flight hours the client has bought for the season.",
    )
    mission_ids = fields.One2many('seeding.mission', 'contract_id', string='Missions')
    mission_count = fields.Integer(compute='_compute_mission_count')
    hours_used = fields.Float(
        string='Hours Used', compute='_compute_hours_used',
        help="Flight hours of this contract's completed and billed missions.",
    )
    hours_remaining = fields.Float(compute='_compute_hours_used')
    standby_fee = fields.Monetary(
        string='Standby Fee per Day', currency_field='currency_id', tracking=True,
    )
    flight_hour_rate = fields.Monetary(
        string='Flight-Hour Rate', currency_field='currency_id', tracking=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('active', 'Active'),
            ('closed', 'Closed'),
            ('cancelled', 'Cancelled'),
        ],
        string='Status', default='draft', required=True, tracking=True, copy=False,
    )
    note = fields.Html()

    _name_uniq = models.Constraint(
        'unique (name, company_id)',
        'The contract reference must be unique.',
    )
    _season_order = models.Constraint(
        'check(season_end >= season_start)',
        'The season must end on or after the day it starts.',
    )
    _budget_positive = models.Constraint(
        'check(flight_hour_budget >= 0)',
        'The flight-hour budget cannot be negative.',
    )
    _rates_positive = models.Constraint(
        'check(standby_fee >= 0 and flight_hour_rate >= 0)',
        'Fees and rates cannot be negative.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('seeding.contract') or _('New')
        return super().create(vals_list)

    @api.depends('flight_hour_budget', 'mission_ids.state', 'mission_ids.flight_hours')
    def _compute_hours_used(self):
        for contract in self:
            flown = contract.mission_ids.filtered(lambda m: m.state in ('completed', 'billed'))
            contract.hours_used = sum(flown.mapped('flight_hours'))
            contract.hours_remaining = contract.flight_hour_budget - contract.hours_used

    @api.depends('mission_ids')
    def _compute_mission_count(self):
        for contract in self:
            contract.mission_count = len(contract.mission_ids)

    def action_view_missions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('serein_ops.seeding_mission_action')
        action['domain'] = [('contract_id', '=', self.id)]
        action['context'] = {'default_contract_id': self.id}
        return action

    @api.constrains('partner_id', 'area_ids')
    def _check_areas_belong_to_client(self):
        for contract in self:
            wrong = contract.area_ids.filtered(lambda a: a.partner_id != contract.partner_id)
            if wrong:
                raise ValidationError(_(
                    "Target area %(area)s belongs to %(owner)s, not to %(client)s.",
                    area=wrong[0].name, owner=wrong[0].partner_id.display_name,
                    client=contract.partner_id.display_name,
                ))

    @api.onchange('partner_id')
    def _onchange_partner_id(self):
        self.area_ids = self.area_ids.filtered(lambda a: a.partner_id == self.partner_id)

    def action_activate(self):
        for contract in self:
            if contract.state != 'draft':
                raise UserError(_("Only a draft contract can be activated."))
            if not contract.area_ids:
                raise UserError(_("Add at least one target area before activating %s.", contract.name))
        self.state = 'active'

    def action_close(self):
        if any(c.state != 'active' for c in self):
            raise UserError(_("Only an active contract can be closed."))
        self.state = 'closed'

    def action_cancel(self):
        if any(c.state not in ('draft', 'active') for c in self):
            raise UserError(_("Only a draft or active contract can be cancelled."))
        self.state = 'cancelled'

    def action_draft(self):
        if any(c.state != 'cancelled' for c in self):
            raise UserError(_("Only a cancelled contract can be reset to draft."))
        self.state = 'draft'
