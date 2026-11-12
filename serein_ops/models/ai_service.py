import json
import logging
import os
import time

import requests

from odoo import api, models

_logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'gemini-3.5-flash-lite'
MODEL_PARAM = 'serein_ops.gemini_model'
API_KEY_ENV = 'GEMINI_API_KEY'
GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent'
GEMINI_TIMEOUT = 30  # seconds
TEMPERATURE = 0.2


class SeedingAiService(models.AbstractModel):
    """Calls Google Gemini with a fixed JSON response schema and logs every
    call in seeding.ai.log.

    Only `_request` talks to the network, so tests mock that one method.
    The API key is read from the server environment and is never stored,
    logged, or sent anywhere but the request header."""
    _name = 'seeding.ai.service'
    _description = 'Serein AI Service (Gemini)'

    @api.model
    def _model_name(self):
        return self.env['ir.config_parameter'].sudo().get_param(MODEL_PARAM) or DEFAULT_MODEL

    @api.model
    def _api_key(self):
        return os.environ.get(API_KEY_ENV) or ''

    @api.model
    def _request(self, model, body, api_key):
        """POST one generateContent request. Returns (HTTP status, body text)."""
        response = requests.post(
            GEMINI_URL % model,
            json=body,
            headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json'},
            timeout=GEMINI_TIMEOUT,
        )
        return response.status_code, response.text

    @api.model
    def generate(self, purpose, instructions, payload, schema, mission=None, alert=None):
        """Ask the model for a JSON object shaped by `schema`.

        Returns (data, log). `data` is the decoded object, or None when the
        call failed or the answer could not be read; the log then says why.
        A returned log has outcome 'accepted': the caller validates `data`
        and calls `log._reject(reason)` if it cannot be used."""
        model = self._model_name()
        body = {
            'systemInstruction': {'parts': [{'text': instructions}]},
            'contents': [{
                'role': 'user',
                'parts': [{'text': json.dumps(payload, indent=1, default=str)}],
            }],
            'generationConfig': {
                'responseMimeType': 'application/json',
                'responseSchema': schema,
                'temperature': TEMPERATURE,
            },
        }
        log_vals = {
            'purpose': purpose,
            'mission_id': mission.id if mission else False,
            'alert_id': alert.id if alert else False,
            'model_name': model,
            'request': json.dumps(body, indent=2, default=str),
        }

        api_key = self._api_key()
        if not api_key:
            return None, self._log(
                log_vals, 'failed',
                "No Gemini API key is configured on the server (%s)." % API_KEY_ENV,
            )

        started = time.monotonic()
        try:
            status, text = self._request(model, body, api_key)
        except Exception as error:  # noqa: BLE001 - any failure falls back to the rules
            log_vals['duration'] = time.monotonic() - started
            return None, self._log(
                log_vals, 'failed',
                "Could not reach Gemini: %s: %s" % (type(error).__name__, error),
            )
        log_vals.update({
            'duration': time.monotonic() - started,
            'http_status': status,
            'response': text,
        })

        try:
            envelope = json.loads(text or '')
        except ValueError:
            envelope = None
        if status != 200:
            message = ''
            if isinstance(envelope, dict) and isinstance(envelope.get('error'), dict):
                error = envelope['error']
                message = ': %s %s' % (error.get('status') or '', error.get('message') or '')
            return None, self._log(log_vals, 'failed', "Gemini answered HTTP %s%s" % (status, message.rstrip()))
        if not isinstance(envelope, dict):
            return None, self._log(log_vals, 'rejected', "The answer is not a readable Gemini response.")

        usage = envelope.get('usageMetadata') or {}
        log_vals.update({
            'model_version': envelope.get('modelVersion') or False,
            'prompt_tokens': usage.get('promptTokenCount') or 0,
            'output_tokens': usage.get('candidatesTokenCount') or 0,
        })
        candidates = envelope.get('candidates') or []
        parts = ((candidates[0].get('content') or {}).get('parts') or []) if candidates else []
        answer = ''.join(
            part.get('text') or '' for part in parts
            if isinstance(part, dict) and not part.get('thought')
        ).strip()
        if not answer:
            reason = (candidates[0].get('finishReason') if candidates else None) \
                or (envelope.get('promptFeedback') or {}).get('blockReason') or 'no text'
            return None, self._log(log_vals, 'rejected', "The answer contained no text (%s)." % reason)
        try:
            data = json.loads(answer)
        except ValueError:
            return None, self._log(log_vals, 'rejected', "The answer is not valid JSON.")
        if not isinstance(data, dict):
            return None, self._log(log_vals, 'rejected', "The answer is not a JSON object.")
        return data, self._log(log_vals, 'accepted')

    @api.model
    def _log(self, vals, outcome, reason=False):
        log = self.env['seeding.ai.log'].sudo().create({
            **vals, 'outcome': outcome, 'outcome_reason': reason,
        })
        _logger.info(
            "Serein AI %s call: %s in %.2f s (model %s)%s",
            vals.get('purpose'), outcome, vals.get('duration') or 0.0,
            vals.get('model_name'), ': %s' % reason if reason else '',
        )
        return log


class SeedingAiLog(models.Model):
    _inherit = 'seeding.ai.log'

    def _reject(self, reason):
        self.sudo().write({'outcome': 'rejected', 'outcome_reason': reason})
        _logger.info("Serein AI %s answer rejected: %s", self.purpose, reason)
