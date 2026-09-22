"""Make the scripts directory executable."""
import os
import stat

here = os.path.dirname(os.path.abspath(__file__))
for name in ("setup-subscription.sh",):
    path = os.path.join(here, name)
    if os.path.exists(path):
        current = os.stat(path).st_mode
        os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)