from __future__ import annotations

from django.core.asgi import get_asgi_application

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "veltrix_project.settings")

application = get_asgi_application()
