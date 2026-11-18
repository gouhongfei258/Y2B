"""Blueprints that keep the historical endpoint names.

Templates and the CSRF check call url_for('index') and compare
request.endpoint to 'login'. Flask would otherwise prefix those names
with the blueprint name.
"""
from flask import Blueprint as FlaskBlueprint

try:
    from flask.sansio.blueprints import BlueprintSetupState
except ImportError:  # pragma: no cover - Flask 2 fallback
    from flask.blueprints import BlueprintSetupState


class _UnprefixedSetupState(BlueprintSetupState):
    def add_url_rule(self, rule, endpoint=None, view_func=None, **options):
        if self.url_prefix is not None:
            if rule:
                rule = "/".join((self.url_prefix.rstrip("/"), rule.lstrip("/")))
            else:
                rule = self.url_prefix
        options.setdefault("subdomain", self.subdomain)
        if endpoint is None:
            endpoint = view_func.__name__
        defaults = self.url_defaults
        if "defaults" in options:
            defaults = dict(defaults, **options.pop("defaults"))
        self.app.add_url_rule(rule, endpoint, view_func, defaults=defaults, **options)


class Blueprint(FlaskBlueprint):
    def make_setup_state(self, app, options, first_registration=False):
        return _UnprefixedSetupState(self, app, options, first_registration)
