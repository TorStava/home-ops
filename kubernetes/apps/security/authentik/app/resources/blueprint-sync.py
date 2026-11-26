# Nightly blueprint sync, run with `ak shell` by the authentik-blueprint-sync CronJob.
#
# 1. Force-applies every mounted blueprint, so UI edits to anything described in code
#    and credentials rotated in Bitwarden reach Authentik within a day.
# 2. Fails if any application is not described in code (no app-<slug>.yaml) or has no
#    access-group binding. A failed Job fires KubeJobFailed.
#
# Only log events are printed, never entry attributes: those can hold client secrets.
import os
import sys
from pathlib import Path

from authentik.blueprints.models import BlueprintInstance
from authentik.blueprints.v1.importer import Importer
from authentik.core.models import Application
from authentik.policies.models import PolicyBinding
from authentik.tenants.models import Tenant

BLUEPRINT_DIR = "mounted/cm-authentik-blueprints"
ACCESS_GROUPS = {"Guests", "Household", "Operators"}
# Validate and check only; set by hand to test without changing anything.
DRY_RUN = os.environ.get("BLUEPRINT_SYNC_DRY_RUN") == "1"

files = sorted(p.name for p in Path("/blueprints", BLUEPRINT_DIR).glob("*.yaml"))
# The groups must exist before any binding can find them.
files.sort(key=lambda name: name != "access-groups.yaml")
problems = []

for tenant in Tenant.objects.filter(ready=True):
    with tenant:
        for name in files:
            content = BlueprintInstance(path=f"{BLUEPRINT_DIR}/{name}").retrieve()
            importer = Importer.from_string(content)
            valid, logs = importer.validate()
            if not valid:
                events = sorted({str(log.event) for log in logs if log.log_level in ("warning", "error")})
                problems.append(f"{name}: invalid ({'; '.join(events) or 'no details'})")
                continue
            if DRY_RUN:
                print(f"valid   {name}")
            elif importer.apply():
                print(f"applied {name}")
            else:
                problems.append(f"{name}: apply failed")

        for app in Application.objects.order_by("slug"):
            if f"app-{app.slug}.yaml" not in files:
                problems.append(f"application {app.slug}: not described in code (no app-{app.slug}.yaml)")
            groups = {
                binding.group.name
                for binding in PolicyBinding.objects.filter(target=app, enabled=True, group__isnull=False)
            }
            if not groups & ACCESS_GROUPS:
                problems.append(f"application {app.slug}: no access-group binding")

for problem in problems:
    print(f"PROBLEM {problem}", file=sys.stderr)
print(f"{len(files)} blueprint(s), {len(problems)} problem(s){' (dry run)' if DRY_RUN else ''}")
sys.exit(1 if problems else 0)
