from datetime import datetime, time, timedelta

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Contracts that can be invoiced: the season is running, or it has ended.
INVOICEABLE_STATES = ('active', 'closed')


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
    invoice_ids = fields.One2many('account.move', 'seeding_contract_id', string='Invoices')
    invoice_count = fields.Integer(compute='_compute_invoice_count')

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

    @api.depends('invoice_ids')
    def _compute_invoice_count(self):
        counts = dict(self.env['account.move'].sudo()._read_group(
            [('seeding_contract_id', 'in', self.ids)], ['seeding_contract_id'], ['__count'],
        )) if self.ids else {}
        for contract in self:
            contract.invoice_count = counts.get(contract._origin, 0)

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

    # Billing ----------------------------------------------------------------

    @api.model
    def _billing_product(self, xml_id):
        product = self.env.ref(xml_id, raise_if_not_found=False)
        if not product:
            raise UserError(_(
                "The service product %s is missing. Update the Serein module to restore it.", xml_id,
            ))
        return product

    def _billed_standby_days(self):
        """Days already invoiced as standby, on invoices that are not cancelled."""
        self.ensure_one()
        lines = self.env['account.move.line'].sudo().search([
            ('move_id.seeding_contract_id', '=', self.id),
            ('parent_state', '!=', 'cancel'),
            ('seeding_standby_start', '!=', False),
            ('seeding_standby_end', '!=', False),
        ])
        days = set()
        for line in lines:
            day = line.seeding_standby_start
            while day <= line.seeding_standby_end:
                days.add(day)
                day += timedelta(days=1)
        return days

    def _unbilled_standby_ranges(self, date_from, date_to):
        """Runs of consecutive season days in the period that no invoice has
        covered yet, as [(first_day, last_day)]."""
        self.ensure_one()
        start = max(date_from, self.season_start)
        end = min(date_to, self.season_end)
        billed = self._billed_standby_days()
        ranges = []
        day = start
        while day <= end:
            if day not in billed:
                if ranges and ranges[-1][1] == day - timedelta(days=1):
                    ranges[-1] = (ranges[-1][0], day)
                else:
                    ranges.append((day, day))
            day += timedelta(days=1)
        return ranges

    def _billable_missions(self, date_from, date_to):
        """Completed, not yet invoiced missions of this contract that landed
        in the period (UTC dates)."""
        self.ensure_one()
        return self.env['seeding.mission'].search([
            ('contract_id', '=', self.id),
            ('state', '=', 'completed'),
            ('invoice_id', '=', False),
            ('landing_at', '>=', datetime.combine(date_from, time.min)),
            ('landing_at', '<', datetime.combine(date_to + timedelta(days=1), time.min)),
        ], order='landing_at, id')

    def _check_invoice_period(self, date_from, date_to):
        self.ensure_one()
        if self.state not in INVOICEABLE_STATES:
            raise UserError(_("Only an active or closed contract can be invoiced (%s).", self.name))
        if not date_from or not date_to:
            raise UserError(_("Set the first and last day of the billing period."))
        if date_to < date_from:
            raise UserError(_("The billing period must end on or after the day it starts."))

    def _invoice_line_values(self, date_from, date_to):
        """The invoice lines for a period: one line per run of unbilled
        standby days at the standby fee, and one line per unbilled completed
        mission for its flight hours at the flight-hour rate."""
        self.ensure_one()
        lines = []
        if self.standby_fee:
            standby = self._billing_product('serein_ops.product_seeding_standby_day')
            for first, last in self._unbilled_standby_ranges(date_from, date_to):
                days = (last - first).days + 1
                lines.append({
                    'product_id': standby.id,
                    'name': _(
                        "Standby, %(first)s to %(last)s (%(days)s days), contract %(contract)s",
                        first=first.strftime('%d %b %Y'), last=last.strftime('%d %b %Y'),
                        days=days, contract=self.name,
                    ),
                    'quantity': days,
                    'price_unit': self.standby_fee,
                    'seeding_standby_start': first,
                    'seeding_standby_end': last,
                })
        missions = self._billable_missions(date_from, date_to)
        if missions:
            flight_hour = self._billing_product('serein_ops.product_seeding_flight_hour')
            for mission in missions:
                lines.append({
                    'product_id': flight_hour.id,
                    'name': _(
                        "Flight hours, mission %(mission)s on %(date)s, %(area)s (%(aircraft)s)",
                        mission=mission.name, date=mission.landing_at.strftime('%d %b %Y'),
                        area=mission.area_id.name, aircraft=mission.aircraft_id.name or '',
                    ),
                    'quantity': mission.flight_hours,
                    'price_unit': self.flight_hour_rate,
                    'seeding_mission_id': mission.id,
                })
        return lines

    def _create_invoice(self, date_from, date_to):
        """Create a draft customer invoice for the period, or nothing if the
        period holds no unbilled standby days and no unbilled missions. The
        missions on it move to billed."""
        self.ensure_one()
        self._check_invoice_period(date_from, date_to)
        lines = self._invoice_line_values(date_from, date_to)
        if not lines:
            return self.env['account.move']
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'invoice_origin': self.name,
            'ref': _(
                "%(contract)s, %(first)s to %(last)s", contract=self.name,
                first=date_from.strftime('%d %b %Y'), last=date_to.strftime('%d %b %Y'),
            ),
            'seeding_contract_id': self.id,
            'seeding_period_start': date_from,
            'seeding_period_end': date_to,
            'invoice_line_ids': [Command.create(vals) for vals in lines],
        })
        missions = invoice.invoice_line_ids.seeding_mission_id
        missions.write({'state': 'billed', 'invoice_id': invoice.id})
        for mission in missions:
            mission.message_post(body=_("Invoiced on %s.", invoice._get_html_link()))
        standby_days = sum(int(v['quantity']) for v in lines if v.get('seeding_standby_start'))
        self.message_post(body=_(
            "Invoice %(invoice)s drafted for %(first)s to %(last)s: %(days)s standby days and "
            "%(count)s missions.",
            invoice=invoice._get_html_link(), first=date_from.strftime('%d %b %Y'),
            last=date_to.strftime('%d %b %Y'), days=standby_days, count=len(missions),
        ))
        return invoice

    def action_create_invoice(self):
        self.ensure_one()
        if self.state not in INVOICEABLE_STATES:
            raise UserError(_("Only an active or closed contract can be invoiced."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create Invoice"),
            'res_model': 'seeding.contract.invoice.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_contract_id': self.id},
        }

    def action_view_invoices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_out_invoice_type')
        action['domain'] = [('seeding_contract_id', '=', self.id)]
        action['context'] = {'default_move_type': 'out_invoice', 'create': False}
        if len(self.invoice_ids) == 1:
            action['views'] = [(self.env.ref('account.view_move_form').id, 'form')]
            action['res_id'] = self.invoice_ids.id
        return action
