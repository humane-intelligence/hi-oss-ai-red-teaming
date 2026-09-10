"""Organizations — the tenancy root.

A `User` optionally belongs to one organization, and (in a later stage)
evaluation groups are scoped to one. Organizations are created and managed by
platform admins only; membership is a nullable FK so existing/orgless accounts
keep working unchanged.
"""
