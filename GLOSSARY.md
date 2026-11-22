# Home-ops

The services running on this home k3s cluster and how people sign in to them.

## Sign-in and access

**Access group**:
An Authentik group whose only purpose is to open a set of applications to its members.
_Avoid_: Role, permission group

**Authentik Role**:
Authentik's own permission set for administering Authentik objects; it never decides who may open an application.
_Avoid_: using "role" to mean an access group
