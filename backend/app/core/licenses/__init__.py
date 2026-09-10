"""Data-licensing policy: the data-license table and its curated catalog.

Data licenses are DB rows (`DataLicense`, [models.py](models.py)) managed through the CRUD API.
The *curated* set ([catalog.py](catalog.py)) is the code-shipped list of well-known data licenses
the platform ships with; `sync_licenses` ([service.py](service.py)) upserts it into the table
(idempotent, deterministic ids). User-authored licenses are plain rows with an author
(`created_by_id`) and no `spdx_id`.

The platform default lives on the `app.core.platform_settings` singleton. Evaluation
groups and evaluations each carry an optional data-license override; the effective license cascades
most-specific-first — evaluation → group → platform default.
"""
