from odoo import api, fields, models

PURPOSES = [
    ('plan', 'Plan'),
    ('replan', 'Re-plan'),
    ('briefing', 'Briefing'),
    ('summary', 'Client Summary'),
]
OUTCOMES = [
    ('accepted', 'Accepted'),
    ('rejected', 'Rejected'),
    ('failed', 'Failed'),
]


class SeedingAiLog(models.Model):
    """Audit trail of every call to the language model: what was sent, what
    came back, how long it took, and whether the answer was used.
    Records are written by the AI service as superuser and are read-only
    for every user."""
    _name = 'seeding.ai.log'
    _description = 'AI Activity Log'
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    purpose = fields.Selection(PURPOSES, required=True, readonly=True)
    outcome = fields.Selection(OUTCOMES, required=True, readonly=True)
    outcome_reason = fields.Text(
        string='Reason', readonly=True,
        help="Why the answer was rejected or the call failed.",
    )
    mission_id = fields.Many2one(
        'seeding.mission', string='Mission', readonly=True, index=True, ondelete='set null',
    )
    alert_id = fields.Many2one(
        'seeding.cloud.alert', string='Cloud Alert', readonly=True, index=True, ondelete='set null',
    )
    user_id = fields.Many2one(
        'res.users', string='Requested By', readonly=True, default=lambda self: self.env.user,
    )
    model_name = fields.Char(string='Model', readonly=True)
    model_version = fields.Char(
        string='Model Version', readonly=True,
        help="The model version reported in the answer.",
    )
    http_status = fields.Integer(string='HTTP Status', readonly=True)
    duration = fields.Float(string='Duration (s)', digits=(6, 2), readonly=True)
    prompt_tokens = fields.Integer(string='Prompt Tokens', readonly=True)
    output_tokens = fields.Integer(string='Output Tokens', readonly=True)
    request = fields.Text(string='Request', readonly=True, help="The body sent to the model.")
    response = fields.Text(string='Response', readonly=True, help="The answer, exactly as received.")

    @api.depends('purpose', 'mission_id.name', 'alert_id.name')
    def _compute_name(self):
        labels = dict(PURPOSES)
        for log in self:
            subject = log.mission_id.name or log.alert_id.name
            log.name = f"{labels.get(log.purpose, '')} {subject or ''}".strip()
