"""Central model registry — import every model module here.

SQLModel.metadata is populated whenever this module is imported, so Alembic
and any tooling needing the full schema can import this single module.

Importing app.core.base_model first installs the constraint naming convention
on SQLModel.metadata before any table=True declaration is loaded.

When you add a new model module, add its import here and nowhere else:
    import app.core.conversations.models  # noqa: F401
"""

import app.core.ai_gateway.models
import app.core.annotations.models
import app.core.audit.models
import app.core.auth.models
import app.core.auth.object_roles.models
import app.core.base_model
import app.core.conversations.models
import app.core.email.models
import app.core.evaluations.models
import app.core.exports.models
import app.core.licenses.models
import app.core.media.models
import app.core.notifications.models
import app.core.organizations.models
import app.core.platform_settings.models
import app.core.reviews.models
import app.core.saved_views.models
import app.core.terms.models  # noqa: F401

__all__: list[str] = []
