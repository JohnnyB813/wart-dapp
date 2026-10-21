"""Validated, persistent wallet preferences; no signing material."""
DEFAULTS = dict(auto_minfee=True, auto_failover=True, auto_lowest_fee=True,
                auto_lock_minutes=5, refresh_seconds=40, fallback_fee_e8=9992)


def validate_preferences(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('Unsupported wallet preferences.')
    values = dict(DEFAULTS, **values)
    for key in ('auto_minfee', 'auto_failover', 'auto_lowest_fee'):
        if type(values[key]) is not bool:
            raise ValueError('Invalid wallet preference.')
    for key, low, high in [('auto_lock_minutes', 1, 60), ('refresh_seconds', 10, 300)]:
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError('Wallet preference is outside the supported range.')
    from utils import round_fee_e8
    if round_fee_e8(values['fallback_fee_e8']) != values['fallback_fee_e8']:
        raise ValueError('The fallback fee must be locally rounded.')
    return dict(values)


def load_preferences(store):
    import json
    raw = store.setting('wallet_preferences')
    if raw is not None:
        try:
            return validate_preferences(json.loads(raw))
        except (ValueError, TypeError):
            pass
    return dict(DEFAULTS)


def save_preferences(store, values):
    import json
    store.set_setting('wallet_preferences', json.dumps(validate_preferences(values)))
