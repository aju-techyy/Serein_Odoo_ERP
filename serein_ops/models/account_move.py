from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class AccountMove(models.Model):
    _inherit = 'account.move'

    seeding_contract_id = fields.Many2one(
        'seeding.contract', string='Seeding Contract', readonly=True, copy=False, index='btree_not_null',
        help="The seeding contract this invoice was created from.",
    )
    seeding_period_start = fields.Date(string='Billing Period Start', readonly=True, copy=False)
    seeding_period_end = fields.Date(string='Billing Period End', readonly=True, copy=False)

    def button_cancel(self):
        result = super().button_cancel()
        self.line_ids._seeding_release_missions()
        return result

    def button_draft(self):
        if any(move.state == 'cancel' and move.seeding_contract_id for move in self):
            raise UserError(_(
                "A cancelled contract invoice cannot be reset to draft: its missions and standby "
                "days are free to be invoiced again. Create a new invoice from the contract instead.",
            ))
        return super().button_draft()


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    seeding_mission_id = fields.Many2one(
        'seeding.mission', string='Seeding Mission', readonly=True, copy=False, index='btree_not_null',
    )
    seeding_standby_start = fields.Date(string='Standby From', readonly=True, copy=False)
    seeding_standby_end = fields.Date(string='Standby To', readonly=True, copy=False)

    @api.constrains('seeding_mission_id')
    def _check_mission_billed_once(self):
        """A mission appears on one invoice line at most, cancelled invoices aside."""
        for line in self.filtered('seeding_mission_id'):
            other = self.sudo().search_count([
                ('id', '!=', line.id),
                ('seeding_mission_id', '=', line.seeding_mission_id.id),
                ('parent_state', '!=', 'cancel'),
            ])
            if other:
                raise ValidationError(_(
                    "Mission %s is already on another invoice.", line.seeding_mission_id.name,
                ))

    def unlink(self):
        missions = self.seeding_mission_id
        result = super().unlink()
        missions.exists()._release_from_invoice()
        return result

    def _seeding_release_missions(self):
        self.seeding_mission_id._release_from_invoice()
