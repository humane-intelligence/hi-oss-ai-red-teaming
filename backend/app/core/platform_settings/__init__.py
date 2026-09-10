"""Platform-wide admin-tunable policy: the one-row settings singleton (django-solo analogue).

`PlatformSettings` ([models.py](models.py)) holds knobs that ship with a default but can be
overridden and persisted: data licensing (`default_license_id`, env-seeded; the catalog lives
in [app/core/licenses/](../licenses/)), registration policy, password policy, and
password-reset throttling. The model is the inventory — the service derives the knob list from
it rather than repeating it. Reads never persist: before the first admin write they serve a
transient instance carrying the shipped defaults ([service.py](service.py)).
"""
