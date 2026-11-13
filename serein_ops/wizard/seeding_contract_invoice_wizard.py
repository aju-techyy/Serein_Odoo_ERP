from datetime import timedelta

from odoo import _, api, fields, models


class SeedingContractInvoiceWizard(models.TransientModel):
    _name = 'seeding.contract.invoice.wizard'
    _description = 'Invoice a Seeding Contract for a Period'

    contract_id = fields.Many2one('seeding.contract', string='Contract', required=True, readonly=True)
    partner_id = fields.Many2one(related='contract_id.partner_id')
    currency_id = fields.Many2one(related='contract_id.currency_id')
    date_from = fields.Date(
        string='From', required=True, default=lambda self: self._default_dates()[0],
    )
    date_to = fields.Date(
        string='To', required=True, default=lambda self: self._default_dates()[1],
    )
    standby_fee = fields.Monetary(related='contract_id.standby_fee')
    flight_hour_rate = fields.Monetary(related='contract_id.flight_hour_rate')
    standby_days = fields.Integer(
        string='Standby Days', compute='_compute_preview',
        help="Season days in the period that no earlier invoice has billed.",
    )
    mission_ids = fields.Many2many(
        'seeding.mission', string='Missions', compute='_compute_preview',
        help="Completed missions that landed in the period and are not invoiced yet.",
    )
    flight_hours = fields.Float(string='Flight Hours', compute='_compute_preview')
    standby_amount = fields.Monetary(string='Standby', compute='_compute_preview')
    flight_amount = fields.Monetary(string='Flight Hours Amount', compute='_compute_preview')
    amount_untaxed = fields.Monetary(string='Untaxed Total', compute='_compute_preview')

    @api.model
    def _default_dates(self):
        """The current calendar month."""
        today = fields.Date.context_today(self)
        first = today.replace(day=1)
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return first, last

    @api.depends('contract_id', 'date_from', 'date_to')
    def _compute_preview(self):
        for wizard in self:
            contract = wizard.contract_id
            if not contract or not wizard.date_from or not wizard.date_to or wizard.date_to < wizard.date_from:
                wizard.update({
                    'standby_days': 0, 'mission_ids': False, 'flight_hours': 0.0,
                    'standby_amount': 0.0, 'flight_amount': 0.0, 'amount_untaxed': 0.0,
                })
                continue
            currency = contract.currency_id
            lines = contract._invoice_line_values(wizard.date_from, wizard.date_to)
            standby = [v for v in lines if v.get('seeding_standby_start')]
            flights = [v for v in lines if v.get('seeding_mission_id')]
            wizard.standby_days = int(sum(v['quantity'] for v in standby))
            wizard.mission_ids = [v['seeding_mission_id'] for v in flights]
            wizard.flight_hours = sum(v['quantity'] for v in flights)
            wizard.standby_amount = sum(currency.round(v['quantity'] * v['price_unit']) for v in standby)
            wizard.flight_amount = sum(currency.round(v['quantity'] * v['price_unit']) for v in flights)
            wizard.amount_untaxed = wizard.standby_amount + wizard.flight_amount

    def action_create_invoice(self):
        self.ensure_one()
        invoice = self.contract_id._create_invoice(self.date_from, self.date_to)
        if not invoice:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Nothing to invoice"),
                    'message': _(
                        "%(contract)s has no uninvoiced standby days and no uninvoiced completed "
                        "missions between %(first)s and %(last)s.",
                        contract=self.contract_id.name,
                        first=self.date_from.strftime('%d %b %Y'),
                        last=self.date_to.strftime('%d %b %Y'),
                    ),
                    'type': 'warning',
                    'sticky': False,
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': invoice.id,
            'view_mode': 'form',
            'views': [(self.env.ref('account.view_move_form').id, 'form')],
            'name': _("Invoice"),
        }
