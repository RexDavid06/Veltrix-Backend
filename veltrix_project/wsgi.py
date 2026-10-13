from __future__ import annotations

from django.core.wsgi import get_wsgi_application

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "veltrix_project.settings")

application = get_wsgi_application()
