"""Seed the local UI stand (docker-compose.ui.yml) with an organization, a project and an issue.

Run inside the Sentry container, which is where the `sentry` package lives:

    docker compose -f docker-compose.ui.yml exec -T web sentry exec /seed.py

The Group is created straight through the ORM — exactly as tests/integration/ does it — so the
stand needs no event ingest (no Relay, no ingest consumers). This is a LOCAL STAND: the user is
a superuser with a known password.
"""

from __future__ import annotations

from django.utils import timezone
from sentry.models.group import Group, GroupStatus
from sentry.models.organization import Organization
from sentry.models.organizationmember import OrganizationMember
from sentry.models.project import Project
from sentry.models.team import Team
from sentry.users.models.user import User

ORG_SLUG = "jaga-test"
PROJECT_SLUG = "jaga-demo"
ADMIN_EMAIL = "admin@example.com"

user = User.objects.get(email=ADMIN_EMAIL)

org, _ = Organization.objects.get_or_create(slug=ORG_SLUG, defaults={"name": "Jaga Test"})
# Without an owner membership the organization is invisible to the user in the UI, and the
# integration directory (an organization-scoped route) 403s.
OrganizationMember.objects.get_or_create(
    organization=org, user_id=user.id, defaults={"role": "owner"}
)

team, _ = Team.objects.get_or_create(
    organization=org, slug="jaga-team", defaults={"name": "Jaga Team"}
)
project, _ = Project.objects.get_or_create(
    organization=org,
    slug=PROJECT_SLUG,
    defaults={"name": "Jaga Demo", "platform": "javascript"},
)
project.add_team(team)

# A recognizable issue: `get_create_issue_config()` pre-fills the task title from
# `group.title` and the description from the culprit + the issue link, so both need to be
# visibly distinct in the browser.
group, created = Group.objects.get_or_create(
    project=project,
    culprit="app/components/UserProfile.tsx in renderAvatar",
    defaults={
        "message": "TypeError: cannot read property 'id' of undefined",
        "level": 40,  # ERROR
        "status": GroupStatus.UNRESOLVED,
        "times_seen": 42,
        "last_seen": timezone.now(),
        "first_seen": timezone.now(),
        "data": {
            "type": "error",
            "metadata": {
                "type": "TypeError",
                "value": "cannot read property 'id' of undefined",
                "function": "renderAvatar",
            },
        },
    },
)

print(f"org        = {org.slug} (id={org.id})")
print(f"project    = {project.slug} (id={project.id})")
print(f"group      = {group.id} (created={created}) title={group.title!r}")
print(f"issue url  = http://localhost:9000/organizations/{org.slug}/issues/{group.id}/")
